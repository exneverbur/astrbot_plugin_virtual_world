"""动作注册表视图与身份（A2）。

为什么要这个模块
----------------

A1 的事实清单（``.vwtest/embedding-b-work-arrangement.md`` §11.6）说得很清楚：现在动作的
唯一身份就是一个裸字符串 id，三条来源在 ``core/engine.py`` 的
``_apply_extension_actions`` 里合流时是「**已存在的赢**」——``definition.id in existing``
就 ``continue``，只留一条 warning。于是两件事说不清：

1. 「这条 ``hug`` 到底是内置的、我改的、还是某个扩展带来的」，运行时问不出来；
2. 扩展定义被挡住时**没有任何出口**：编辑器、日志、模型都看不到它被丢了。

本模块给出一份**只读视图**：每条定义都有 ``(source_namespace, action_id)`` 二元身份、
来源版本、有效定义版本，以及「哪一份在生效、哪一份被谁挡住」的冲突清单。

为什么不重建动作 id（硬约束）
------------------------------

``DEFAULT_WORLD["actions"]`` 里的 id 是**线上数据的一部分**：提示词、计划、动作日志、
编辑器分组、``core/engine.py`` 里一排硬编码分支、用户已经存下来的 world.json 都按裸 id 走。
所以本模块一个 id 都不改，只用命名空间把「同一个 id 的不同来源」分开：

* :data:`NS_CORE`（``"core"``）：内置默认动作；
* :data:`NS_WORLD`（``"world"``）：用户配置里的那一份（改过的内置，以及用户自己新建的）；
* ``"ext:<扩展名>"``（:func:`ext_namespace`）：扩展带来的动作。

本模块**不加载扩展、不读文件、不写库、不连网络**，全是纯计算：把 ``WorldConfig``（或它的
``actions`` 列表）与一份 ``(扩展名, 动作定义)`` 列表算成视图。所以它能在没有数据库、没有
宿主、没有事件循环的情况下单测；接线（引擎每轮怎么用这份视图）属于 A6。

踩过的坑：派生信息不能进版本
----------------------------

``discovery``（A3）是派生内容。算「有效定义版本」与「输入指纹」时必须把它排掉
（:data:`DERIVED_CONTAINER_FIELDS`）：否则后台整理写一次简述 → 有效定义版本变了 →
上游认为「定义改了」 → 再入队一次 → 永远在整理同一个动作。

认不出 / 坏输入一律降级、绝不外抛
----------------------------------

``world`` 是 ``None``、``actions`` 不是列表、扩展定义校验不过、id 是空的、带不可见字符——
全部只记进 :attr:`ActionRegistry.notes` / 冲突清单，返回值退化成「这份没进来」。
本模块任何公开函数都不抛异常。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .defaults import DEFAULT_WORLD
from .models import ActionDef, clean_identifier


# ---------------------------------------------------------------------------
# 常量：命名空间与来源
# ---------------------------------------------------------------------------

NS_CORE = "core"
"""内置默认动作的命名空间（用户没改过的那些内置动作就用这个身份）。"""

NS_WORLD = "world"
"""用户配置那一份的命名空间（用户改过的内置、以及用户自己新建的动作）。"""

NS_EXT_PREFIX = "ext:"
"""扩展命名空间前缀，完整形状是 ``ext:<扩展名>``。"""

IDENTITY_SEPARATOR = "::"
"""身份文本里的分隔符：``"core::say"``。

为什么用双冒号：命名空间本身可能带一个冒号（``ext:romance``），单冒号切不干净。
:func:`ext_namespace` 会把扩展名里的 ``"::"`` 洗成 ``"_"``，所以这个分隔符在身份文本里
永远是**第一个**出现的那个。
"""

ORIGIN_DEFAULT = "default"
ORIGIN_USER = "user"
ORIGIN_EXTENSION = "extension"

SOURCE_CORE = "core"
SOURCE_WORLD = "world"
SOURCE_EXTENSION = "extension"

CONFLICT_SHADOW = "shadow"
"""用户配置盖住了同名的内置默认（**设计内的覆盖**，信息级）。"""

CONFLICT_EXT_SHADOWED = "extension_shadowed"
"""扩展动作被同名的内置 / 用户配置挡住（引擎现在**静默丢弃**的就是这一种）。"""

CONFLICT_EXT_DUPLICATE = "extension_duplicate"
"""两个扩展注册了同一个 id；后一个在运行时**永远不可达**。"""

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

STATUS_FOUND = "found"
STATUS_MISSING = "missing"
STATUS_CONFLICT = "conflict"

DERIVED_CONTAINER_FIELDS: tuple[str, ...] = ("discovery",)
"""派生信息容器：算「有效定义版本 / 输入指纹」时排除掉（理由见模块 docstring）。"""

REVISION_CHARS = 32
"""版本号的十六进制长度（与 ``topic_vectors.text_hash`` 同口径）。"""

CORE_SOURCE_VERSION = ""
"""内置动作层的「来源版本」前缀。默认动作用内容哈希，不编版本号。"""


# ---------------------------------------------------------------------------
# 身份（纯函数）
# ---------------------------------------------------------------------------


def ext_namespace(name: Any) -> str:
    """扩展名 → 命名空间：``"romance"`` → ``"ext:romance"``；认不出回空串。"""

    text = clean_identifier(name).replace(IDENTITY_SEPARATOR, "_")
    return f"{NS_EXT_PREFIX}{text}" if text else ""


def is_extension_namespace(namespace: Any) -> bool:
    """这个命名空间是不是扩展（``ext:`` 开头且后面有名字）。"""

    text = str(namespace or "")
    return text.startswith(NS_EXT_PREFIX) and len(text) > len(NS_EXT_PREFIX)


def extension_name_of(namespace: Any) -> str:
    """``"ext:romance"`` → ``"romance"``；不是扩展命名空间回空串。"""

    text = str(namespace or "")
    return text[len(NS_EXT_PREFIX) :] if is_extension_namespace(text) else ""


def source_kind(namespace: Any) -> str:
    """命名空间 → 来源大类：``core`` / ``world`` / ``extension``；认不出回空串。"""

    text = str(namespace or "")
    if text == NS_CORE:
        return SOURCE_CORE
    if text == NS_WORLD:
        return SOURCE_WORLD
    if is_extension_namespace(text):
        return SOURCE_EXTENSION
    return ""


def make_identity(namespace: Any, action_id: Any) -> tuple[str, str]:
    """拼一个身份 ``(source_namespace, action_id)``；两侧都做 id 清洗。

    ``namespace`` 认不出（不是 core / world / ext:）时**原样保留**——注册表里
    以后可能有别的来源，这里不替调用方判死。``action_id`` 空就是空。
    """

    return (str(namespace or "").strip(), clean_identifier(action_id))


def identity_text(namespace: Any, action_id: Any) -> str:
    """身份 → 文本：``("core", "say")`` → ``"core::say"``；没有 id 就回空串。"""

    space, name = make_identity(namespace, action_id)
    if not name:
        return ""
    return f"{space}{IDENTITY_SEPARATOR}{name}" if space else name


def parse_identity_text(text: Any) -> tuple[str, str]:
    """身份文本 → ``(namespace, action_id)``。

    认不出的形状退化成「没有命名空间」（第一项空串、第二项是裸 id）——这是**故意的**：
    老数据里到处都是裸 id，把它们标成「命名空间未知」比硬塞一个 ``core`` 要诚实；
    真正的确定性映射由 :meth:`ActionRegistry.resolve` 负责。
    """

    raw = str(text or "").strip()
    if not raw:
        return ("", "")
    if IDENTITY_SEPARATOR in raw:
        space, _, rest = raw.partition(IDENTITY_SEPARATOR)
        return (space.strip(), clean_identifier(rest))
    return ("", clean_identifier(raw))


def source_label(namespace: Any) -> str:
    """命名空间 → 中文说明（给冲突提示用）。认不出回它自己。"""

    kind = source_kind(namespace)
    if kind == SOURCE_CORE:
        return "内置默认"
    if kind == SOURCE_WORLD:
        return "用户配置"
    if kind == SOURCE_EXTENSION:
        return f"扩展「{extension_name_of(namespace)}」"
    return str(namespace or "未知来源")


# ---------------------------------------------------------------------------
# 定义载荷与版本（纯函数）
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、无多余空白、非 ASCII 不转义。失败回空串（绝不抛）。"""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return ""


def definition_payload(action: Any, *, drop_derived: bool = True) -> dict[str, Any]:
    """动作定义 → 可序列化的普通 dict；``drop_derived`` 时排除 :data:`DERIVED_CONTAINER_FIELDS`。

    认不出（不是 ``ActionDef`` 也不是 dict）回空 dict。
    """

    if isinstance(action, dict):
        data: dict[str, Any] = dict(action)
    else:
        dump = getattr(action, "model_dump", None)
        if not callable(dump):
            return {}
        try:
            dumped = dump(mode="json")
        except Exception:
            return {}
        if not isinstance(dumped, dict):
            return {}
        data = dict(dumped)
    if drop_derived:
        for name in DERIVED_CONTAINER_FIELDS:
            data.pop(name, None)
    return data


def definition_revision(action: Any) -> str:
    """一条定义的**有效定义版本**：权威字段的 sha256 前 32 位；算不出来回空串。

    这是「索引版本依据最终有效定义计算」的落地点：**只看这一条定义自己**，
    不看它在哪一层、也不看扩展的版本号。
    """

    payload = definition_payload(action)
    if not payload:
        return ""
    text = canonical_json(payload)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:REVISION_CHARS]


def layer_revision(namespace: Any, definitions: Any, *, version: Any = "") -> str:
    """一层的**来源版本**：``"<扩展自报版本>#<内容哈希>"``；没有内容也没版本回空串。

    为什么把扩展的 ``version`` 接在内容哈希前面：扩展作者改了代码但忘了让内容变化时，
    版本号一变下游也能认得出「来源换了」；而只改版本号、内容没变的升级则靠哈希那半段判
    「定义其实没变」（A4/A5 靠它决定要不要重新整理）。
    """

    rows: list[str] = []
    seen: set[str] = set()
    for item in _iter_definitions(definitions):
        action_id = _definition_id(item)
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        rows.append(f"{action_id}\t{definition_revision(item)}")
    rows.sort()
    text = str(version or "").strip()
    if not rows and not text:
        return ""
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:REVISION_CHARS]
    return f"{text}#{digest}"


def default_action_payloads(defaults: Any = None) -> dict[str, dict[str, Any]]:
    """内置默认动作的 ``{id: 原始 dict}``；``defaults`` 不给就读 ``DEFAULT_WORLD``。

    重复 id 只留**第一个**（``parse_world`` 也是这个口径：后一个丢弃并警告）；
    坏输入（不是列表、元素不是 dict、id 空）直接跳过——回空 dict 也不抛。
    """

    raw = DEFAULT_WORLD.get("actions") if defaults is None else defaults
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return {}
    picked: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        action_id = clean_identifier(item.get("id"))
        if action_id and action_id not in picked:
            picked[action_id] = dict(item)
    return picked


def legacy_action_map(defaults: Any = None) -> dict[str, str]:
    """裸 id → 「只按内置那层看，它会解析成哪一份身份」的**确定性**静态映射。

    返回 ``{id: "core::<id>"}``。用户改过的那部分只有拿到真实世界配置才能判，
    所以完整解析要用 :func:`resolve_action`；这张表是给脚本 / 编辑器做对照用的。
    """

    return {
        action_id: identity_text(NS_CORE, action_id)
        for action_id in default_action_payloads(defaults)
    }


def extension_action_pairs(host: Any) -> list[tuple[str, dict]]:
    """从 ``ExtensionHost``（或任何有 ``specs`` 的对象）取出 ``(扩展名, 动作定义)``。

    保持扩展注册顺序（``specs`` 是 dict，Python 3.7+ 保留插入顺序），因为运行时
    「撞名时第一个赢」就是按这个顺序来的（``core/engine.py`` 的 ``_apply_extension_actions``）。
    ``host`` 是 ``None`` / 形状不对 / 单个 spec 属性访问炸了：跳过那一份，绝不抛。
    """

    specs = getattr(host, "specs", None)
    if not isinstance(specs, dict):
        return []
    pairs: list[tuple[str, dict]] = []
    for name, spec in specs.items():
        try:
            actions = getattr(spec, "actions", None) or []
        except Exception:  # pragma: no cover - 属性访问炸了也不该拖垮注册表
            continue
        for item in actions:
            if isinstance(item, dict):
                pairs.append((str(name or ""), dict(item)))
    return pairs


def extension_versions(host: Any) -> dict[str, str]:
    """``{扩展命名空间: 扩展自报 version}``（没写版本号的扩展不进表）。"""

    specs = getattr(host, "specs", None)
    if not isinstance(specs, dict):
        return {}
    picked: dict[str, str] = {}
    for name, spec in specs.items():
        namespace = ext_namespace(name)
        version = str(getattr(spec, "version", "") or "").strip()
        if namespace and version:
            picked[namespace] = version
    return picked


# ---------------------------------------------------------------------------
# 视图的数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionEntry:
    """注册表里的一条定义（一个 ``(source_namespace, action_id)`` 身份一份）。"""

    namespace: str
    action_id: str
    definition: Any
    origin: str
    source_revision: str
    effective_revision: str
    active: bool = True
    """这份身份在**当前世界**里能不能被执行。

    ``False`` 有两种：被别的来源盖住（``shadowed_by`` 非空），或者「默认里有、
    当前世界配置里没有」（用户把这条内置删了）——后者不是冲突，只是不在场。
    """

    shadowed_by: tuple[str, str] | None = None
    """被哪一份盖住（``None`` = 没被盖住）。"""

    supersedes: tuple[str, str] | None = None
    """这份盖住了谁（用户配置盖住内置时，内置那份的身份）。"""

    extension: str = ""
    """扩展名（非扩展为空串）。"""

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.action_id)

    @property
    def identity_text(self) -> str:
        return identity_text(self.namespace, self.action_id)

    @property
    def shadowed(self) -> bool:
        """被别的来源盖住了（``active=False`` 且知道是谁盖的）。"""

        return self.shadowed_by is not None

    @property
    def present(self) -> bool:
        """这份身份在**当前世界**里存在（生效，或正被别人盖住）。

        「默认里有、用户删了」的内置条目 ``present=False``——它只是对照物。
        """

        return self.active or self.shadowed

    @property
    def name(self) -> str:
        """显示名：没写名字就用 id（和 ``parse_world`` 的补全口径一致）。"""

        text = str(getattr(self.definition, "name", "") or "")
        return text or self.action_id

    @property
    def builtin(self) -> bool:
        return bool(getattr(self.definition, "builtin", False))

    @property
    def enabled(self) -> bool:
        """定义自己的 ``enabled``（与「这份身份在不在场」是两件事，别混）。"""

        return bool(getattr(self.definition, "enabled", True))

    def available_in(self, node_id: Any) -> bool:
        """转发 ``ActionDef.available_in``；坏定义 / 坏输入一律 ``False``。"""

        checker = getattr(self.definition, "available_in", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(str(node_id or "")))
        except Exception:
            return False


@dataclass(frozen=True)
class ActionConflict:
    """同一个动作 id 出现了多份定义时的**可解释**冲突记录。

    设计口径是「遇到歧义应提示冲突，不能随便选一个执行」（设计 §3）。所以这里一定带
    ``winner``（确定性赢家，用来对齐运行时行为）与 ``losers``（被挡住的那些，必须在
    编辑器 / 日志里看得见），绝不做「静默丢弃」。
    """

    action_id: str
    kind: str
    severity: str
    winner: tuple[str, str]
    losers: tuple[tuple[str, str], ...]
    detail: str

    @property
    def identities(self) -> tuple[tuple[str, str], ...]:
        """全部相关身份，赢家排第一。"""

        return (self.winner, *self.losers)

    @property
    def hard_block(self) -> bool:
        """错误级冲突：**无论 strict 都不许**按裸 id 执行。"""

        return self.severity == SEVERITY_ERROR

    def line(self) -> str:
        """一行中文提示（编辑器 / 日志 / 状态接口直接显示它）。"""

        winner = identity_text(*self.winner)
        losers = "、".join(identity_text(*item) for item in self.losers) or "（无）"
        return (
            f"[{self.severity}] 动作 id「{self.action_id}」："
            f"生效 {winner}（{source_label(self.winner[0])}），"
            f"被挡住 {losers}。{self.detail}"
        )


@dataclass(frozen=True)
class LegacyResolution:
    """把一个**裸 id**（老数据里到处都是）解析成确定身份的结果。

    上层怎么知道「能不能直接执行」：看 :attr:`status` 与 :attr:`executable`，
    出错原因看 :attr:`reason`，被挡住的那些看 :attr:`conflicts`。
    """

    action_id: str
    status: str
    executable: bool
    identity: tuple[str, str] | None
    definition: Any
    candidates: tuple[tuple[str, str], ...] = ()
    conflicts: tuple[ActionConflict, ...] = ()
    strict: bool = True
    reason: str = ""
    overridden: bool = False
    """生效的那份盖住了别人（例如用户配置盖住内置）——升级时要保留的就是这一份。"""

    @property
    def ok(self) -> bool:
        """``status == "found"`` 的别名（读起来顺一点）。"""

        return self.status == STATUS_FOUND

    @property
    def identity_label(self) -> str:
        return identity_text(*self.identity) if self.identity else ""

    @property
    def missing(self) -> bool:
        return self.status == STATUS_MISSING


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class ActionRegistry:
    """动作注册表的只读快照：默认 / 用户 / 扩展三层 + 有效覆盖后的那一份。

    构造用 :func:`build_registry`（本类不接受裸参数，免得把视图口径散到两处）。
    快照里的 ``definition`` 对象是**活的**（引擎手里的 ``ActionDef`` 就是它们），
    所以改了定义要重新 ``build_registry`` 才能看到新的版本号。
    """

    def __init__(
        self,
        entries: Sequence[ActionEntry],
        *,
        conflicts: Sequence[ActionConflict] = (),
        notes: Sequence[str] = (),
        revisions: dict[str, str] | None = None,
    ) -> None:
        self.entries: tuple[ActionEntry, ...] = tuple(entries)
        self.conflicts: tuple[ActionConflict, ...] = tuple(conflicts)
        self.notes: tuple[str, ...] = tuple(str(item) for item in notes)
        self.revisions: dict[str, str] = dict(revisions or {})
        """``{层名: 来源版本}``——``core`` / ``world`` / ``ext:<名>``。"""

        ext_order: dict[str, int] = {}
        for entry in self.entries:
            if entry.namespace not in ext_order:
                ext_order[entry.namespace] = len(ext_order)

        def rank(entry: ActionEntry) -> tuple[int, int, int]:
            if entry.active:
                return (0, 0, 0)
            if entry.namespace == NS_WORLD:
                return (1, 0, 0)
            if entry.namespace == NS_CORE:
                return (1, 1, 0)
            return (1, 2, ext_order.get(entry.namespace, 10_000))

        by_identity: dict[tuple[str, str], ActionEntry] = {}
        by_action: dict[str, list[ActionEntry]] = {}
        for entry in self.entries:
            by_identity.setdefault(entry.identity, entry)
            by_action.setdefault(entry.action_id, []).append(entry)
        for items in by_action.values():
            items.sort(key=rank)
        self._by_identity = by_identity
        self._by_action = by_action
        self._effective: dict[str, ActionEntry] = {
            entry.action_id: entry for entry in self.entries if entry.active
        }
        self._conflicts_by_action: dict[str, list[ActionConflict]] = {}
        for conflict in self.conflicts:
            self._conflicts_by_action.setdefault(conflict.action_id, []).append(conflict)
        self.revision: str = self._compute_index_revision()

    # -- 基本读取 ---------------------------------------------------------

    def __len__(self) -> int:
        """有效动作的条数（不是三层加起来的条数）。"""

        return len(self._effective)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __contains__(self, action_id: Any) -> bool:
        return clean_identifier(action_id) in self._effective

    @property
    def effective(self) -> dict[str, ActionEntry]:
        """``{裸 id: 生效的那一条}``（**当前世界真正能用的那些**）。"""

        return dict(self._effective)

    def effective_ids(self) -> list[str]:
        """生效的裸 id，升序。"""

        return sorted(self._effective)

    def effective_map(self) -> dict[str, Any]:
        """``{裸 id: 生效的动作定义}``——引擎侧要的就是这一份。"""

        return {key: entry.definition for key, entry in self._effective.items()}

    def get(self, namespace: Any, action_id: Any) -> ActionEntry | None:
        """按**完整身份**取一条；没有回 ``None``。"""

        return self._by_identity.get(make_identity(namespace, action_id))

    def candidates(self, action_id: Any) -> tuple[ActionEntry, ...]:
        """这个裸 id 的全部候选（生效的排第一，其余按 world → core → 扩展注册顺序）。"""

        return tuple(self._by_action.get(clean_identifier(action_id)) or [])

    def effective_entry(self, action_id: Any) -> ActionEntry | None:
        """这个裸 id 生效的那一条（``None`` = 当前世界里没有这个动作）。"""

        return self._effective.get(clean_identifier(action_id))

    def namespace_entries(self, namespace: Any) -> tuple[ActionEntry, ...]:
        """某一层的全部条目（``"core"`` / ``"world"`` / ``"ext:xxx"``）。"""

        wanted = str(namespace or "").strip()
        return tuple(entry for entry in self.entries if entry.namespace == wanted)

    def entries_with_action(self, action_id: Any) -> tuple[ActionEntry, ...]:
        """:meth:`candidates` 的同义入口（按 id 取全部来源）。"""

        return self.candidates(action_id)

    def conflicts_for(self, action_id: Any) -> tuple[ActionConflict, ...]:
        """某个 id 上的冲突。"""

        return tuple(self._conflicts_by_action.get(clean_identifier(action_id)) or ())

    def conflict_report(self) -> list[str]:
        """全部冲突渲染成中文行（编辑器 / 日志 / 状态接口直接显示）。"""

        return [item.line() for item in self.conflicts]

    # -- 旧 id 映射 -------------------------------------------------------

    def resolve(self, action_id: Any, *, strict: bool = True) -> LegacyResolution:
        """**裸 id → 确定身份**的映射（老数据只有裸 id 时的唯一入口）。

        规则（确定性、与运行时行为对齐）：

        1. 一个 id 只有一份**生效**候选、又没有警告级以上的冲突 → ``found``，可执行；
        2. 用户配置盖住内置（``core::id`` + ``world::id``）→ 生效的是 **world**；
           这是设计内的覆盖，报 ``info`` 级冲突，仍是 ``found`` + ``executable=True``，
           ``overridden=True`` 告诉上层「升级时别动这一份」；
        3. 扩展定义被同名内置 / 用户配置挡住 → ``warning`` 级冲突、``status='conflict'``。
           **默认（``strict=True``）不给执行**：裸 id 分不清用户想调的是哪一份，硬选一个
           就是「静默误执行」。调用方要么改用完整身份（:meth:`get`），要么显式传
           ``strict=False`` 表示「我接受与运行时一致的确定性赢家」（那时
           ``executable=True``，``identity`` 仍是确定性赢家）；
        4. 两个扩展撞名 → ``error`` 级，**无论 strict 都不给执行**（后一个在运行时
           本来就永远不可达）；
        5. 没有任何**生效**候选（包括「内置里有、但当前世界配置删掉了」）→ ``missing``。

        所以：``status`` 说的是「这个裸 id 歧义不歧义」，``executable`` 说的是「这次调用
        允不允许直接执行」，``conflicts`` 永远带着被挡住的那些身份。
        认不出（空 id / ``None`` / 只剩不可见字符）等同 ``missing``，不抛异常。
        """

        key = clean_identifier(action_id)
        if not key:
            return LegacyResolution(
                action_id="",
                status=STATUS_MISSING,
                executable=False,
                identity=None,
                definition=None,
                strict=strict,
                reason="动作 id 是空的，没法映射到任何一份定义。",
            )
        items = self.candidates(key)
        conflicts = self.conflicts_for(key)
        active = [entry for entry in items if entry.active]
        if not items:
            return LegacyResolution(
                action_id=key,
                status=STATUS_MISSING,
                executable=False,
                identity=None,
                definition=None,
                strict=strict,
                reason=f"注册表里没有「{key}」这个动作。",
            )
        if not active:
            return LegacyResolution(
                action_id=key,
                status=STATUS_MISSING,
                executable=False,
                identity=None,
                definition=None,
                candidates=tuple(entry.identity for entry in items),
                conflicts=conflicts,
                strict=strict,
                reason=(
                    f"「{key}」在当前世界配置里没有生效的定义（候选："
                    f"{'、'.join(identity_text(*entry.identity) for entry in items)}）。"
                ),
            )
        winner = active[0]
        severe = [
            item
            for item in conflicts
            if item.severity in (SEVERITY_WARNING, SEVERITY_ERROR)
        ]
        status = STATUS_FOUND if (len(active) == 1 and not severe) else STATUS_CONFLICT
        executable = status == STATUS_FOUND or (
            status == STATUS_CONFLICT
            and not strict
            and not any(item.hard_block for item in conflicts)
        )
        if status == STATUS_FOUND:
            reason = f"只有一份生效定义：{winner.identity_text}（{source_label(winner.namespace)}）。"
            if conflicts:
                reason += f"另有 {len(conflicts)} 条信息级提示，见 conflicts。"
        else:
            others = "、".join(
                identity_text(*entry.identity) for entry in items if entry is not winner
            )
            reason = f"「{key}」有 {len(items)} 份定义（生效 {winner.identity_text}；另有 {others}）。"
            reason += (
                "按裸 id 直接执行会有歧义，请用完整身份，或显式传 strict=False 接受确定性赢家。"
                if not executable
                else "已按运行时的确定性赢家放行（strict=False）。"
            )
        return LegacyResolution(
            action_id=key,
            status=status,
            executable=executable,
            identity=winner.identity,
            definition=winner.definition,
            candidates=tuple(entry.identity for entry in items),
            conflicts=conflicts,
            strict=strict,
            reason=reason,
            overridden=winner.supersedes is not None,
        )

    def resolve_label(self, text: Any, *, strict: bool = True) -> LegacyResolution:
        """按**显示名**（例如「分享」）或裸 id 解析；名字不唯一一律不给执行。

        ⚠️ 这里**不查** ``core/aliases.py`` 那两张中文表：那是口吻 / 贡献类型的域，
        值恰好和动作 id 同名（``hug`` / ``share``），拿它们查动作就是设计文档 §11.6
        点名的「同形不同义」坑。这里只认 ``ActionDef.name`` 的精确匹配。
        """

        key = clean_identifier(text)
        if not key:
            return LegacyResolution(
                action_id="",
                status=STATUS_MISSING,
                executable=False,
                identity=None,
                definition=None,
                strict=strict,
                reason="名字是空的。",
            )
        direct = self.resolve(key, strict=strict)
        if not direct.missing:
            return direct
        matched = sorted(
            {
                entry.action_id
                for entry in self.entries
                if str(getattr(entry.definition, "name", "") or "").strip() == key
            }
        )
        if not matched:
            return LegacyResolution(
                action_id=key,
                status=STATUS_MISSING,
                executable=False,
                identity=None,
                definition=None,
                strict=strict,
                reason=f"注册表里既没有 id 也没有显示名叫「{key}」的动作。",
            )
        if len(matched) > 1:
            return LegacyResolution(
                action_id=key,
                status=STATUS_CONFLICT,
                executable=False,
                identity=None,
                definition=None,
                strict=strict,
                reason=(
                    f"显示名「{key}」对应多个动作（{'、'.join(matched)}），"
                    "按名字解析会有歧义，请用 id 或完整身份。"
                ),
            )
        return self.resolve(matched[0], strict=strict)

    # -- 版本 -------------------------------------------------------------

    def _compute_index_revision(self) -> str:
        """索引版本：全部**生效**定义的身份 + 有效定义版本，排序后取 sha256。"""

        rows = [
            f"{entry.identity_text}\t{entry.effective_revision}"
            for entry in self._effective.values()
        ]
        rows.sort()
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:REVISION_CHARS]

    @property
    def index_revision(self) -> str:
        """:meth:`_compute_index_revision` 的只读别名（派生视图的缓存键就是它）。"""

        return self.revision

    def user_overrides(self) -> dict[str, Any]:
        """``{裸 id: 用户那份定义}``——**盖住了内置默认**的那些动作。

        A5 的「升级保留用户覆盖」就是拿这张表判的：重新读一遍默认动作时，
        id 在这张表里的一律**不动**（用户改过的名字 / 文案 / 门槛都留着）。
        """

        picked: dict[str, Any] = {}
        for entry in self.entries:
            if entry.active and entry.origin == ORIGIN_USER and entry.supersedes is not None:
                picked[entry.action_id] = entry.definition
        return picked

    def detailed(self) -> list[dict[str, Any]]:
        """给编辑器 / 调试输出用的一行行摘要（纯数据，不带定义对象）。"""

        return [
            {
                "identity": entry.identity_text,
                "namespace": entry.namespace,
                "action_id": entry.action_id,
                "name": entry.name,
                "origin": entry.origin,
                "active": entry.active,
                "shadowed_by": identity_text(*entry.shadowed_by) if entry.shadowed_by else "",
                "supersedes": identity_text(*entry.supersedes) if entry.supersedes else "",
                "source_revision": entry.source_revision,
                "effective_revision": entry.effective_revision,
                "extension": entry.extension,
            }
            for entry in self.entries
        ]


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------


def build_registry(
    world: Any = None,
    *,
    extension_actions: Iterable[tuple[str, dict]] = (),
    extension_version_map: dict[str, str] | None = None,
    defaults: Any = None,
    notes: Sequence[str] = (),
) -> ActionRegistry:
    """把三层定义合成一份只读视图。

    参数
    ----
    * ``world``：``WorldConfig``、``{"actions": [...]}`` 这样的 dict、或者干脆 ``None``。
      认不出就当成「没有世界动作」（视图里只剩内置层与扩展层），不会抛。
    * ``extension_actions``：``(扩展名, 动作定义 dict)`` 的序列，**顺序即运行时优先级**
      （第一个撞名的赢）。用 :func:`extension_action_pairs` 从 ``ExtensionHost`` 里取。
    * ``extension_version_map``：``{扩展命名空间: 扩展自报 version}``（:func:`extension_versions`
      取得到就传进来，进「来源版本」的前缀）。
    * ``defaults``：内置默认动作的原始列表；不给就读 ``DEFAULT_WORLD["actions"]``。
    * ``notes``：调用方想一并带在视图上的提示（例如 ``ConfigStore.warnings``）。

    判层规则（「这一份到底属于谁」）
    ------------------------------

    * 世界配置里的某条动作与**同 id 的内置默认**权威字段逐字节一致 → 它就是内置那一份
      （身份 ``core::<id>``，不额外造 world 条目）；
    * 不一致 → 内置那条以 ``core`` 身份保留但标 ``active=False`` + ``shadowed_by``，
      用户那份以 ``world::<id>`` 生效，并记 :data:`CONFLICT_SHADOW`（信息级）；
    * 内置有、世界配置里没有 → 保留 ``core`` 条目并标 ``active=False``（用户删掉了），
      只记一条 note：**这条身份不在场，解析它会得到 missing**；
    * 扩展定义与生效 id 撞名 → ``active=False`` + 冲突（警告级）；
      两个扩展之间撞名 → 后一个 ``active=False`` + 冲突（错误级）。

    一个已知的保守偏差：``parse_world`` 会把「还等于**老**内置值」的字段跟着
    ``BUILTIN_ACTION_UPDATES`` 升级（例如 ``pour_tea``），于是用户其实没动过、
    但内容与**当前**默认不同 → 这里会报一条信息级 shadow。方向是安全的
    （多一条提示，不会误执行），真机里通常只影响极少数动作。
    """

    result_notes: list[str] = [str(item).strip() for item in (notes or ()) if str(item).strip()]

    default_defs: dict[str, Any] = {}
    for action_id, payload in default_action_payloads(defaults).items():
        definition = _coerce_definition(payload)
        if definition is None:
            result_notes.append(f"内置默认动作「{action_id}」校验不过，已从视图里忽略")
            continue
        default_defs[_definition_id(definition) or action_id] = definition

    world_defs: dict[str, Any] = {}
    for item in _iter_world_actions(world, result_notes):
        definition = _coerce_definition(item)
        action_id = _definition_id(definition) if definition is not None else ""
        if definition is None or not action_id:
            result_notes.append("世界配置里有一条动作没有可用 id 或校验不过，已从视图里忽略")
            continue
        world_defs[action_id] = definition

    core_entries: list[ActionEntry] = []
    world_entries: list[ActionEntry] = []
    conflicts: list[ActionConflict] = []

    for action_id, definition in default_defs.items():
        override = world_defs.get(action_id)
        if override is None:
            # 默认里有、当前世界配置里没有：用户把它删了。它不在场（解析 → missing），
            # 但视图里仍然留着——A5 的「缺失内置补回」与升级对比要看它。
            core_entries.append(
                _entry(NS_CORE, action_id, definition, ORIGIN_DEFAULT, active=False)
            )
            result_notes.append(
                f"内置默认动作「{action_id}」当前不在世界配置里（被删掉了）：不可用，视图里只作对照"
            )
            continue
        if definition_payload(override) == definition_payload(definition):
            # 权威字段与默认逐字节一致 → 这条**就是**内置那份（身份仍是 core、来源仍是默认）。
            # 但交出来的**定义用用户配置里那一份**：派生字段（`discovery`）只有它带着，
            # 用默认对象交出去会让"后台刚整理好的简述"在视图里看不见（A6 接线时踩过，
            # 只能在引擎侧绕）。判等去派生字段、交出来用用户那份，两件事互不矛盾。
            core_entries.append(
                _entry(NS_CORE, action_id, override, ORIGIN_DEFAULT, active=True)
            )
            continue
        core_entries.append(
            _entry(
                NS_CORE,
                action_id,
                definition,
                ORIGIN_DEFAULT,
                active=False,
                shadowed_by=(NS_WORLD, action_id),
            )
        )
        world_entries.append(
            _entry(
                NS_WORLD,
                action_id,
                override,
                ORIGIN_USER,
                active=True,
                supersedes=(NS_CORE, action_id),
            )
        )
        conflicts.append(
            ActionConflict(
                action_id=action_id,
                kind=CONFLICT_SHADOW,
                severity=SEVERITY_INFO,
                winner=(NS_WORLD, action_id),
                losers=((NS_CORE, action_id),),
                detail="用户配置盖住了同名内置默认；升级内置时保留用户这一份。",
            )
        )

    for action_id, definition in world_defs.items():
        if action_id in default_defs:
            continue
        world_entries.append(_entry(NS_WORLD, action_id, definition, ORIGIN_USER, active=True))

    # 扩展层：顺序即优先级，撞名的一律 active=False 并记冲突（运行时就是「已存在的赢」）。
    ext_entries: list[ActionEntry] = []
    ext_seen: dict[str, str] = {}
    taken: dict[str, tuple[str, str]] = {}
    for item in (*core_entries, *world_entries):
        if item.active:
            taken[item.action_id] = item.identity
    for name, payload in extension_actions or ():
        namespace = ext_namespace(name)
        if not namespace:
            result_notes.append("有一个扩展没有可用名字，它带来的动作已忽略（说不清是谁给的）")
            continue
        definition = _coerce_definition(payload)
        action_id = _definition_id(definition) if definition is not None else ""
        if definition is None or not action_id:
            result_notes.append(f"扩展「{name}」有一条动作定义校验不过或没有 id，已忽略")
            continue
        existing = taken.get(action_id)
        if existing is not None:
            ext_entries.append(
                _entry(
                    namespace,
                    action_id,
                    definition,
                    ORIGIN_EXTENSION,
                    active=False,
                    shadowed_by=existing,
                    extension=extension_name_of(namespace),
                )
            )
            conflicts.append(
                ActionConflict(
                    action_id=action_id,
                    kind=CONFLICT_EXT_SHADOWED,
                    severity=SEVERITY_WARNING,
                    winner=existing,
                    losers=((namespace, action_id),),
                    detail=(
                        f"扩展「{name}」的这个动作被同名的{source_label(existing[0])}挡住，"
                        "运行时不会执行；要么改扩展里的 id，要么用完整身份显式指定。"
                    ),
                )
            )
            continue
        holder = ext_seen.get(action_id)
        if holder is not None:
            ext_entries.append(
                _entry(
                    namespace,
                    action_id,
                    definition,
                    ORIGIN_EXTENSION,
                    active=False,
                    shadowed_by=(holder, action_id),
                    extension=extension_name_of(namespace),
                )
            )
            conflicts.append(
                ActionConflict(
                    action_id=action_id,
                    kind=CONFLICT_EXT_DUPLICATE,
                    severity=SEVERITY_ERROR,
                    winner=(holder, action_id),
                    losers=((namespace, action_id),),
                    detail=(
                        f"扩展「{name}」与扩展「{extension_name_of(holder)}」注册了同一个 id；"
                        "运行时后者永远不可达，请改名或删掉一份。"
                    ),
                )
            )
            continue
        ext_seen[action_id] = namespace
        ext_entries.append(
            _entry(
                namespace,
                action_id,
                definition,
                ORIGIN_EXTENSION,
                active=True,
                extension=extension_name_of(namespace),
            )
        )

    all_entries = [*core_entries, *world_entries, *ext_entries]
    hints = dict(extension_version_map or {})
    revisions: dict[str, str] = {}
    for namespace in _layers_of(all_entries):
        definitions = [item.definition for item in all_entries if item.namespace == namespace]
        version = CORE_SOURCE_VERSION if namespace == NS_CORE else hints.get(namespace, "")
        value = layer_revision(namespace, definitions, version=version)
        if value:
            revisions[namespace] = value

    return ActionRegistry(
        _stamp_revisions(all_entries, revisions),
        conflicts=conflicts,
        notes=result_notes,
        revisions=revisions,
    )


def build_registry_from_host(
    world: Any = None,
    host: Any = None,
    *,
    defaults: Any = None,
    notes: Sequence[str] = (),
) -> ActionRegistry:
    """从 ``ExtensionHost`` 直接建视图（保持注册顺序 + 带上扩展自报版本）。"""

    return build_registry(
        world,
        extension_actions=extension_action_pairs(host),
        extension_version_map=extension_versions(host),
        defaults=defaults,
        notes=notes,
    )


def resolve_action(
    source: Any,
    action_id: Any,
    *,
    strict: bool = True,
    extension_actions: Iterable[tuple[str, dict]] = (),
    defaults: Any = None,
) -> LegacyResolution:
    """一行便利入口：``source`` 是 :class:`ActionRegistry` 就直接解析，是 world 就先建表。"""

    registry = (
        source
        if isinstance(source, ActionRegistry)
        else build_registry(source, extension_actions=extension_actions, defaults=defaults)
    )
    return registry.resolve(action_id, strict=strict)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _entry(
    namespace: str,
    action_id: str,
    definition: Any,
    origin: str,
    *,
    active: bool,
    shadowed_by: tuple[str, str] | None = None,
    supersedes: tuple[str, str] | None = None,
    extension: str = "",
) -> ActionEntry:
    """造一条还没回填版本号的条目（``source_revision`` / ``effective_revision`` 后面统一填）。"""

    return ActionEntry(
        namespace=namespace,
        action_id=action_id,
        definition=definition,
        origin=origin,
        source_revision="",
        effective_revision="",
        active=active,
        shadowed_by=shadowed_by,
        supersedes=supersedes,
        extension=extension,
    )


def _definition_id(definition: Any) -> str:
    """一条定义的裸 id（清洗过；认不出回空串）。"""

    if isinstance(definition, dict):
        raw = definition.get("id")
    else:
        raw = getattr(definition, "id", "")
    return clean_identifier(raw)


def _coerce_definition(payload: Any) -> Any:
    """把 dict 校成 ``ActionDef``；已经是 ``ActionDef`` 的原样返回；坏输入回 ``None``。"""

    if isinstance(payload, ActionDef):
        return payload
    if isinstance(payload, dict):
        try:
            return ActionDef.model_validate(dict(payload))
        except Exception:
            return None
    return None


def _iter_definitions(definitions: Any) -> list[Any]:
    """归一成「一串定义」：dict 取 values、单个定义包一层、其余丢弃。"""

    if definitions is None:
        return []
    if isinstance(definitions, dict):
        return [item for item in definitions.values() if item is not None]
    if isinstance(definitions, (list, tuple, set)):
        return [item for item in definitions if item is not None]
    if isinstance(definitions, ActionDef):
        return [definitions]
    return []


def _iter_world_actions(world: Any, notes: list[str]) -> list[Any]:
    """从 world（配置对象 / dict / 裸列表）里取出动作定义列表；坏输入记 note 后回空表。"""

    if world is None:
        return []
    if isinstance(world, dict):
        raw: Any = world.get("actions")
    elif isinstance(world, (list, tuple)):
        raw = world
    else:
        if not hasattr(world, "actions"):
            notes.append("传进来的 world 既不是配置对象也不是动作列表，视图按「没有世界动作」处理")
            return []
        raw = getattr(world, "actions", None)
    if isinstance(raw, dict):
        raw = list(raw.values())
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        notes.append("world.actions 不是列表，视图按「没有世界动作」处理")
        return []
    return list(raw)


def _layers_of(entries: Sequence[ActionEntry]) -> list[str]:
    """条目里出现过的层名，保持首次出现顺序（core → world → 扩展注册顺序）。"""

    order: list[str] = []
    for entry in entries:
        if entry.namespace not in order:
            order.append(entry.namespace)
    return order


def _stamp_revisions(
    entries: Sequence[ActionEntry], revisions: dict[str, str]
) -> list[ActionEntry]:
    """回填 ``source_revision`` / ``effective_revision``（要等全部条目定下来才能算）。

    * ``source_revision`` = 这条定义**所在层**的来源版本；
    * ``effective_revision`` = 这个 id **生效那一份**的有效定义版本——被盖住的条目也填
      赢家的版本，这样上层一眼能看出「你手里这条不是真正在跑的那条」。
    """

    effective: dict[str, str] = {}
    for entry in entries:
        if entry.active:
            effective[entry.action_id] = definition_revision(entry.definition)
    return [
        ActionEntry(
            namespace=entry.namespace,
            action_id=entry.action_id,
            definition=entry.definition,
            origin=entry.origin,
            source_revision=revisions.get(entry.namespace, ""),
            effective_revision=effective.get(entry.action_id, ""),
            active=entry.active,
            shadowed_by=entry.shadowed_by,
            supersedes=entry.supersedes,
            extension=entry.extension,
        )
        for entry in entries
    ]


__all__ = [
    "ActionConflict",
    "ActionEntry",
    "ActionRegistry",
    "CONFLICT_EXT_DUPLICATE",
    "CONFLICT_EXT_SHADOWED",
    "CONFLICT_SHADOW",
    "CORE_SOURCE_VERSION",
    "DERIVED_CONTAINER_FIELDS",
    "IDENTITY_SEPARATOR",
    "LegacyResolution",
    "NS_CORE",
    "NS_EXT_PREFIX",
    "NS_WORLD",
    "ORIGIN_DEFAULT",
    "ORIGIN_EXTENSION",
    "ORIGIN_USER",
    "REVISION_CHARS",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SOURCE_CORE",
    "SOURCE_EXTENSION",
    "SOURCE_WORLD",
    "STATUS_CONFLICT",
    "STATUS_FOUND",
    "STATUS_MISSING",
    "build_registry",
    "build_registry_from_host",
    "canonical_json",
    "default_action_payloads",
    "definition_payload",
    "definition_revision",
    "ext_namespace",
    "extension_action_pairs",
    "extension_name_of",
    "extension_versions",
    "identity_text",
    "is_extension_namespace",
    "layer_revision",
    "legacy_action_map",
    "make_identity",
    "parse_identity_text",
    "resolve_action",
    "source_kind",
    "source_label",
]
