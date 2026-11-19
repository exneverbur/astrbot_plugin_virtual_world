"""谈资向量索引队列（B1-WP1）。

**定位**：把"哪些谈资该重算向量"变成一张可重试的队列表，把"算向量"这件事留给调用方
（B1-WP3 的引擎 tick / B1-WP4 的装配）在后台按预算慢慢跑。本模块**只做三件事**：

1. 索引文本配方 :func:`index_text`（纯函数，配方版本 ``INDEX_RECIPE_VERSION = "t1"``）；
2. 暴力余弦排序 :func:`rank_by_cosine`（纯函数，B0 不做 ANN 的决定见 §10.4）；
3. 队列 :class:`TopicIndexQueue`（入队 / 批量索引 / 有界重试 / 迟到结果丢弃）。

**本模块不碰的东西**：不查谈资、不做许可判定（``authorize`` 永远走
``core/topic_policy.py``）、不决定"召回谁"（那是 B1-WP3），也不自己造时钟、不自己
开数据库连接。所有 I/O 都通过 ``db`` 上的 ``call()`` 协议或同名方法走
``core/db.py`` 里 B1 追加的那组函数。

**铁律（本模块内逐条兑现）**：

* **任何异常都不外抛**：所有对外方法内部兜底，失败写 :attr:`TopicIndexQueue.last_error`
  （并同步到 kv ``embedding_last_error``），返回值退化成"这次什么都没做"。
* **不长时间持锁**：本模块每次只调一次 ``db``（一次 ``call`` 就是一次短事务），
  从不把数据库锁跨 ``await`` 持有，更不跨 embedding 请求持有。
* **未配置就完全不工作**：``provider_id`` 为空、``space`` 为 ``None``、或端口自己
  声明 ``provider`` 为 ``None`` 时，:meth:`TopicIndexQueue.run_batch` 直接返回空结果，
  **一次宿主调用都不发**。
* **迟到结果一律丢弃**：算出向量之前先重读权威谈资快照，``information_revision``
  对不上就标 ``stale``、**不写向量**——谈资被改过之后，旧文本算出来的向量比没有向量
  更糟（它会以"当前谈资"的身份被召回）。
* **换 provider / 模型 / 维度 = 换空间**：队列键**不含**空间（索引文本与空间无关），
  重建索引的唯一入口是 :meth:`TopicIndexQueue.clear` / :meth:`TopicIndexQueue.rescan`；
  向量行本身按 ``space_fingerprint`` 分行，跨空间不会互相污染（见 ``core/db.py`` 的
  ``topic_vectors`` 五列主键）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .db import TOPIC_INDEX_QUEUE_MAX_BACKOFF, unpack_vector


# ---------------------------------------------------------------------------
# 常量：索引文本配方
# ---------------------------------------------------------------------------

# 索引文本配方版本。改配方（换分隔符、加字段、改截断长度……）必须同时改这个字符串：
# ``topic_vectors.recipe_version`` 是主键的一部分，换版本 = 换一行，不会有"旧配方算的
# 向量被新配方当自己人"的事。
INDEX_RECIPE_VERSION = "t1"

# 索引文本的硬上限（字符，不是字节）。谈资摘要 + 关键事实超过就截断。
INDEX_TEXT_MAX_CHARS = 1000

# ``text_hash`` 取 sha256 十六进制的前多少位（与 ``topic_vectors.text_hash`` 同义）。
INDEX_TEXT_HASH_CHARS = 32

# 一行一条：摘要一行、每个关键事实各一行，用换行分隔。
INDEX_TEXT_LINE_SEPARATOR = "\n"


# ---------------------------------------------------------------------------
# 常量：队列行为默认值（与 world.topics.retrieval 的默认值同源）
# ---------------------------------------------------------------------------

DEFAULT_INDEX_BATCH_SIZE = 16
DEFAULT_REQUEST_TIMEOUT_SECONDS = 0.8
"""召回路径的单次嵌入超时默认（秒）：交互里必须快，引擎按配置覆盖。"""

DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS = 20.0
"""后台索引的单次嵌入超时默认（秒）：不占回复路径，给足余量。

真机实测（api.openlux.ai + ``text-embedding-3-small``）一次调用 3.5~4.3 秒，
沿用召回路径那个 0.8 秒会让后台批量**每一批都 provider_timeout**，
队列里的谈资永远出不来向量（真机自检踩过这个坑）。
"""

DEFAULT_INDEX_BATCH_SECONDS = 30.0
DEFAULT_INDEX_MAX_ATTEMPTS = 5
DEFAULT_RESCAN_LIMIT = 200

# "谈资暂时不在场"时的延迟重试间隔（秒）：快照**形状正常**、但这条谈资不在里面时用。
# 快照是按"当前有效 + 有效期窗口"过滤的（见 ``core/db.py:get_topic_snapshot``），
# "这轮查不到"可能是有效期窗口、可见性、事务时序造成的**暂时**不在场，不等于谈资被删；
# 判死（``stale``）会把队列行永久埋掉，所以这里改成"往后推一小时再捞一次"。
INDEX_DEFER_SECONDS = 3600.0

# 最近一次索引错误写进 kv 的键（B1-WP4 的状态接口读它，不用把队列实例端过去）。
EMBEDDING_LAST_ERROR_KV_KEY = "embedding_last_error"

# "上一次重建索引时用的向量空间"记在 kv 的键；与当前 ``space.fingerprint`` 不一致
# 就说明换过 provider / 模型 / 维度。
TOPIC_INDEX_SPACE_KV_KEY = "topic-index:space"

# B1 收尾第 6 条的补丁：**同一次重建用的 provider** 也记下来。
#
# 为什么非记不可：指纹虽然含 provider（`EmbeddingSpace.fingerprint` 把 provider 拼进了
# 哈希），但"清旧空间"那一步是用**当前** provider + **旧**指纹去删的——换过 provider
# 之后这个组合一条也匹配不上（旧行写的是"旧 provider + 旧指纹"），于是旧向量永远是
# 孤儿：谁也读不到（空间对不上）、谁也删不掉。
#
# 新的键存一行 JSON（``{"provider_id": …, "space_fingerprint": …}``），**老的裸指纹键
# 继续照写**（升级前写的书签不会丢，别的读取方也不受影响）。
TOPIC_INDEX_SPACE_STATE_KV_KEY = "topic-index:space-state"

# ``getattr(port, "provider", _MISSING)`` 的哨兵：用来区分"端口没有这个属性"和
# "端口明说自己是禁用的（``provider is None``）"。
_MISSING = object()


# ---------------------------------------------------------------------------
# 索引文本配方（纯函数）
# ---------------------------------------------------------------------------


def _normalize(value: Any) -> str:
    """把任意值归一成"没有多余空白的单行"（``None`` → 空串）。

    索引文本要么进 sha256，要么进向量请求，都不该被前后空白、连续空格、制表符、
    换行这些**跟语义无关**的东西影响——否则同一份谈资在两台机器上会算出两个
    ``text_hash``，幂等性就废了。
    """

    if value is None:
        return ""
    return " ".join(str(value).split())


def _index_lines(summary: Any, facts: Any) -> list[str]:
    """把 ``summary`` + ``facts`` 变成"去重后的一行一条"。

    去重是**按归一后的整行**去重、**保留第一次出现的顺序**：摘要永远第一行，关键事实
    按给定顺序跟在后面；和摘要一模一样的事实会被吃掉（不然同一句话会被算两遍权重）。
    """

    lines: list[str] = []
    head = _normalize(summary)
    if head:
        lines.append(head)
    if isinstance(facts, str):
        facts = (facts,)
    if facts is not None:
        for fact in facts:
            line = _normalize(fact)
            if line and line not in lines:
                lines.append(line)
    return lines


def index_text(summary: str, facts: Sequence[str] = ()) -> tuple[str, str]:
    """谈资 → 索引文本：``(text, text_hash)``，纯函数、无 I/O。

    配方（``INDEX_RECIPE_VERSION = "t1"``）：

    * 每段先去空白归一，**一行一条**，用换行分隔：摘要第一行，关键事实跟在后面；
    * 关键事实**去重**（含"与摘要重复"的那一条），顺序保持给定顺序；
    * 总量截断到 :data:`INDEX_TEXT_MAX_CHARS` 个字符（截断后去掉尾部空白）；
    * ``text_hash = sha256(text.encode('utf-8')).hexdigest()[:32]``——**哈希算在截断后的
      文本上**，所以它描述的就是真正送进 embedding 的那串字节；
    * **只收摘要与关键事实，绝不掺提示指令**：像"你手边有件可以顺口提一句的事"这种
      给模型看的引导语属于别的层，混进索引文本会污染向量（它会让所有谈资在同一方向上
      更相似）。索引文本 = 谈资自己说了什么，不是"该怎么用它"。

    ``summary`` 与 ``facts`` 全空（或只有空白）时返回 ``("", "")``——空文本算出来的
    是零向量，调用方必须据此跳过，本函数不替它编内容。
    """

    lines = _index_lines(summary, facts)
    if not lines:
        return ("", "")
    text = INDEX_TEXT_LINE_SEPARATOR.join(lines).strip()
    if len(text) > INDEX_TEXT_MAX_CHARS:
        text = text[:INDEX_TEXT_MAX_CHARS].strip()
    if not text:
        return ("", "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:INDEX_TEXT_HASH_CHARS]
    return (text, digest)


# ---------------------------------------------------------------------------
# 暴力余弦排序（纯函数）
# ---------------------------------------------------------------------------


def _clean_vector(raw: Any, *, dim: int | None = None) -> tuple[list[float] | None, float]:
    """把一行"疑似向量"清理成 ``(分量列表, 范数)``；不可用返回 ``(None, 0.0)``。

    不可用 = 不是 bytes / 不是数字序列 / 空 / 维度不符（``dim`` 给了的话）/ 有
    ``NaN`` / ``±Inf`` / 范数为零（零向量和谁算余弦都是 0/0，denominator 是零，
    在 numpy 里会变成 NaN，在这里直接拒收更省事）。

    ``self._vector_problem`` / ``check_embedding_vectors`` 已经在写库前挡过一轮，
    这里是**读路径上的第二道闸**：库里可能存着更早版本、或者被别人写坏的字节，
    绝不能让一个坏向量把整轮召回的分数变成 NaN。
    """

    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            values = unpack_vector(raw)
        except (TypeError, ValueError):
            return (None, 0.0)
    elif isinstance(raw, (list, tuple)):
        try:
            values = [float(item) for item in raw]
        except (TypeError, ValueError):
            return (None, 0.0)
    else:
        return (None, 0.0)
    if not values:
        return (None, 0.0)
    if dim is not None and len(values) != dim:
        return (None, 0.0)
    total = 0.0
    for item in values:
        if not math.isfinite(item):
            return (None, 0.0)
        total += item * item
    if total <= 0.0:
        return (None, 0.0)
    return (values, math.sqrt(total))


def rank_by_cosine(
    query: Sequence[float], rows: Sequence[dict], *, limit: int
) -> list[tuple[str, float]]:
    """按余弦相似度给候选行排序，返回 ``[(topic_id, score), ...]``（纯函数）。

    ``query`` 是查询向量；``rows`` 每项至少含 ``topic_id`` 与 ``vector``
    （``vector`` 是 ``core.db.pack_vector`` 打的小端 float32 ``bytes``，也可以用
    现成的浮点序列）。**query 自己非法**（空 / 非有限 / 零范数 / 不是数字序列）时
    直接返回 ``[]``——没有可信的查询就没有排序，返回"随便挑几条"比返回空更危险。

    跳过（不进结果）的行：``vector`` 解不开、**维度与 query 不符**、含非有限数、
    零范数、``topic_id`` 为空。分数按降序；同分时按 ``topic_id`` 升序，保证同一批
    输入在任何机器上给同一个顺序（召回要可复现）。

    同一 ``topic_id`` 出现多行（例如同一谈资的旧 ``information_revision`` 行还留在
    库里）时**只留分最高的那行**：召回的单位是"谈资"，不是"向量行"。

    最坏情况 ``O(len(rows) * dim)``，是 B1 §10.4 明确接受的"首版暴力余弦 + 上限"
    （候选行由调用方先按会话组 / 状态 / 空间过滤，量级是"一组谈资"，不是全库）。
    """

    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        return []
    if wanted <= 0:
        return []
    query_values, query_norm = _clean_vector(query)
    if query_values is None:
        return []
    dim = len(query_values)
    scored: list[tuple[str, float]] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        topic_id = str(row.get("topic_id") or "")
        if not topic_id:
            continue
        values, norm = _clean_vector(row.get("vector"), dim=dim)
        if values is None:
            continue
        dot = 0.0
        for left, right in zip(query_values, values):
            dot += left * right
        score = dot / (query_norm * norm)
        # float32 往返 + 求和顺序会让"同一个方向"算出 1.0000000002，夹到 [-1, 1]，
        # 免得下游按"相似度 ∈ [-1,1]"做的判断被浮点噪声顶穿。
        if score > 1.0:
            score = 1.0
        elif score < -1.0:
            score = -1.0
        scored.append((topic_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    best: dict[str, float] = {}
    for topic_id, score in scored:
        if topic_id not in best:
            best[topic_id] = score
    return list(best.items())[:wanted]


# ---------------------------------------------------------------------------
# 索引队列
# ---------------------------------------------------------------------------


class TopicIndexQueue:
    """谈资向量索引队列：入队、按预算批量索引、有界重试、迟到结果丢弃。

    **canonical：B1-WP3 / B1-WP4 只用下面这一套**（``enqueue`` / ``refresh`` /
    ``run_batch`` / ``pending_count`` / ``clear`` / ``rescan`` / ``last_error``）；文件末尾
    那几个"旧形状"别名（``enqueue_topic`` / ``run_pending`` / ``status``）只是**一行委派**
    的兼容入口，不复制任何逻辑。

    构造：``TopicIndexQueue(db, *, provider_id="", space=None, clock=time.time)``。

    * ``db``：``core.db.Database`` 实例（直接调同名方法），或者任何带
      ``call(name, *args, **kwargs)`` 的替身（``core.db.AsyncDatabase`` 就是后者）；
    * ``provider_id`` / ``space``：由 B1-WP4 从插件配置装配。**两者缺一 = 关闭**：
      ``run_batch`` 直接返回空结果，一次宿主调用都不发（B1 §10.2 铁律 1）；
    * ``clock``：取"现在"的函数（默认 ``time.time``），测试注入假时钟用。

    状态记账：每行的 ``attempts`` / ``status`` / ``next_attempt_at`` 都在库里（重启
    不丢），失败原因在 :attr:`last_error`（字符串，``''`` 表示没错），并同步写 kv
    ``embedding_last_error`` 供状态接口读。

    线程 / 协程模型：**单个队列实例同一时刻只跑一批**（``run_batch`` 重入直接返回
    ``stopped='busy'``）。这不是为了防并发写坏数据（库层自己会串行化），而是为了让
    预算和"每批 embedding 发几次请求"可预测。
    """

    def __init__(
        self,
        db: Any,
        *,
        provider_id: str = "",
        space: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.provider_id = str(provider_id or "").strip()
        self.space = space
        self._clock = clock if callable(clock) else time.time
        self._last_error = ""
        self._in_batch = False
    # -- 只读状态 ---------------------------------------------------------

    @property
    def last_error(self) -> str:
        """最近一次失败的原因；``''`` = 目前没有未清掉的错误。

        "清掉"的时机是**一批全部处理干净**（一条不失败）之后，那时会顺手把 kv 里的
        ``embedding_last_error`` 也写回空串——状态接口不该永远挂着一个已经不存在的
        旧错误。
        """

        return self._last_error

    @property
    def configured(self) -> bool:
        """队列是否处于"可用"状态：``provider_id`` 非空 **且** ``space`` 不是 ``None``。

        这是 B1 §10.2 铁律 1 的判定口径，也是"未配置 = 与 A 逐字节相同"的分界线：
        未配置时 :meth:`run_batch` 连数据库都不查。
        """

        return bool(self.provider_id) and self.space is not None

    @property
    def space_fingerprint(self) -> str:
        """当前向量空间的指纹（``EmbeddingSpace.fingerprint``，没有就是空串）。"""

        return str(getattr(self.space, "fingerprint", "") or "")

    def _now(self) -> float:
        """取当前时间；时钟坏掉（抛异常 / 非有限）时退回 ``time.time()``，绝不外抛。"""

        try:
            value = float(self._clock())
        except Exception:
            return time.time()
        return value if math.isfinite(value) else time.time()

    def _deadline(self, started: float, budget_seconds: Any) -> float:
        """把 ``budget_seconds`` 翻成这一批的墙钟截止时刻（``inf`` = 不限）。

        预算的语义是"**单批总时长超过 ``budget_seconds`` 就立刻收工**"，而"零预算"不是一个
        有意义的要求：``0`` 会把每一批都就地掐断（一个请求都不发），让调用方"传 0 表示不设
        限"的直觉完全反过来。所以 ``<= 0`` / ``None`` / ``NaN`` / ``±Inf`` / 转不成数一律
        当**不限**（只受 ``batch_size`` 约束）；只有有限的**正数**才真的设闸门。
        """

        try:
            budget = float(budget_seconds)
        except (TypeError, ValueError):
            return float("inf")
        if not math.isfinite(budget) or budget <= 0.0:
            return float("inf")
        return started + budget

    def _request_timeout(self, timeout_seconds: Any, *, deadline: float, now: float) -> float:
        """单次请求的超时：调用方给的值再**夹到本批剩余预算内**（下限 0.05s）。

        闸门（:meth:`_deadline`）只在发请求前查一次，所以单批墙钟上限本来是
        ``budget_seconds + timeout_seconds``；把请求超时夹到剩余预算里，就把"这一批最多
        花多久"真正收敛到预算附近，而不是每个请求各自再花掉一个完整超时。
        """

        try:
            wanted = float(timeout_seconds)
        except (TypeError, ValueError):
            wanted = DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS
        if not math.isfinite(wanted) or wanted <= 0.0:
            # 坏值退回**宽的那个**默认：后台批量本来就该有 20 秒余量；召回路径的
            # 单次超时还会被总预算（``query_total_budget_seconds``）夹住，不会真等这么久
            wanted = DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS
        left = deadline - now
        if not math.isfinite(left):
            return wanted
        return max(0.05, min(wanted, left))

    # -- 底层：数据库调用 + 错误记账 --------------------------------------

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """调一次数据库，两种协议都认（``call()`` 优先，其次同名方法）。

        ``core.db.AsyncDatabase`` 是前者（``call`` 会 ``to_thread`` 过去），
        ``core.db.Database`` 与测试里的同步替身是后者。返回 awaitable 就 await——
        这样"同步替身 / 异步替身 / 两种真库"四种组合都能跑同一份逻辑。
        """

        caller = getattr(self.db, "call", None)
        if callable(caller):
            outcome = caller(name, *args, **kwargs)
        else:
            target = getattr(self.db, name, None)
            if not callable(target):
                raise AttributeError(f"数据库对象没有 {name}()")
            outcome = target(*args, **kwargs)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    async def _note_error(self, message: Any) -> None:
        """记一次失败：更新 :attr:`last_error`，变了就同步写 kv（best-effort）。

        相同的原因不重复写 kv（避免每次后台批都刷一次盘）；kv 写失败也不影响队列
        本身（状态接口读不到只是少了个观测点，不该把索引循环带崩）。
        """

        text = str(message or "").strip()
        if text == self._last_error:
            return
        self._last_error = text
        try:
            await self._call("kv_set", EMBEDDING_LAST_ERROR_KV_KEY, text)
        except Exception:
            pass

    def _empty_result(self) -> dict[str, Any]:
        """一次批量作业的标准返回骨架（失败路径也用它，键一个不少）。

        逐行终态四个计数键：``indexed``（写完向量 + 标 done）、``failed``（这批里没算成
        的行数）、``permanent_failures``（其中 ``attempts`` 到顶、已经被标成 ``failed``
        的行数——``failed`` 会给它们再留一次机会）、``deferred``（快照里暂时看不到、
        推到 ``INDEX_DEFER_SECONDS`` 之后再来的行）。
        """

        return {
            "scanned": 0,
            "indexed": 0,
            "failed": 0,
            "permanent_failures": 0,
            "deferred": 0,
            "stale": 0,
            "skipped": 0,
            "errors": [],
            "last_error": self._last_error,
            "configured": self.configured,
            "stopped": "",
        }

    # -- 入队 -------------------------------------------------------------

    async def enqueue(self, rows: Sequence[dict[str, Any]]) -> int:
        """把一批谈资登记进队列，返回**真正新入队**的条数。

        每行形状：``{'topic_id': str, 'information_revision': int, 'summary': str,
        'facts': list[str], 'scope_id': str (可选)}``。``scope_id`` 只用于"按会话组
        清空 / 计数"，不入幂等键。

        * **幂等**：键是 ``(topic_id, information_revision, text_hash)``。同一份索引
          文本重复入队不产生第二行；冲突时**只复活死行**——旧行是 ``failed`` / ``stale``
          就把这行拉回 ``pending``、``attempts`` 归零、``next_attempt_at`` 重设成"立即可取"
          （这是"重试次数用尽"与"换 provider 前标过 stale"的唯一出路）；旧行是
          ``pending`` / ``done`` 就一行不动，也**不重置** ``attempts`` / ``status`` /
          ``next_attempt_at``（已经算完的行不会被重复入队重新拉起来；换空间的重建走
          :meth:`clear` / :meth:`rescan`）。
        * **跳过**：``text`` 为空（摘要与事实全空）或 ``topic_id`` 为空的行不入队，
          也不报错——"没内容可索引"是正常情况，不是故障。
        * **不外抛**：单行出问题只记 :attr:`last_error` 并继续下一行；整批出问题
          （库挂了）记错误后返回已入队的条数。
        """

        added = 0
        try:
            stamp = self._now()
            for row in rows or ():
                try:
                    if not isinstance(row, dict):
                        continue
                    topic_id = str(row.get("topic_id") or "").strip()
                    if not topic_id:
                        continue
                    text, text_hash = index_text(row.get("summary") or "", row.get("facts") or ())
                    if not text:
                        continue
                    inserted = await self._call(
                        "enqueue_topic_index",
                        topic_id=topic_id,
                        information_revision=int(row.get("information_revision") or 1),
                        text=text,
                        text_hash=text_hash,
                        scope_id=str(row.get("scope_id") or ""),
                        now=stamp,
                    )
                    if inserted:
                        added += 1
                except Exception as exc:  # 单行坏数据不该拖垮整批
                    await self._note_error(f"入队失败：{exc!r}")
        except Exception as exc:
            await self._note_error(f"入队失败：{exc!r}")
        return added

    async def refresh(self, rows: Sequence[dict[str, Any]]) -> int:
        """让一批"**索引文本已经改了**"的谈资重新排队，返回真正重新排上的条数。

        和 :meth:`enqueue` 的分工，是这一层最要紧的一句话：

        * :meth:`enqueue` 是"**登记一条素材**"，幂等键 ``(topic_id, information_revision,
          text_hash)`` 撞上 ``pending`` / ``done`` 就一行不动——这是它刻意保留的语义（重复
          登记不产生第二行、不重置 ``attempts``），**本方法不碰它**。
        * :meth:`refresh` 是"**同一条谈资的索引文本变了**"：``update_topic`` 只涨
          ``version``、绝不动 ``information_revision``，所以改完 summary / details 之后
          新文本的 ``text_hash`` 和旧行对不上，两边谁也认不出谁——旧行是 ``done``，新文本
          又永远不会被入队，向量就永远停在旧摘要上。这里必须**先删旧、再按新文本入队**。

        每行形状与 :meth:`enqueue` 完全一样（``topic_id`` / ``information_revision`` /
        ``summary`` / ``facts`` / ``scope_id``）。逐行两步：

        1. 先走 :meth:`enqueue` 的既有路径登记**新文本**（顺带继承它的跳过与死行复活规则）；
        2. 再 ``drop_superseded_topic_index``：删掉这条谈资**除了目标幂等键之外**的队列行
           （旧版本的作业、同一版本下旧文本的作业），以及本空间里**版本对不上**的向量行。

        **顺序是「先建后删」**（B1 收尾第 5 条）：这两步各自是一个事务，中间失败（库挂了）
        时，先建后删留下的是"新作业 + 旧 done 行"——新作业照常会被 :meth:`run_batch` 处理，
        向量**会**更新，旧行下一次重复 / 重扫时被删掉。反过来（先删后建）一旦第二步失败，
        就成了"这条谈资暂时没有作业行"、向量停在旧摘要上，只能等下一次编辑或重扫才自愈。
        两步都不碰"文本没变"的行（幂等键撞上 ``pending``/``done`` 时 enqueue 一行不动）。

        **不负责**判断"这次到底改没改文本"：调用方只在真改了时才调（引擎那边比
        ``(summary, facts)``）。索引文本为空的行（摘要与事实都空）按 :meth:`enqueue` 的
        约定跳过，**旧行也不删**——那是"这条谈资没有可索引的内容"，清理属于
        :meth:`clear` / :meth:`rescan`。

        未配置（``provider_id`` 空 / ``space`` 是 ``None``）时**一次数据库调用都不发**、
        直接返回 0（与 :meth:`run_batch` 同一条铁律）；单行失败只记 :attr:`last_error`，
        不外抛、不影响别的行。
        """

        if not self.configured:
            return 0
        added = 0
        try:
            stamp = self._now()
            for row in rows or ():
                try:
                    if not isinstance(row, dict):
                        continue
                    topic_id = str(row.get("topic_id") or "").strip()
                    if not topic_id:
                        continue
                    text, text_hash = index_text(row.get("summary") or "", row.get("facts") or ())
                    if not text:
                        continue
                    # 先建后删（见 docstring 的 B1 收尾第 5 条）：中间挂了也不会
                    # 变成"这条谈资没有作业行"。
                    inserted = await self._call(
                        "enqueue_topic_index",
                        topic_id=topic_id,
                        information_revision=int(row.get("information_revision") or 1),
                        text=text,
                        text_hash=text_hash,
                        scope_id=str(row.get("scope_id") or ""),
                        now=stamp,
                    )
                    # 计数紧跟在**建作业**之后：返回值回答的是"这次真排上了几条"，
                    # 清理旧行失败（下面那句）不该让这个数字变小（B1 收尾第 5 条）。
                    if inserted:
                        added += 1
                    await self._call(
                        "drop_superseded_topic_index",
                        topic_id=topic_id,
                        information_revision=int(row.get("information_revision") or 1),
                        text_hash=text_hash,
                        provider_id=self.provider_id,
                        space_fingerprint=self.space_fingerprint,
                        recipe_version=INDEX_RECIPE_VERSION,
                    )
                except Exception as exc:  # 单行坏数据不该拖垮整批
                    await self._note_error(f"重新索引失败：{exc!r}")
        except Exception as exc:
            await self._note_error(f"重新索引失败：{exc!r}")
        return added

    # -- 批量索引 ---------------------------------------------------------

    async def run_batch(
        self,
        port: Any,
        *,
        batch_size: int = DEFAULT_INDEX_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS,
        budget_seconds: float = DEFAULT_INDEX_BATCH_SECONDS,
        max_attempts: int = DEFAULT_INDEX_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """跑一批索引作业，返回 ``{scanned, indexed, failed, permanent_failures, deferred,
        stale, skipped, errors, last_error, configured, stopped}``。

        流程（每一步失败都**只降级、不外抛**）：

        1. **未配置直接收工**：``provider_id`` 空 / ``space is None`` / 端口自己声明
           ``provider is None`` / ``port is None`` → 空结果 + 0 次宿主调用 + 0 次数据库
           调用（这一条是"未配置 = 与 A 逐字节相同"的地基）。
        2. 取 due 行：``status='pending'`` 且 ``next_attempt_at <= now``，按 ``id`` 升序，
           最多 ``batch_size`` 条。
        3. **重读权威快照**：按会话组分组调 ``get_topic_snapshot(ids, scope_id, now)``。
           两种"不在场"要分开：``information_revision`` 与队列行不一致 → 该行标 ``stale``
           （谈资被改过，这份索引再也用不上，**不写向量**）；快照形状正常但**整条谈资不在
           里面** → 该行**不判死**，标回 ``pending`` 并把 ``next_attempt_at`` 推到
           ``now + INDEX_DEFER_SECONDS``（有效期窗口 / 可见性 / 事务时序都可能让它"暂时
           不在场"，判死会把行永久埋掉），记在 ``deferred`` 里。快照**读失败**（抛异常）
           或者**形状畸形**（payload 不是 dict、没有 ``topics`` 键、``topics`` 不是 dict）
           → 整批不动（行保持 pending、不烧 ``attempts``），``stopped='snapshot_error'``。
        4. 全批一次 ``port.embed(texts, provider_id=..., space=...)``，外面套
           ``asyncio.wait_for(timeout)``，``timeout`` 是"调用方给的 ``timeout_seconds``"
           再**夹到本批剩余预算内**（``max(0.05, min(timeout_seconds, deadline - now))``）；
           **空 texts 不发起请求**。
        5. 失败（超时 / 抛异常 / ``ok=False`` / 批次校验不过 / 条数不符 / 空间不符）→
           每条 ``attempts + 1``、``next_attempt_at = now + min(60, 2 ** attempts)``，
           ``attempts >= max_attempts`` 时标 ``failed``（不再自动重试，计入
           ``permanent_failures``）；**不写向量**。
        6. 成功 → 按行写 ``upsert_topic_vector``（带 ``dim`` / ``text_hash`` /
           ``recipe_version=INDEX_RECIPE_VERSION`` / ``status='ready'`` /
           ``space_fingerprint``），写成功再标 ``done``。
        7. ``budget_seconds`` 是**单批墙钟预算**：``<= 0``、``None``、``NaN``、``±Inf``
           或转不成数 → **视为不限**（这一批只受 ``batch_size`` 约束，不做时间闸门）；
           给了有限正数就"发请求前查一次"（用完就 ``skipped`` + ``over_budget``，剩下的
           due 行留给下一批），这一次检查之后**不再打断**，所以单批墙钟上限是
           ``budget_seconds + 实际请求耗时``（请求超时本身已经被夹到剩余预算内）。

        返回值口径（**逐行终态看四个计数键**）：``indexed`` = 写完向量 + 标 ``done`` 的行；
        ``failed`` = 这一批里因为供应商 / 写库失败而**没算成**的行（没到 ``max_attempts``
        的会留在 ``pending`` 等下一轮）；``permanent_failures`` = 其中 ``attempts`` 已经到顶、
        被标成 ``failed``（不会自动再试）的行数；``deferred`` = 快照里暂时看不到、推到
        ``INDEX_DEFER_SECONDS`` 之后再来的行；``stale`` = ``information_revision`` 对不上、
        被永久丢弃的行；``skipped`` = 捞到了但因为预算这轮没发出去的行；``errors`` = 本轮
        的错误描述（可能多条）；``last_error`` = :attr:`last_error` 的当前值。

        ``stopped`` 说明的是**这一批为什么结束**，不是逐行终态（例如
        ``provider_timeout`` / ``provider_error`` 批次里可能同时存在"还有机会"和"已永久
        失败"的行；后者要看 ``permanent_failures`` 和库里的 ``status``）。
        """

        outcome = self._empty_result()
        if not self.configured or port is None:
            outcome["stopped"] = "provider_disabled" if not self.configured else "no_port"
            return outcome
        # 端口自己说"我没配置"时也别发请求（原派发书铁律：provider is None → 0 次宿主
        # 调用）。用哨兵取值：替身没有这个属性时**不算禁用**，只有属性存在且为 None 才算。
        if getattr(port, "provider", _MISSING) is None:
            outcome["stopped"] = "provider_disabled"
            return outcome
        if self._in_batch:
            outcome["stopped"] = "busy"
            return outcome
        self._in_batch = True
        try:
            return await self._run_batch(
                port,
                batch_size=batch_size,
                timeout_seconds=timeout_seconds,
                budget_seconds=budget_seconds,
                max_attempts=max_attempts,
            )
        except Exception as exc:  # 兜底：这个函数永远不往外抛
            outcome["errors"].append(f"索引批失败：{exc!r}")
            outcome["stopped"] = "error"
            await self._note_error(outcome["errors"][-1])
            outcome["last_error"] = self._last_error
            return outcome
        finally:
            self._in_batch = False

    async def _run_batch(
        self,
        port: Any,
        *,
        batch_size: int,
        timeout_seconds: float,
        budget_seconds: float,
        max_attempts: int,
    ) -> dict[str, Any]:
        """:meth:`run_batch` 的正身（已经过了"配置好没有 / 重入没有"两道门）。"""

        result = self._empty_result()
        started = self._now()
        deadline = self._deadline(started, budget_seconds)
        fingerprint = self.space_fingerprint
        if not fingerprint:
            # 空间连指纹都没有就没法给向量分行，写了也会和别的空间混在一起。
            result["stopped"] = "space_unknown"
            return result

        try:
            due = await self._call("claim_topic_index_due", now=started, limit=batch_size)
        except Exception as exc:
            result["stopped"] = "db_error"
            result["errors"].append(f"取 due 行失败：{exc!r}")
            await self._note_error(result["errors"][-1])
            result["last_error"] = self._last_error
            return result
        rows = [item for item in (due or ()) if isinstance(item, dict)]
        result["scanned"] = len(rows)
        if not rows:
            result["stopped"] = "empty"
            return result

        snapshot = await self._read_snapshot(rows, now=started, result=result)
        if snapshot is None:
            return result

        fresh: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for row in rows:
            item = snapshot.get(str(row.get("topic_id") or ""))
            if item is None:
                # 快照形状正常、但这条谈资不在里面 = **暂时不在场**（有效期窗口 / 可见性 /
                # 事务时序……），不是"被删了"：判死（stale）会让这行永久埋掉，所以只推到
                # 一小时后再捞一次，也不烧 attempts。
                deferred.append(row)
                continue
            if int(item.get("information_revision") or 0) != int(row.get("information_revision") or 0):
                # 谈资被改过：这份索引文本再也用不上，迟到结果丢弃。
                stale.append(row)
                continue
            fresh.append(row)
        for row in stale:
            try:
                marked = await self._call(
                    "mark_topic_index", int(row.get("id") or 0), status="stale", now=started
                )
                if marked:
                    result["stale"] += 1
            except Exception as exc:
                result["errors"].append(f"标 stale 失败：{exc!r}")
        for row in deferred:
            try:
                marked = await self._call(
                    "mark_topic_index",
                    int(row.get("id") or 0),
                    status="pending",
                    now=started,
                    next_attempt_at=started + INDEX_DEFER_SECONDS,
                )
                if marked:
                    result["deferred"] += 1
            except Exception as exc:
                result["errors"].append(f"推迟重试失败：{exc!r}")
        if not fresh:
            # 整批都没发出请求：全是延迟行才叫 deferred_only，只要有一行永久判死就叫
            # stale_only（这样调用方一眼能看出"这批有没有丢东西"）。
            if result["deferred"] and not result["stale"]:
                result["stopped"] = "deferred_only"
            else:
                result["stopped"] = "stale_only"
            result["last_error"] = self._last_error
            return result

        if self._now() >= deadline:
            # 预算已经在取行 / 读快照上花光：这一批一个请求都不发，行原样留到下一批。
            result["skipped"] += len(fresh)
            result["stopped"] = "over_budget"
            result["last_error"] = self._last_error
            return result

        texts = [str(row.get("text") or "") for row in fresh]
        request_timeout = self._request_timeout(timeout_seconds, deadline=deadline, now=self._now())
        reason, batch = await self._embed(port, texts, timeout_seconds=request_timeout)
        if reason:
            await self._fail_rows(fresh, reason, now=started, max_attempts=max_attempts, result=result)
            result["stopped"] = "provider_timeout" if reason.startswith("provider_timeout") else "provider_error"
            result["last_error"] = self._last_error
            return result

        dim = int(getattr(batch, "dim", 0) or 0)
        for row, vector in zip(fresh, list(getattr(batch, "vectors", ()) or ())):
            topic_id = str(row.get("topic_id") or "")
            try:
                written = await self._call(
                    "upsert_topic_vector",
                    topic_id=topic_id,
                    information_revision=int(row.get("information_revision") or 0),
                    provider_id=self.provider_id,
                    recipe_version=INDEX_RECIPE_VERSION,
                    space_fingerprint=fingerprint,
                    dim=dim,
                    text_hash=str(row.get("text_hash") or ""),
                    vector=[float(item) for item in vector],
                    status="ready",
                    now=started,
                )
            except Exception as exc:
                written = False
                result["errors"].append(f"写向量失败（{topic_id}）：{exc!r}")
            if not written:
                await self._fail_rows(
                    [row],
                    f"provider_error: 向量落库失败（{topic_id}）",
                    now=started,
                    max_attempts=max_attempts,
                    result=result,
                )
                continue
            result["indexed"] += 1
            try:
                await self._call(
                    "mark_topic_index", int(row.get("id") or 0), status="done", now=started
                )
            except Exception as exc:
                result["errors"].append(f"标 done 失败（{topic_id}）：{exc!r}")

        if not result["errors"]:
            await self._note_error("")
        if result["failed"]:
            result["stopped"] = "failed"
        elif self._now() >= deadline:
            result["stopped"] = "over_budget"
        else:
            result["stopped"] = "ok"
        result["last_error"] = self._last_error
        return result

    async def _read_snapshot(
        self, rows: list[dict[str, Any]], *, now: float, result: dict[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        """按会话组重读权威谈资快照，返回 ``{topic_id: 行}``；读失败或**形状畸形**返回 ``None``。

        分组是因为 ``get_topic_snapshot`` 的 ``scope_id`` 是**必给的精确匹配**（谈资的
        隔离单位是会话组，跨组永不召回），一批 due 行可能来自多个组。

        调用方拿到 ``None`` 就**整批不动**（行保持 pending，下一轮再来、不烧 attempts）。
        所以"读不到权威快照"有**两种**同样算失败的情形，都不能被当成"谈资过期"：

        * ``get_topic_snapshot`` 抛异常；
        * 返回的 payload **形状畸形**（不是 dict、没有 ``topics`` 键、``topics`` 不是 dict）
          ——这里以前是静默 ``continue``，于是"读不到"被下游当成"谈资被删"直接标 ``stale``
          永久埋掉，和这条注释的语义正好相反。

        两种情形都走 :meth:`_note_error` + ``result['stopped'] = 'snapshot_error'``；
        形状正常但**某条谈资不在里面**不在这里判定（那是"暂时不在场"，
        ``_run_batch`` 会把它推到 ``INDEX_DEFER_SECONDS`` 之后再试）。
        """

        grouped: dict[str, list[str]] = {}
        for row in rows:
            topic_id = str(row.get("topic_id") or "")
            if topic_id:
                grouped.setdefault(str(row.get("scope_id") or ""), []).append(topic_id)
        snapshot: dict[str, dict[str, Any]] = {}
        try:
            for scope, ids in grouped.items():
                payload = await self._call("get_topic_snapshot", ids, scope_id=scope, now=now)
                topics = payload.get("topics") if isinstance(payload, dict) else None
                if not isinstance(topics, dict):
                    result["stopped"] = "snapshot_error"
                    result["errors"].append(
                        "重读谈资快照失败：快照形状畸形"
                        f"（scope_id={scope!r}，payload 类型 {type(payload).__name__}，"
                        "缺少可用的 topics 映射）"
                    )
                    await self._note_error(result["errors"][-1])
                    result["last_error"] = self._last_error
                    return None
                for topic_id, item in topics.items():
                    if isinstance(item, dict):
                        snapshot[str(topic_id)] = item
        except Exception as exc:
            # 读不到权威快照 ≠ 谈资过期：整批不动（行保持 pending），不烧重试次数。
            result["stopped"] = "snapshot_error"
            result["errors"].append(f"重读谈资快照失败：{exc!r}")
            await self._note_error(result["errors"][-1])
            result["last_error"] = self._last_error
            return None
        return snapshot

    async def _embed(self, port: Any, texts: list[str], *, timeout_seconds: float) -> tuple[str, Any]:
        """发一次批量 embedding，返回 ``(失败原因, 批次)``；成功时原因 ``''``。

        ``timeout_seconds`` 是**已经夹到本批剩余预算内**的单次超时（由
        :meth:`_request_timeout` 算好传进来，见 ``_run_batch``）；这里只做"转不成数 /
        非有限 / 非正数就退回 :data:`DEFAULT_REQUEST_TIMEOUT_SECONDS`"的兜底与
        ``0.001s`` 的下限，不重复做预算计算。

        失败原因一律以 ``provider_timeout`` / ``provider_error`` 开头，方便日志与状态
        接口分类；失败时批次是 ``None``（**不把失败批次往上带**，免得调用方手滑拿它的
        ``vectors`` 去落库）。
        """

        if not texts:
            return ("provider_error: 没有可发的文本", None)
        try:
            timeout = max(float(timeout_seconds or 0.0), 0.001)
        except (TypeError, ValueError):
            timeout = DEFAULT_REQUEST_TIMEOUT_SECONDS
        try:
            batch = await asyncio.wait_for(
                port.embed(texts, provider_id=self.provider_id, space=self.space),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return (f"provider_timeout: {timeout:g}s 内没有返回", None)
        except Exception as exc:
            return (f"provider_error: {exc!r}", None)
        problem = self._batch_problem(batch, len(texts))
        if problem:
            return (problem, None)
        return ("", batch)

    def _batch_problem(self, batch: Any, expected: int) -> str:
        """检查一批向量能不能落库；返回 ``''`` 表示没问题，否则是失败原因。

        检查项（B1 §10.2 铁律 2/6 的落地点）：批次自报成功、``validate()`` 通过
        （维度 / 非有限 / 零向量）、条数与文本数一致、带的空间就是我们要写的空间。
        任何一条不过都**整批不落库**——半个批次写进去会让"哪些谈资有向量"变得不可解释。
        """

        if batch is None:
            return "provider_error: 端口没有返回批次"
        if not getattr(batch, "ok", False):
            detail = str(getattr(batch, "error", "") or "供应商返回失败")
            return f"provider_error: {detail}"
        validate = getattr(batch, "validate", None)
        if callable(validate):
            try:
                problem = str(validate() or "")
            except Exception as exc:
                problem = f"批次校验抛异常：{exc!r}"
            if problem:
                return f"provider_error: {problem}"
        count = int(getattr(batch, "count", 0) or 0)
        if count != expected:
            return f"provider_error: 批次条数不符（期望 {expected}、实际 {count}）"
        got_space = getattr(batch, "space", None)
        if got_space is None or self.space is None:
            return "provider_error: 批次没有带向量空间"
        want_fp = self.space_fingerprint
        got_fp = str(getattr(got_space, "fingerprint", "") or "")
        if want_fp and got_fp and want_fp != got_fp:
            return f"provider_error: 批次空间不符（期望 {want_fp}、实际 {got_fp}）"
        return ""

    async def _fail_rows(
        self,
        rows: list[dict[str, Any]],
        reason: str,
        *,
        now: float,
        max_attempts: int,
        result: dict[str, Any],
    ) -> None:
        """把一批没算成的行记上重试（或标死），并记一次 :attr:`last_error`。

        退避 = ``min(60, 2 ** attempts)``（``attempts`` 是**加过这一次**的值），到
        ``max_attempts`` 就标 ``failed``，不再自动重试——谈资索引是"有空再算"的后台活，
        重试要收敛，不能变成一个永远在打电话的循环。

        ``result['failed']`` 数的是"这批没算成的行"，其中**真正被标成 ``failed``**（=
        ``attempts`` 到顶、不会再有自动重试）的那些另外记进
        ``result['permanent_failures']``：只看 ``stopped`` 会漏掉"批次因为超时结束、但
        里面有几行已经永久失败"这种情况。
        """

        ceiling = max(int(max_attempts or 0), 1)
        for row in rows:
            attempts = int(row.get("attempts") or 0) + 1
            terminal = attempts >= ceiling
            payload: dict[str, Any] = {
                "status": "failed" if terminal else "pending",
                "now": now,
                "attempts": attempts,
            }
            if not terminal:
                payload["next_attempt_at"] = now + min(
                    TOPIC_INDEX_QUEUE_MAX_BACKOFF, float(2 ** min(attempts, 30))
                )
            marked = False
            try:
                marked = bool(await self._call("mark_topic_index", int(row.get("id") or 0), **payload))
            except Exception as exc:
                result["errors"].append(f"记重试失败：{exc!r}")
            result["failed"] += 1
            if terminal and marked:
                result["permanent_failures"] += 1
        result["errors"].append(reason)
        await self._note_error(reason)

    # -- 队列管理 ---------------------------------------------------------

    async def pending_count(self) -> int:
        """还有多少行没算完（``status='pending'``）；查不到返回 0。"""

        try:
            return int(await self._call("count_pending_topic_index") or 0)
        except Exception as exc:
            await self._note_error(f"取待索引条数失败：{exc!r}")
            return 0

    async def clear(self, *, scope_id: str | None = None) -> int:
        """清队列 + 清**当前向量空间**的向量行，返回一共清掉多少行。

        ``scope_id`` 给 ``None``（默认）或空串 = 清所有会话组，给了就只清那一组
        （队列行按自己的 ``scope_id`` 过滤，向量行 join ``topics`` 按谈资的
        ``scope_id`` 过滤）。

        **清的是当前空间**：换 provider / 模型 / 维度之后旧空间的行留在库里（它们
        本来就进不了新空间的查询），要连旧空间一起清就得先用旧配置调一次——这是
        "不要误删还能用的向量"和"别让旧行悄悄复活"之间的取舍，B1 选前者。
        """

        removed = 0
        dropped = 0
        try:
            removed = int(await self._call("clear_topic_index", scope_id=scope_id) or 0)
        except Exception as exc:
            await self._note_error(f"清队列失败：{exc!r}")
        try:
            if self.provider_id and self.space_fingerprint:
                dropped = int(
                    await self._call(
                        "delete_topic_vectors_for_scope",
                        provider_id=self.provider_id,
                        space_fingerprint=self.space_fingerprint,
                        scope_id=scope_id,
                    )
                    or 0
                )
        except Exception as exc:
            await self._note_error(f"清向量失败：{exc!r}")
        return removed + dropped

    async def _read_space_state(self) -> tuple[str, str]:
        """读出"上一次重建用的那家 provider + 那个空间指纹"：``(provider_id, fingerprint)``。

        两个来源：新键（JSON，两个字段都有）优先；升级前写的老键（裸指纹）也读得出来，
        那时 provider 回空串——调用方按"不知道是哪家，用当前这家"处理。
        **任何读取失败都回 ``("", "")``**：读不出书签只是"当作第一次"，不该炸重建。
        """

        provider = ""
        fingerprint = ""
        try:
            raw = await self._call("kv_get", TOPIC_INDEX_SPACE_STATE_KV_KEY)
        except Exception:
            raw = None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                provider = str(parsed.get("provider_id") or "")
                fingerprint = str(parsed.get("space_fingerprint") or "")
        if not fingerprint:
            try:
                legacy = await self._call("kv_get", TOPIC_INDEX_SPACE_KV_KEY)
            except Exception:
                legacy = None
            if isinstance(legacy, str):
                fingerprint = legacy
        return provider, fingerprint

    async def rescan(self, *, scope_id: str = "", limit: int = DEFAULT_RESCAN_LIMIT) -> dict[str, Any]:
        """把"当前有效、但当前空间里还没有 ready 向量"的谈资**渐进**重新入队。

        返回 ``{"added": int, "cleared": int}``：``added`` = 这次真正入队的条数（新插的
        + 被复活的死行），``cleared`` = 因为换空间而顺带清掉的行数（队列行 + 旧空间的
        向量行；没换空间时恒为 0）。

        这是**换 provider / 模型 / 维度之后的唯一重建入口**（幂等键里没有空间，所以
        换空间不能靠"再入队一次"触发，只能靠"当前空间缺向量"这个事实）。一次最多
        ``limit`` 条，调用方（后台 tick）多跑几轮就是渐进重建。

        **换空间 = 清队列 + 删旧空间向量 + 重建**。判据是 kv ``topic-index:space`` 里记的
        上一次指纹：与当前 ``space.fingerprint`` 不同**且旧值非空**时，顺序必须是

        1. ``self.clear(scope_id=scope_id or None)``：清掉队列行（含旧空间的 ``done``、
           还有 ``failed`` / ``stale`` 死行——它们会挡住同键重入队）与**当前空间**的向量；
        2. 用**旧指纹**再调一次 ``delete_topic_vectors_for_scope``：把旧空间的向量也清掉
           （``clear`` 只清当前空间，见它的 docstring）；
        3. 才 ``topics_missing_vectors`` + ``enqueue``：按新空间重建。

        顺序不能反：先入队会被旧 ``done`` 行挡住（唯一键不含空间），先删队列又会让"清旧
        向量"失去组成员依据。旧指纹为空（第一次跑）不触发清理，只记下当前指纹。

        ``scope_id`` 空 = 所有会话组（管理动作，不是召回查询），给了就只重扫那一组；
        清理与重建用同一个 ``scope_id``。

        ``space`` 为 ``None`` / ``provider_id`` 为空 / 空间连指纹都没有时直接返回
        ``{"added": 0, "cleared": 0}``（且一次库调用都不发）：没有空间就没有"缺哪些向量"
        可言。
        """

        outcome: dict[str, Any] = {"added": 0, "cleared": 0}
        if not self.provider_id or self.space is None:
            return outcome
        fingerprint = self.space_fingerprint
        if not fingerprint:
            return outcome
        scope = scope_id or None
        try:
            stamp = self._now()
            previous_provider, previous = await self._read_space_state()
            # 换空间 = 指纹变了**或者**供应商换了（指纹含 provider，正常会一起变，
            # 但老数据里只记了裸指纹时 provider 是空，只能按当前这家来）。
            provider_changed = bool(previous_provider) and previous_provider != self.provider_id
            if previous and (previous != fingerprint or provider_changed):
                # 换空间：先清队列（含旧空间的 done / failed / stale 死行）+ 当前空间向量，
                # 再用**记下来的那家 provider + 旧指纹**清掉旧空间的向量（B1 收尾第 6 条：
                # 以前这里用当前 provider 去删，换过 provider 之后一条都删不掉，旧向量
                # 变成永远的孤儿），最后才重建（见 docstring 里的顺序理由）。
                outcome["cleared"] = int(await self.clear(scope_id=scope) or 0)
                try:
                    outcome["cleared"] += int(
                        await self._call(
                            "delete_topic_vectors_for_scope",
                            provider_id=previous_provider or self.provider_id,
                            space_fingerprint=previous,
                            scope_id=scope,
                        )
                        or 0
                    )
                except Exception as exc:
                    await self._note_error(f"清旧空间向量失败：{exc!r}")
                if provider_changed:
                    # 供应商换了：**当前** provider 名下也可能留着同一个旧指纹的行
                    # （同一家模型换部署、或者手工改过配置），一起清掉不留半份。
                    try:
                        outcome["cleared"] += int(
                            await self._call(
                                "delete_topic_vectors_for_scope",
                                provider_id=self.provider_id,
                                space_fingerprint=previous,
                                scope_id=scope,
                            )
                            or 0
                        )
                    except Exception as exc:
                        await self._note_error(f"清当前供应商的旧空间向量失败：{exc!r}")
            if previous != fingerprint or provider_changed:
                try:
                    await self._call("kv_set", TOPIC_INDEX_SPACE_KV_KEY, fingerprint)
                    await self._call(
                        "kv_set",
                        TOPIC_INDEX_SPACE_STATE_KV_KEY,
                        json.dumps(
                            {
                                "provider_id": self.provider_id,
                                "space_fingerprint": fingerprint,
                            },
                            ensure_ascii=False,
                        ),
                    )
                except Exception as exc:
                    await self._note_error(f"记录向量空间失败：{exc!r}")
            rows = await self._call(
                "topics_missing_vectors",
                provider_id=self.provider_id,
                space_fingerprint=fingerprint,
                recipe_version=INDEX_RECIPE_VERSION,
                scope_id=scope_id,
                now=stamp,
                limit=limit,
            )
        except Exception as exc:
            await self._note_error(f"重扫失败：{exc!r}")
            return outcome
        outcome["added"] = await self.enqueue([item for item in (rows or ()) if isinstance(item, dict)])
        return outcome

    # -- 旧形状兼容入口（纯委派，不复制逻辑）-------------------------------

    async def enqueue_topic(self, row: dict[str, Any]) -> bool:
        """旧形状别名：单行入队 → :meth:`enqueue`，返回"这次是否新入队"。"""

        return bool(await self.enqueue([row]))

    async def run_pending(
        self,
        port: Any,
        *,
        batch_size: int = DEFAULT_INDEX_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS,
        budget_seconds: float = DEFAULT_INDEX_BATCH_SECONDS,
        max_attempts: int = DEFAULT_INDEX_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """旧形状别名：一行委派给 :meth:`run_batch`（返回值原样透传）。"""

        return await self.run_batch(
            port,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            budget_seconds=budget_seconds,
            max_attempts=max_attempts,
        )

    async def status(self) -> dict[str, Any]:
        """状态快照（B1-WP4 的状态接口用）：条数 + **当前 space 指纹** + 最近错误。"""

        indexed = 0
        total = 0
        if self.provider_id and self.space_fingerprint:
            try:
                indexed = int(
                    await self._call(
                        "count_topic_vectors",
                        provider_id=self.provider_id,
                        space_fingerprint=self.space_fingerprint,
                        status="ready",
                    )
                    or 0
                )
                total = int(
                    await self._call(
                        "count_topic_vectors",
                        provider_id=self.provider_id,
                        space_fingerprint=self.space_fingerprint,
                    )
                    or 0
                )
            except Exception as exc:
                await self._note_error(f"取索引条数失败：{exc!r}")
        return {
            "provider_id": self.provider_id,
            "space_fingerprint": self.space_fingerprint,
            "indexed": indexed,
            "pending": await self.pending_count(),
            "total": total,
            "last_error": self._last_error,
        }


__all__ = [
    "INDEX_RECIPE_VERSION",
    "INDEX_TEXT_MAX_CHARS",
    "INDEX_TEXT_HASH_CHARS",
    "INDEX_TEXT_LINE_SEPARATOR",
    "EMBEDDING_LAST_ERROR_KV_KEY",
    "TOPIC_INDEX_SPACE_KV_KEY",
    "DEFAULT_INDEX_BATCH_SIZE",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_INDEX_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_INDEX_BATCH_SECONDS",
    "DEFAULT_INDEX_MAX_ATTEMPTS",
    "DEFAULT_RESCAN_LIMIT",
    "INDEX_DEFER_SECONDS",
    "index_text",
    "rank_by_cosine",
    "TopicIndexQueue",
]
