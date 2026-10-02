"""扩展挂载点。

主插件只说"这里可以挂东西"，不认识任何一个具体扩展：

- 装了扩展包 → 它注册的动作、数值、提示词层、门控、设置页一起生效；
- 没装 → 一切都是空的，插件行为与从前完全一样。

扩展包是一个独立的 AstrBot 插件，它自己找到插件实例（``star_cls.extension_host``）
再调 :meth:`ExtensionHost.register`。这样主仓库里不会留下任何扩展的痕迹，
扩展怎么长、什么时候停用都由装上它的那个人决定。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SETTINGS_FILE = "extensions.json"
"""扩展自己的配置存在数据目录的这个文件里（不写进世界配置 / 预设）。"""

ADJUSTABLE = ("energy", "loneliness", "curiosity", "affect", "valence", "boredom")
"""``adjust`` 能推的世界数值，取值都归一到 0~1。"""

MEMORY_KINDS = ("scene", "relation", "event", "inner", "interaction")
"""``remember`` 能写的记忆类型（和插件自己的记忆层一致）。"""

MODEL_SLOTS = ("helper", "creator", "llm", "event", "judge", "consolidate")
"""扩展写文字时可以点名的模型档位（对应主插件配置里那几栏）。

``helper`` 打杂 / ``creator`` 内容生成（编事件包那个） / ``llm`` 主模型 /
``event`` 事件 / ``judge`` 判断 / ``consolidate`` 整理。
"""


JSON_FIELD_KINDS = ("list", "str", "text", "num", "bool")
"""扩展声明 JSON 字段时能选的形状。"""

JSON_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
"""字段名规则：小写字母开头，只含小写字母 / 数字 / 下划线，最长 24 个字符。"""

MAX_JSON_FIELDS = 4
"""最多给扩展开几个字段：再多说明该合并成一个了，免得把输出格式那块撑爆。"""

MAX_JSON_FIELD_PROMPT = 140
"""每个字段写进提示词的说明最多多少个字（超了截断）。"""

MAX_JSON_FIELD_ITEMS = 5
"""列表型字段最多收几项。"""

MAX_JSON_FIELD_CHARS = 24
"""列表型字段每一项最多几个字。"""

MAX_JSON_FIELD_TEXT = 1200
"""长文本字段（``kind="text"``）最多多少字。"""

RESERVED_JSON_KEYS = frozenset(
    {
        "reasoning",
        "memory",
        "chat_note",
        "open_topic",
        "heart_knot",
        "grudge",
        "forgive",
        "own_topic",
        "own_topic_done",
        "tone",
        "addressing",
        "touch",
        "valence_delta",
        "affinity_delta",
        "actions",
        "cancel",
        "plan_mode",
    }
)
"""主插件自己那套键名：扩展不能占（占了就是跟提示词协议打架）。"""


@dataclass(frozen=True)
class JsonField:
    """扩展要主模型额外标出来的一个字段。

    扩展在 :attr:`ExtensionSpec.json_fields` 里声明，主插件负责三件事：

    - 把说明写进提示词的「输出格式」那一段（**静态前缀**，不随状态变化，
      所以不会破坏提示词缓存）；
    - 按声明的形状把模型给的值解析干净（截断、去重、限长）；
    - 把结果原样转发给声明它的扩展（``ExtensionSpec.on_json``）。
    """

    name: str
    kind: str = "list"
    prompt: str = ""
    """写进提示词的那句说明（例如「他这一轮推进到哪一步」）。"""

    empty: str = ""
    """什么时候留空（可选），会跟在说明后面用括号带出来。"""

    max_items: int = MAX_JSON_FIELD_ITEMS
    max_chars: int = 12

    example: str = ""
    """只是给扩展作者看的示例，不写进提示词。"""

    def render(self) -> str:
        """拼成提示词里的那一行（写法与主插件自己那几个字段一致）。"""

        note = " ".join(str(self.prompt or "").split())[:MAX_JSON_FIELD_PROMPT]
        blank = " ".join(str(self.empty or "").split())
        if blank:
            note = f"{note}（{blank}）" if note else f"（{blank}）"
        if self.kind == "list":
            return f'  "{self.name}": [{note}]\n'
        if self.kind == "bool":
            return f'  "{self.name}": false,{"（" + note + "）" if note else ""}\n'
        if self.kind == "num":
            return f'  "{self.name}": 0,{"（" + note + "）" if note else ""}\n'
        if self.kind == "text":
            # 长文本：照字符串的写法给，但允许换行（发出去的时候换行照原样留着）
            return f'  "{self.name}": "{note}",\n'
        return f'  "{self.name}": "{note}",\n'


BUILTIN_JSON_FIELDS: dict[str, JsonField] = {
    "touch": JsonField(
        name="touch",
        kind="list",
        prompt="他这一轮碰到你身上哪些地方，写名词就行（例如 腰、耳后、手）",
        empty="他没碰到你、或者只是你单方面在做动作，就留空数组",
        max_items=5,
        max_chars=12,
    ),
}
"""主插件自己认得的那几个字段：扩展只能"声明要它"，形状由这里定。"""


def _keep_paragraphs(text: Any) -> str:
    """收拾一段要发出去的文字：**保留换行**，只去掉多余空行与行尾空格。

    扩展写的长段叙述靠空行分段；以前这里用 ``" ".join(text.split())`` 把换行全吃掉，
    发到聊天里就是一大坨没有排版的字。
    """

    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    lines = [line.rstrip() for line in raw.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
            continue
        blank += 1
        if blank <= 1:
            out.append("")
    return "\n".join(out).strip()


def normalize_json_field(raw: Any) -> JsonField | None:
    """把扩展给的字段声明收拾成 :class:`JsonField`；认不出来就返回 ``None``。

    坏声明一律安静丢掉：扩展写错了不该把主插件的提示词搞坏。
    """

    if isinstance(raw, JsonField):
        data: dict[str, Any] = {
            "name": raw.name,
            "kind": raw.kind,
            "prompt": raw.prompt,
            "empty": raw.empty,
            "max_items": raw.max_items,
            "max_chars": raw.max_chars,
        }
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        return None
    name = " ".join(str(data.get("name") or "").split())
    if not JSON_FIELD_NAME_RE.match(name) or name in RESERVED_JSON_KEYS:
        return None
    kind = str(data.get("kind") or "list").strip().lower()
    if kind not in JSON_FIELD_KINDS:
        kind = "list"
    upper_chars = MAX_JSON_FIELD_TEXT if kind == "text" else MAX_JSON_FIELD_CHARS
    try:
        max_items = int(data.get("max_items") or MAX_JSON_FIELD_ITEMS)
    except (TypeError, ValueError):
        max_items = MAX_JSON_FIELD_ITEMS
    try:
        max_chars = int(data.get("max_chars") or upper_chars)
    except (TypeError, ValueError):
        max_chars = upper_chars
    return JsonField(
        name=name,
        kind=kind,
        prompt=str(data.get("prompt") or "")[:MAX_JSON_FIELD_PROMPT],
        empty=str(data.get("empty") or ""),
        max_items=max(1, min(MAX_JSON_FIELD_ITEMS, max_items)),
        max_chars=max(1, min(upper_chars, max_chars)),
        example=str(data.get("example") or ""),
    )


@dataclass
class ExtensionSpec:
    """一个扩展：它能挂上来的东西。字段都可选。"""

    name: str
    """扩展的唯一标识（也是它在状态里的桶名）。"""

    title: str = ""
    """设置页上显示的名字。"""

    version: str = ""

    actions: list[dict] = field(default_factory=list)
    """要加进动作库的动作定义（跑一遍 ``ActionDef`` 校验，坏的就丢掉）。"""

    settings: list[dict] = field(default_factory=list)
    """设置页里的字段：``{"key","label","kind","hint","default","choices"}``。"""

    on_tick: Callable[[Any, Any, float, "ExtensionHost"], None] | None = None
    """每拍推进一次：``(state, world, now, host)``。用来长数值、结算状态。"""

    prompt_layer: Callable[[Any, str, "ExtensionHost"], str] | None = None
    """往提示词里加一段：``(state, session_id, host) -> str``。空串表示不加。"""

    gate: Callable[[Any, Any, str, "ExtensionHost"], str] | None = None
    """能不能做这件事：``(definition, state, session_id, host) -> 原因``。

    返回空串 = 允许；返回一句话 = 不让做，那句话会写进日志（"这个动作在这儿做不合适"）。
    """

    panel: Callable[[Any, "ExtensionHost"], dict] | None = None
    """给设置页 / 面板看的只读数据：``(state, host) -> dict``。"""

    status_text: Callable[[Any, str, "ExtensionHost"], str] | None = None
    """面板上「她现在的状态」显示成什么：``(state, session_id, host) -> str``。

    只影响编辑器里那一行，**不进群名片**——例如亲密扩展在戏里时可以显示
    「兴奋中」，但外人看到的群名片不该跟着变。空串 = 交给主插件自己算。
    """

    on_action: Callable[[Any, Any, str, "ExtensionHost"], str] | None = None
    """某个动作真的开始做了：``(definition, state, session_id, host) -> 想说的话``。

    和 ``gate`` 的区别：gate 是"让不让做"，这个是"做的时候顺手记一笔"。
    返回非空字符串时，那句话会作为她这一轮要说的话排进去。
    """

    hidden_actions: Callable[[Any, str, "ExtensionHost"], set] | None = None
    """这个会话里**不要展示**哪些动作：``(state, session_id, host) -> {动作 id}``。

    只在执行时拦是不够的——动作名字本身出现在"你能写的 type"清单里就是泄漏。
    这里隐藏的动作，提示词不列、解析也不认。
    """

    wants: tuple[str, ...] = ()
    """声明"每一轮我还需要主模型标出这些信息"（目前支持 ``"touch"``）。

    **这是让主插件保持干净的关键**：没人声明时，主插件根本不往提示词里写那一项，
    代码里也不留任何扩展的痕迹；有人声明了才加进去并转发给声明方。
    """

    on_touch: Callable[[Any, list[str], float, "ExtensionHost"], None] | None = None
    """主模型标出"这一轮他碰了她哪儿"时的回调：``(state, parts, scale, host)``。

    ``parts`` 是主模型写的自由文本（例如 ``["腰", "耳后"]``），怎么解释由扩展自己定。
    """

    json_fields: tuple[Any, ...] = ()
    """要主模型额外标出来的字段（见 :class:`JsonField`）：``({"name","kind","prompt"}…)``。

    主插件按声明把说明写进提示词、把值解析干净，再回调 :attr:`on_json`。
    **扩展加一个自己的字段不需要动主插件**；没人声明时提示词里一个字都不多。
    """

    on_json: Callable[[Any, dict[str, Any], "ExtensionHost"], None] | None = None
    """主模型标出了声明的字段时的回调：``(state, values, host)``。

    只有该扩展自己声明过的字段会出现在 ``values`` 里（同名广播给所有声明方）。
    """

    on_reply_extra: Callable[[Any, dict[str, Any], "ExtensionHost"], str] | None = None
    """这一轮要**补在她的话后面**一起发出去的一段文本：``(state, values, host) -> str``。

    ``values`` 和 :attr:`on_json` 收到的是同一份。返回非空字符串时，
    主插件把它当作她的下一条发言发出去（同一个发送批次、排在她那几句之后），
    也照常记进她的聊天留档；不占"一次最多说几条"的额度。
    """

    debug_events: tuple[dict[str, str], ...] = ()
    """扩展会写进事件日志的类型：``({"type": "ext_climax", "label": "高潮结算",
    "icon": "💞", "hint": "…", "render": 可调用对象}, …)``。

    主插件把这份清单交给编辑器：

    - 日志页的「类型」筛选里会出现这些条目（带图标和中文名）；
    - 全局设置里的「调试输出」清单会多出一组，勾上就能把这些事件也发到群里；
    - ``render(detail) -> str`` 可选：自己把这一条渲染成中文（日志和调试输出都用它），
      不写就退回主插件的通用兜底（``类型：key=值，…``，能看但不好读）。
    """

    desire_relief: Callable[..., Any] | None = None
    """这次肢体接触算不算"解渴"：``(definition, state, session_id, host) -> 0~1``。

    返回 1 = 照常满足欲求，0 = 这一下一点都不解渴（只是更想要），中间按比例。
    老写法 ``(state) -> bool`` 也认（``False`` = 0，``True`` = 1）。

    "日常亲昵不解渴、只有性事才解渴"这种口味归扩展管——主插件只管把这一下
    折算成欲求落多少，别在通用插件里写死哪类动作算解渴。
    """
    """这次亲密接触**要不要按"满足欲求"处理**：``(state) -> bool``。

    返回 False 表示"这回不算满足"（例如扩展那边认为"正处在一种持续的状态里，
    这时候只会更想要"）。没扩展声明时主插件照旧按接触满足。
    """

    tone_scale: Callable[[Any, str], float] | None = None
    """这一轮"对方口吻"的情绪脉冲打几折：``(state, tone) -> 倍数``。

    ``tone`` 是主插件判出来的口吻（``praise`` / ``hug`` / ``comfort`` / ``attack`` /
    ``negative``…）。返回 ``None`` = 不表态；返回数字时**乘到这一下的幅度上**
    （夹在 0~1.5 之间，0 = 这一下不产生情绪）。第一个表态的扩展说了算。
    **没扩展声明时脉搏怎么算还是怎么算。**
    """

    reply_guard: Callable[[Any, str, "ExtensionHost"], bool] | None = None
    """这一轮的模型原始输出要不要作废：``(state, raw, host) -> True = 废的``。

    返回 True 时主插件**不解析、不执行、不发送**这一轮，直接重新问一次模型
    （最多 ``REPLY_GUARD_RETRIES`` 次）；问满次数还是废的，这一轮就不出声、
    也不会退回主人格。用来拦"我不能生成这类内容"这种拒答。
    """

    on_command_event: Callable[[Any, str, str, "ExtensionHost"], Any] | None = None
    """用户用 ``/vw event <文本>`` 投递事件时的接管钩子：
    ``(state, text, session_id, host) -> None | 回执字符串``。

    返回 ``None`` = 不接手，主插件按原来的事件流程走；
    返回字符串 = **这次事件由这个扩展接手**（空串表示"接下了，但不用回话"），
    那句话会直接回给用户。所以扩展可以用自己的关键词（例如 ``event h``）起一段
    自己的流程，而不是硬塞进主插件的事件系统。
    """


class ExtensionHost:
    """扩展注册表 + 主插件对外的几个钩子。"""

    def __init__(self, plugin: Any = None) -> None:
        self.plugin = plugin
        self.specs: dict[str, ExtensionSpec] = {}
        self._settings: dict[str, dict[str, Any]] | None = None

    # ---------------- 注册 ----------------

    def register(self, spec: ExtensionSpec) -> ExtensionSpec:
        if not spec.name:
            raise ValueError("扩展必须有 name")
        self.specs[spec.name] = spec
        # 扩展是插件加载之后才挂上来的：重载一次配置，
        # 它带来的动作/数值才会立刻生效（没装扩展时这段什么都不做）
        engine = getattr(self.plugin, "engine", None)
        if engine is not None:
            try:
                engine.reload_config()
            except Exception:
                pass
        return spec

    def unregister(self, name: str) -> None:
        self.specs.pop(str(name or ""), None)

    def names(self) -> list[str]:
        return list(self.specs)

    def installed(self) -> bool:
        return bool(self.specs)

    # ---------------- 配置 ----------------

    def settings_path(self) -> Path | None:
        store = getattr(self.plugin, "store", None)
        data_dir = getattr(store, "data_dir", None)
        if data_dir is None:
            return None
        return Path(data_dir) / SETTINGS_FILE

    def _load_settings(self) -> dict[str, dict[str, Any]]:
        if self._settings is not None:
            return self._settings
        data: dict[str, dict[str, Any]] = {}
        path = self.settings_path()
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            if isinstance(raw, dict):
                data = {
                    str(key): dict(value)
                    for key, value in raw.items()
                    if isinstance(value, dict)
                }
        self._settings = data
        return data

    def settings(self, name: str) -> dict[str, Any]:
        """某个扩展的配置（没存过就用字段默认值）。"""

        saved = dict(self._load_settings().get(str(name)) or {})
        spec = self.specs.get(str(name))
        if spec is None:
            return saved
        for item in spec.settings:
            key = str(item.get("key") or "")
            if key and key not in saved:
                saved[key] = item.get("default")
        return saved

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存设置页提交的内容（只认它自己声明过的字段）。"""

        data = self._load_settings()
        for name, values in dict(payload or {}).items():
            spec = self.specs.get(str(name))
            if spec is None or not isinstance(values, dict):
                continue
            allowed = {str(item.get("key") or "") for item in spec.settings}
            picked = {key: value for key, value in values.items() if key in allowed}
            data[str(name)] = {**(data.get(str(name)) or {}), **picked}
        self._settings = data
        path = self.settings_path()
        if path is not None:
            try:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
        return data

    # ---------------- 钩子 ----------------

    def actions(self) -> list[dict]:
        picked: list[dict] = []
        for spec in self.specs.values():
            picked.extend(dict(item) for item in spec.actions if isinstance(item, dict))
        return picked

    def action_groups(self) -> list[dict[str, Any]]:
        """动作库里"这些动作是哪个扩展带来的"（编辑器按它分组，没装扩展就是空）。"""

        groups: list[dict[str, Any]] = []
        for spec in self.specs.values():
            ids = [
                str(item.get("id") or "")
                for item in spec.actions
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
            if ids:
                groups.append(
                    {"name": spec.name, "title": spec.title or spec.name, "ids": ids}
                )
        return groups

    def owned_actions(self) -> set[str]:
        """所有扩展带来的动作 id（这些动作不写进世界配置）。"""

        picked: set[str] = set()
        for group in self.action_groups():
            picked.update(str(item) for item in group["ids"])
        return picked

    def prompt_text(self, state: Any, session_id: str = "") -> str:
        blocks: list[str] = []
        for spec in self.specs.values():
            hook = spec.prompt_layer
            if hook is None:
                continue
            try:
                text = str(hook(state, session_id, self) or "").strip()
            except Exception:
                continue
            if text:
                blocks.append(text)
        return "\n\n".join(blocks)

    def gate_reason(self, definition: Any, state: Any, session_id: str = "") -> str:
        for spec in self.specs.values():
            hook = spec.gate
            if hook is None:
                continue
            try:
                reason = str(hook(definition, state, session_id, self) or "").strip()
            except Exception:
                continue
            if reason:
                return reason
        return ""

    async def on_tick(self, state: Any, world: Any, now: float) -> list[str]:
        notes: list[str] = []
        for spec in self.specs.values():
            hook = spec.on_tick
            if hook is None:
                continue
            try:
                result = hook(state, world, now, self)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as exc:
                notes.append(f"扩展「{spec.name}」这一拍出错：{exc}")
                continue
            if isinstance(result, str) and result.strip():
                notes.append(result.strip())
            elif isinstance(result, list):
                notes.extend(str(item) for item in result if str(item).strip())
        return notes

    # ---------------- 状态与面板 ----------------

    def bucket(self, state: Any, name: str) -> dict[str, Any]:
        """扩展在状态里的那一格（不存在就建）。"""

        store = getattr(state, "ext_data", None)
        if not isinstance(store, dict):
            store = {}
            try:
                state.ext_data = store
            except Exception:
                return {}
        slot = store.get(str(name))
        if not isinstance(slot, dict):
            slot = {}
            store[str(name)] = slot
        return slot

    def panel(self, state: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec in self.specs.values():
            if spec.panel is None:
                continue
            try:
                out[spec.name] = spec.panel(state, self)
            except Exception:
                out[spec.name] = {}
        return out

    def status_text(self, state: Any, session_id: str = "") -> str:
        """扩展想在编辑器里显示的「她现在的状态」（不进群名片）。"""

        for spec in self.specs.values():
            hook = getattr(spec, "status_text", None)
            if hook is None:
                continue
            try:
                text = str(hook(state, session_id, self) or "").strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    def debug_events(self) -> list[dict[str, str]]:
        """扩展注册的事件类型（日志页的筛选与图标用它）。"""

        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for spec in self.specs.values():
            for item in tuple(getattr(spec, "debug_events", ()) or ()):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("type") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "type": key,
                        "label": str(item.get("label") or key),
                        "icon": str(item.get("icon") or "•"),
                        "hint": str(item.get("hint") or ""),
                        "ext": str(spec.title or spec.name),
                    }
                )
        return rows

    def debug_event_types(self) -> set[str]:
        """扩展注册过的事件类型（「调试输出」的白名单要用）。"""

        return {str(item.get("type") or "") for item in self.debug_events()}

    def debug_event_text(self, kind: str, detail: Any) -> str:
        """让注册了这个类型的扩展自己把事件渲染成一行中文；没人认领返回空串。"""

        key = str(kind or "").strip()
        if not key:
            return ""
        for spec in self.specs.values():
            for item in tuple(getattr(spec, "debug_events", ()) or ()):
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip() != key:
                    continue
                render = item.get("render")
                if not callable(render):
                    continue
                try:
                    text = str(render(dict(detail or {})) or "").strip()
                except Exception:
                    return ""
                if text:
                    return text
        return ""

    def action_note(self, definition: Any, state: Any, session_id: str = "") -> str:
        """某个动作真的做起来了：让扩展记一笔（返回她想顺口说的那句）。"""

        for spec in self.specs.values():
            hook = spec.on_action
            if hook is None:
                continue
            try:
                text = str(hook(definition, state, session_id, self) or "").strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    def hidden_actions(self, state: Any, session_id: str = "") -> set[str]:
        """这个会话里要藏起来的动作 id（扩展自己说了算）。"""

        picked: set[str] = set()
        for spec in self.specs.values():
            hook = spec.hidden_actions
            if hook is None:
                continue
            try:
                values = hook(state, session_id, self) or set()
            except Exception:
                continue
            picked.update(str(item) for item in values if str(item))
        return picked

    # ---------------- 「主模型标出来的东西」 ----------------

    def _declared_fields(self, spec: ExtensionSpec) -> list[JsonField]:
        """某个扩展自己声明的字段（坏声明丢掉）。"""

        picked: list[JsonField] = []
        for raw in tuple(getattr(spec, "json_fields", ()) or ()):
            field_item = normalize_json_field(raw)
            if field_item is not None:
                picked.append(field_item)
        return picked

    def json_fields(self) -> list[JsonField]:
        """所有扩展要主模型额外标出来的字段（**按名字去重，先声明的说了算**）。"""

        picked: dict[str, JsonField] = {}
        for spec in self.specs.values():
            for field_item in self._declared_fields(spec):
                picked.setdefault(field_item.name, field_item)
                if len(picked) >= MAX_JSON_FIELDS:
                    return list(picked.values())
        return list(picked.values())

    def wants(self, feature: str) -> bool:
        """有没有扩展声明要这项信息（例如 ``"touch"`` = 他这一轮碰了她哪儿）。"""

        key = str(feature or "")
        if not key:
            return False
        if any(key in tuple(getattr(spec, "wants", ()) or ()) for spec in self.specs.values()):
            return True
        # 自己声明的字段也叫得上号：两套写法别互相不认识
        return any(field_item.name == key for field_item in self.json_fields())

    async def extra(self, state: Any, values: dict[str, Any]) -> bool:
        """把"主模型额外标出来的字段"转交给声明它们的扩展；没人要就什么都不做。"""

        picked = {
            str(key): value
            for key, value in dict(values or {}).items()
            if str(key) and value not in (None, "", [], {})
        }
        if not picked:
            return False
        handled = False
        for spec in self.specs.values():
            declared = {field_item.name for field_item in self._declared_fields(spec)}
            if not declared:
                continue
            mine = {key: value for key, value in picked.items() if key in declared}
            if not mine:
                continue
            hook = getattr(spec, "on_json", None)
            if hook is None:
                continue
            try:
                result = hook(state, mine, self)
                if hasattr(result, "__await__"):
                    await result
                handled = True
            except Exception:
                continue
        return handled

    async def reply_extra(self, state: Any, values: dict[str, Any]) -> str:
        """这一轮要补在她的话后面发出去的那段文本（第一个返回非空的扩展说了算）。"""

        picked = {
            str(key): value
            for key, value in dict(values or {}).items()
            if str(key) and value not in (None, "", [], {})
        }
        if not picked:
            return ""
        for spec in self.specs.values():
            hook = getattr(spec, "on_reply_extra", None)
            if hook is None:
                continue
            try:
                result = hook(state, picked, self)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception:
                continue
            text = _keep_paragraphs(result)
            if text:
                return text
        return ""

    def tone_scale(self, state: Any, tone: str) -> float:
        """这一轮口吻脉冲的倍数（第一个表态的扩展说了算，默认 1.0）。"""

        for spec in self.specs.values():
            hook = getattr(spec, "tone_scale", None)
            if hook is None:
                continue
            try:
                result = hook(state, str(tone or ""))
            except Exception:
                continue
            if result is None:
                continue
            try:
                number = float(result)
            except (TypeError, ValueError):
                continue
            return max(0.0, min(1.5, number))
        return 1.0

    def reply_is_bad(self, state: Any, raw: str) -> bool:
        """有没有扩展说"这一轮是废的"（例如模型拒答）。**第一个表态的说了算。**"""

        for spec in self.specs.values():
            hook = getattr(spec, "reply_guard", None)
            if hook is None:
                continue
            try:
                if hook(state, str(raw or ""), self):
                    return True
            except Exception:
                continue
        return False

    async def touch(self, state: Any, parts: list[str]) -> bool:
        """把"他碰了她哪儿"转交给声明要它的扩展；没人要就什么都不做。"""

        picked = [str(item).strip() for item in (parts or []) if str(item).strip()]
        if not picked:
            return False
        handled = False
        for spec in self.specs.values():
            hook = getattr(spec, "on_touch", None)
            if hook is None or "touch" not in tuple(getattr(spec, "wants", ()) or ()):
                continue
            try:
                result = hook(state, picked, 1.0, self)
                if hasattr(result, "__await__"):
                    await result
                handled = True
            except Exception:
                continue
        return handled

    def contact_relief_scale(
        self,
        definition: Any,
        state: Any,
        session_id: str = "",
    ) -> float:
        """这一次接触有多"解渴"（0~1）：扩展说了算，没人说就是 1。

        新写法 ``(definition, state, session_id, host) -> 0~1``；老写法
        ``(state) -> bool`` 也认。多个扩展时取**最小**的那一个——有人说
        "这一下不算满足"，就别让另一个把它算成满足。
        """

        scale = 1.0
        for spec in self.specs.values():
            hook = getattr(spec, "desire_relief", None)
            if hook is None:
                continue
            value: Any = None
            try:
                value = hook(definition, state, session_id, self)
            except TypeError:
                try:
                    value = hook(state)
                except Exception:
                    continue
            except Exception:
                continue
            try:
                if isinstance(value, bool):
                    number = 1.0 if value else 0.0
                else:
                    number = float(value)
            except (TypeError, ValueError):
                continue
            scale = min(scale, max(0.0, min(1.0, number)))
        return scale

    def allows_desire_relief(self, state: Any) -> bool:
        """老接口：这次接触算不算满足（0 比例 = 不算）。"""

        return self.contact_relief_scale(None, state, "") > 0

    async def command_event(self, session_id: str, text: str) -> str | None:
        """``/vw event <文本>`` 交给扩展先看一眼：谁认领，这一条就归谁。

        返回 ``None`` = 没人认领（主插件按原来的事件流程走）；
        返回字符串 = 认领了，那句话直接回给用户（空串 = 不用回话）。
        """

        seed = " ".join(str(text or "").split())
        engine = getattr(self.plugin, "engine", None)
        state = None
        if engine is not None and str(session_id or ""):
            try:
                state = await engine.load_state(str(session_id), cold_start=False)
            except Exception:
                state = None
        for spec in self.specs.values():
            hook = getattr(spec, "on_command_event", None)
            if hook is None:
                continue
            try:
                result = hook(state, seed, str(session_id or ""), self)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception:
                continue
            if result is None:
                continue
            return str(result)
        return None

    # ---------------- 扩展用得上的能力 ----------------

    def llm(self) -> Any:
        """打杂模型（写长描述、判东西都走它）；没配就是主模型。"""

        engine = getattr(self.plugin, "engine", None)
        return getattr(engine, "helper_llm", None) or getattr(engine, "llm", None)

    def _model_channel(self, slot: str = "") -> Any:
        """按**档位**取模型通道：``helper`` / ``creator`` / ``llm`` / ``event`` / …

        认不出来的档位（或那一档没配）都退回 :meth:`llm`（打杂 → 主模型）。
        """

        key = str(slot or "").strip().lower()
        engine = getattr(self.plugin, "engine", None)
        if not key or engine is None or key not in MODEL_SLOTS:
            return self.llm()
        channel = getattr(engine, f"{key}_llm", None)
        return channel or self.llm()

    def model_slots(self) -> dict[str, str]:
        """每一档模型现在指向哪个 Provider（扩展自己的面板上显示用，只给 id）。"""

        getter = getattr(self.plugin, "_provider_slots", None)
        if getter is None:
            return {}
        try:
            return {str(key): str(value) for key, value in dict(getter()).items()}
        except Exception:
            return {}

    async def generate(
        self,
        session_id: str,
        *,
        system_prompt: str,
        prompt: str,
        temperature: float | None = None,
        slot: str = "",
    ) -> str:
        """让模型写一段文字（扩展自己的提示词，不经过主插件的那些层）。

        ``slot``：用哪一档模型（``helper`` 打杂 / ``creator`` 内容生成 / ``llm`` 主模型…），
        留空 = 默认那档（打杂 → 主模型）。
        """

        channel = self._model_channel(slot)
        if channel is None:
            return ""
        try:
            reply = await channel.generate(
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=temperature,
            )
        except Exception:
            return ""
        if not getattr(reply, "ok", False):
            return ""
        return str(getattr(reply, "text", "") or "").strip()

    def is_private(self, session_id: str) -> bool:
        """这个会话是不是私聊（扩展自己也要用：很多事只能在私聊里做）。"""

        engine = getattr(self.plugin, "engine", None)
        if engine is None:
            return False
        session = engine.session_config(str(session_id or ""))
        return str(getattr(session, "type", "") or "") == "private"

    def node(self, state: Any) -> Any:
        """她现在在哪个地点（地点定义对象；拿不到就是 None）。"""

        engine = getattr(self.plugin, "engine", None)
        if engine is None:
            return None
        try:
            return engine.node(str(getattr(state, "node_id", "") or ""))
        except Exception:
            return None

    def nodes(self) -> list[dict[str, str]]:
        """主插件里所有地点（``id`` / ``name``）：扩展做"地点多选"时用。"""

        engine = getattr(self.plugin, "engine", None)
        world = getattr(engine, "world", None)
        picked: list[dict[str, str]] = []
        for item in getattr(world, "nodes", []) or []:
            picked.append({"id": str(getattr(item, "id", "")), "name": str(getattr(item, "name", "") or getattr(item, "id", ""))})
        return picked

    def pronoun(self) -> str:
        """她/他：跟主插件的性别设置走（扩展页和扩展文案都用它）。"""

        engine = getattr(self.plugin, "engine", None)
        world = getattr(engine, "world", None)
        try:
            from .models import pronoun_for

            return pronoun_for(getattr(world, "gender", "female"))
        except Exception:
            return "她"

    def person(self, state: Any, user_id: str) -> dict[str, Any]:
        """某个人在通讯录里的只读资料：名字、好感、关系档，以及**记在他名下的那些事实**。

        扩展要"知道对方是谁、什么关系、有什么底细"（比如文案里要写清双方的性别）时用它。
        读不到（没装通讯录 / 没这个人）就返回空字典，扩展自己兜底。
        """

        engine = getattr(self.plugin, "engine", None)
        store = getattr(engine, "profiles", None)
        uid = str(user_id or "").strip()
        if store is None or not uid:
            return {}
        session_id = str(getattr(state, "session_id", "") or "")
        try:
            view = store.view(session_id, uid)
        except Exception:
            view = None
        if view is None:
            return {}
        facts: list[dict[str, str]] = []
        for item in list(getattr(view, "facts", None) or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            facts.append({"kind": str(item.get("kind") or ""), "text": text})
        return {
            "user_id": uid,
            "name": str(getattr(view, "name", "") or ""),
            "call_him": str(getattr(view, "call_him", "") or ""),
            "affinity": float(getattr(view, "affinity", 0.0) or 0.0),
            "level": str(getattr(view, "level", "") or ""),
            "facts": facts,
        }

    # ---------------- 世界状态 ----------------

    async def persona_text(self, state: Any, session_id: str = "") -> str:
        """这个会话当前生效的人设原文：扩展要"照她的口吻写点什么"时用它。

        读不到（没接引擎 / 会话没人设）就返回空串，扩展自己兜底。
        """

        engine = getattr(self.plugin, "engine", None)
        sid = str(session_id or getattr(state, "session_id", "") or "")
        hook = getattr(engine, "_persona_text", None)
        if hook is None or not sid:
            return ""
        try:
            return str(await hook(sid) or "")
        except Exception:
            return ""

    def sessions(self) -> list[dict[str, Any]]:
        """可选的"她在哪"：**会话组优先，然后是没有归组的会话**。

        会话组里的几个群 / 私聊是同一个她：状态、数值、记录只有一份（存在组代表会话名下），
        所以挑"组"才是挑到了那份状态；直接挑组里的某个会话会看到同一份数据，
        但界面上分两行容易让人以为是两套。
        """

        engine = getattr(self.plugin, "engine", None)
        items = getattr(getattr(engine, "sessions", None), "sessions", None) or []
        groups = getattr(getattr(engine, "sessions", None), "groups", None) or []
        enabled = {
            str(getattr(item, "session_id", "") or ""): item
            for item in items
            if str(getattr(item, "session_id", "") or "")
            and bool(getattr(item, "enabled", True))
        }
        picked: list[dict[str, Any]] = []
        covered: set[str] = set()
        for group in groups:
            members = [
                str(item) for item in (getattr(group, "sessions", None) or []) if str(item)
            ]
            main = str(getattr(group, "main_session", "") or "")
            target = main if main in enabled else next(
                (item for item in members if item in enabled), ""
            )
            if not target:
                continue
            name = str(getattr(group, "name", "") or getattr(group, "id", "") or "")
            picked.append(
                {
                    "session_id": target,
                    "type": "group",
                    "note": name,
                    "label": f"【会话组】{name or target}（{len(members) or 1} 个会话）",
                    "members": members,
                    "group_id": str(getattr(group, "id", "") or ""),
                }
            )
            covered.update(members)
        for item in items:
            session_id = str(getattr(item, "session_id", "") or "")
            if not session_id or not bool(getattr(item, "enabled", True)):
                continue
            if session_id in covered:
                continue
            note = str(getattr(item, "note", "") or "")
            picked.append(
                {
                    "session_id": session_id,
                    "type": str(getattr(item, "type", "") or ""),
                    "note": note,
                    "label": f"{note or session_id}（{str(getattr(item, 'type', '') or '会话')}）",
                    "members": [],
                }
            )
        return picked

    async def state(self, session_id: str) -> Any:
        """读一份世界状态（只读：改它不会存回去，要改用 :meth:`update_state`）。"""

        engine = getattr(self.plugin, "engine", None)
        if engine is None or not str(session_id or ""):
            return None
        try:
            return await engine.load_state(str(session_id), cold_start=False)
        except Exception:
            return None

    async def update_state(self, session_id: str, mutate: Callable[[Any], Any]) -> Any:
        """改一份世界状态：加锁 → 改 → 存回去。``mutate`` 的返回值原样返回。"""

        engine = getattr(self.plugin, "engine", None)
        if engine is None or not str(session_id or ""):
            return None
        async with engine.session_state(str(session_id)) as state:
            result = mutate(state)
            if hasattr(result, "__await__"):
                result = await result
            return result

    def node(self, state: Any) -> Any:
        """她现在在哪（``NodeDef``；查不到返回 ``None``）。"""

        engine = getattr(self.plugin, "engine", None)
        node_id = str(getattr(state, "node_id", "") or "")
        if engine is None or not node_id:
            return None
        try:
            return engine.node(node_id)
        except Exception:
            return None

    def place_text(self, state: Any) -> str:
        """她所在的地方叫什么（拿不到就退回 node_id）。"""

        node = self.node(state)
        if node is None:
            return str(getattr(state, "node_id", "") or "")
        return str(getattr(node, "name", "") or getattr(node, "id", "") or "")

    # ---------------- 写回世界 ----------------

    def adjust(self, state: Any, **deltas: float) -> dict[str, float]:
        """推几个世界数值（例如做完一件费力气的事扣一点精力），结果归一到 0~1。"""

        applied: dict[str, float] = {}
        for key, delta in deltas.items():
            if key not in ADJUSTABLE:
                continue
            try:
                step = float(delta)
            except (TypeError, ValueError):
                continue
            if step != step:  # NaN
                continue
            before = float(getattr(state, key, 0.0) or 0.0)
            after = max(0.0, min(1.0, before + step))
            try:
                setattr(state, key, after)
            except Exception:
                continue
            applied[key] = round(after - before, 4)
        return applied

    def remember(
        self,
        state: Any,
        text: str,
        *,
        kind: str = "scene",
        weight: float = 0.4,
        emotion: str = "",
        node_id: str = "",
    ) -> bool:
        """往她的长期记忆里写一条（``kind`` 见 :data:`MEMORY_KINDS`）。"""

        body = " ".join(str(text or "").split())
        engine = getattr(self.plugin, "engine", None)
        memory = getattr(engine, "memory", None)
        if memory is None or not body:
            return False
        if kind not in MEMORY_KINDS:
            kind = "scene"
        try:
            memory.remember(
                session_id=str(getattr(state, "session_id", "") or ""),
                persona_id="",
                node_id=str(node_id or getattr(state, "node_id", "") or ""),
                content=body[:220],
                memory_type=kind,
                emotion=str(emotion or getattr(state, "mood", "") or ""),
                weight=max(0.0, min(1.0, float(weight))),
                affect=float(getattr(state, "affect", 0.0) or 0.0),
                valence=float(getattr(state, "valence", 0.5) or 0.5),
            )
        except Exception:
            return False
        return True

    async def log(self, state: Any, kind: str, detail: dict | None = None) -> None:
        """往日志页写一条（扩展自己的事件，类型名自取）。"""

        engine = getattr(self.plugin, "engine", None)
        if engine is None:
            return
        try:
            await engine._log_event(state, str(kind), dict(detail or {}))
        except Exception:
            pass

    def add_affinity(
        self,
        state: Any,
        user_id: str,
        delta: float,
        *,
        reason: str = "",
        source: str = "extension",
    ) -> float:
        """给某个人加/减好感度（走主插件那套上限与日志）；返回实际变化量。"""

        engine = getattr(self.plugin, "engine", None)
        store = getattr(engine, "profiles", None)
        uid = str(user_id or "")
        amount = float(delta or 0.0)
        if store is None or not uid or not amount:
            return 0.0
        try:
            before = 0.0
            view = store.view(str(getattr(state, "session_id", "") or ""), uid)
            before = float(getattr(view, "affinity", before) or before)
            result = store.adjust_affinity(
                str(getattr(state, "session_id", "") or ""),
                uid,
                amount,
                reason=str(reason or ""),
                source=str(source or "extension"),
            )
            after = float((result or {}).get("value") or before)
            return after - before
        except Exception:
            return 0.0

    def note_self(self, state: Any, text: str, session_id: str = "") -> None:
        """把"她自己身上发生的事"写进聊天留档（private 会话才写；标成内部事件）。"""

        body = " ".join(str(text or "").split())
        if not body:
            return
        engine = getattr(self.plugin, "engine", None)
        keep = engine.chat_history_limit() if engine is not None else 50
        try:
            state.note_chat(
                user_id="",
                name="（她自己）",
                text=body,
                now=engine._now() if engine is not None else 0.0,
                keep=keep,
                internal=True,
                origin=str(session_id or ""),
            )
        except Exception:
            pass

    async def say(self, state: Any, text: str, session_id: str = "") -> bool:
        """让扩展**以她的身份说一句**：真的发到那个会话，也记进她的聊天留档。

        跟 :meth:`note_self` 的区别：那个只写留档背景（`internal=True`，平台不发），
        这个是**真的说话**——短句之外，扩展自己写的那一段叙述也能发出来。

        **换行照原样保留**：扩展写的长段叙述是靠空行分段的，
        把它压成一行就等于没有排版（一段几百字糊成一坨）。

        只该在**后台任务**里调：动作回调 / tick 里正拿着这个会话的锁，
        再进来会等锁（等不到就是死等）。
        """

        body = _keep_paragraphs(text)
        if not body:
            return False
        engine = getattr(self.plugin, "engine", None)
        sid = str(session_id or getattr(state, "session_id", "") or "")
        if engine is None or not sid:
            return False
        # 1) 先记进留档：她自己说的那句，跟走正常回复路径一个写法
        try:
            async with engine.session_state(sid) as fresh:
                fresh.note_chat(
                    user_id="__self__",
                    name=fresh.bot_current_nickname
                    or fresh.bot_base_nickname
                    or "她",
                    text=body,
                    now=engine._now(),
                    keep=engine.chat_history_limit(),
                    is_self=True,
                    origin=sid,
                )
                try:
                    engine._tag_chat_origin(fresh, sid)
                except Exception:
                    pass
        except Exception:
            return False
        # 2) 真的发出去（发送通道没注入 / 发失败都返回 False，让扩展自己决定怎么办）
        for name in ("say_sink", "debug_sink"):
            sink = getattr(engine, name, None)
            if sink is None:
                continue
            try:
                if await sink(sid, body):
                    return True
            except Exception:
                continue
        return False

    def snapshot(self, state: Any = None) -> dict[str, Any]:
        """设置页要的全部内容：字段定义 + 已保存的值（+ 面板数据）。"""

        items: list[dict[str, Any]] = []
        for spec in self.specs.values():
            items.append(
                {
                    "name": spec.name,
                    "title": spec.title or spec.name,
                    "version": spec.version,
                    "settings": [dict(item) for item in spec.settings],
                    "values": self.settings(spec.name),
                }
            )
        return {
            "extensions": items,
            "panel": self.panel(state) if state is not None else {},
        }


__all__ = ["ExtensionHost", "ExtensionSpec", "SETTINGS_FILE"]
