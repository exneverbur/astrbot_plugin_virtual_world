"""宿主能力端口（seam）。

引擎只依赖这里的 Protocol，AstrBot 的真实实现放在 main.py 的适配器里，
测试用 tests/stub_ports.py 里的内存实现。这样引擎逻辑可以在没有 AstrBot 的环境下被验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolInfo:
    """工具的元信息（给提示词和 WebUI 用）。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str = "plugin"
    """来源：``official`` = AstrBot 自带的内置工具，``plugin`` = 插件 / MCP 注册的工具。"""

    plugin: str = ""
    """提供这个工具的插件名（拿得到时填，编辑器的工具列表按它分组）。"""


@dataclass
class LLMReply:
    """LLM 返回结果。"""

    text: str = ""
    ok: bool = True
    error: str = ""
    finish_reason: str = ""
    """这一次调用的完成原因（``stop`` / ``length`` / ``content_filter`` …）。

    用户 m00001：输出没法解析时，``length``（被 max_tokens 截断）和 ``stop``（模型
    自己收尾但格式坏了）是两种完全不同的病。适配器拿不到就留空串——调用方按 ``null``
    记日志，**不要**编一个 ``stop`` 出来。
    """


@dataclass
class ToolCallResult:
    """一次工具调用的结果。

    ``ok=False`` 时 ``error`` 说明原因（没找到工具 / 没接上 handler / 抛异常 / 返回空），
    调用方据此决定是"把结果讲给大家听"还是"记一条失败日志、别硬编"。
    """

    ok: bool = False
    text: str = ""
    error: str = ""
    tool: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    """实际传给工具的参数（已按工具 schema 过滤），日志里记这个才准。"""

    image_urls: list[str] = field(default_factory=list)
    """工具 / 指令一起返回的图片（http、file://、base64:// 都可能）。

    转述模型可用时会在适配层就换成文字（不会出现在这里）；只有"直接交给多模态主模型"
    这条路才会把地址带上来。
    """

    attachments: list[str] = field(default_factory=list)
    """结果里带回来的图片原始地址——**用来发到群里**，不跟着转述走。

    和 ``image_urls`` 的区别：那个是"要给模型看的"（配了转述模型就被描述顶掉了），
    这个不管转述成不成功都留着。生图这类工具的结果是要给群友看的，不能只喂给模型。
    """

    audios: list[str] = field(default_factory=list)
    """结果里带回来的语音（合成语音的工具走这条路，用户 m20677）。

    地址形态同图片：``file://``、``http``、本机路径都可能有。引擎拿到之后交给
    ``MessagePort.send_record`` 发出去，并让回执里写清"这是语音"。
    """

    tool_call_id: str = ""
    """这一次工具调用的 id（E1 加，默认为空 ⇒ 老调用方逐字节不变）。

    由引擎的工具调用权威边界（``VirtualWorldEngine._call_tool``）生成并回填，
    用来把"工具回执"和"动作日志"对上——对不上的话，账里就只剩一堆看不出
    谁是谁的 ``tool_result``。
    """

    infrastructure: bool = False
    """前置条件不满足、**工具根本没被调用**（环境问题，不是她的失败）。

    典型场景：插件刚启动还没收到过消息（拿不到 ``event`` 当上下文）、工具没注册、
    工具没有可调用的 handler、没接上工具通道。这些情况下 ``ok=False`` 只说明
    "这会儿跑不起来"，不说明"她做错了"——以前把它当成她的失败，一次没跑起来就
    扣心情、还锁死变奏闸，线上 71 次 ``deepseek_web_search`` 全栽在这条上。

    为真时：``_log_tool_outcome`` 不记 ``tool_failed``（该记 ``tool_unavailable``）、
    ``_variation_turn_failed`` 跳过不算失败。``error`` 文案**照原样**交给模型，
    她得知道为什么没成，才能改走别的动作。
    """


@dataclass
class CardResult:
    """一次群名片操作的结果。``reason`` 说明为什么没成功，会进事件日志。"""

    ok: bool = False
    reason: str = ""
    card: str = ""


@dataclass
class PokeResult:
    """一次「戳一戳」的结果。

    协议端不一定支持（支持的只有 QQ 系），不支持时 ``reason`` 会写清原因，
    调用方据此退化成一句文案，而不是让这次动作凭空消失。
    """

    ok: bool = False
    reason: str = ""
    route: str = ""
    """实际走通的那条路（``poke 消息段`` / ``friend_poke`` / ``send_poke``…）。

    群聊和私聊走的不是同一条路（私聊里 poke 消息段不被支持，发出去客户端只会显示成
    一个认不出的占位），日志里要能看出这次到底是哪条路通了、哪条路为什么没通。
    """


@runtime_checkable
class LLMPort(Protocol):
    """单次 LLM 调用的入口。"""

    async def generate(
        self,
        *,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        image_urls: list[str] | None = None,
    ) -> LLMReply: ...


SEND_ACCEPTED = "accepted"
"""平台明确收下了（适配器返回真值），但不保证对方看到。"""
SEND_DELIVERED = "delivered"
"""平台给出了送达凭据（消息 id 等）。旧适配器返回 True 不许冒充这一档。"""
SEND_FAILED = "failed"
"""确定没发出去（异常、被拒、平台返回假值）。"""
SEND_UNKNOWN = "unknown"
"""无法判断。宁可记不确定，也不谎称已送达。"""


@dataclass
class SendReceipt:
    """一次发送尝试的**事实**回执（阶段 A2 统一登记口径）。

    ``bool(receipt)`` 保持旧 ``MessagePort`` 的布尔语义：只有 accepted / delivered
    算"发出去了"，failed / unknown 都是没有被确认的发送。旧适配器直接返回
    ``True`` / ``False`` 时用 :func:`as_receipt` 归一化：``True`` 记 accepted、
    ``False`` 记 failed，**不会**自动升级成 delivered。
    """

    target: str = ""
    status: str = SEND_UNKNOWN
    platform_message_ids: list[str] = field(default_factory=list)
    error: str = ""
    segments: int = 1
    """这一批分成了几条发（平台侧一条消息一个消息链）。"""

    sent_segments: int = 0
    attachments: int = 0
    operation_id: str = ""
    segment_receipts: list[dict[str, Any]] = field(default_factory=list)
    """Indexed segment facts: segment_id, index, status, platform_message_id.

    Missing or unknown entries remain pending confirmation, never automatic retries.
    """
    partial: bool = False
    """一部分发出去、一部分失败：状态记 accepted，但 error 说明缺了哪部分。"""

    via_temp: bool = False
    """这一批是不是走的「群临时会话」（``send_private_msg`` 带 ``group_id``）。

    只有插件侧的 :meth:`AstrBotMessenger._send_temp_session` 会置 True；引擎拿它
    判断这次出站该记成 ``group_temp`` 还是 ``private``。**不进** :meth:`to_row`：
    那是落库的事实字段，多一个键会让所有按固定键比对的存档迁移多一道噪音。
    """

    def __bool__(self) -> bool:
        return self.status in (SEND_ACCEPTED, SEND_DELIVERED)

    @property
    def ok(self) -> bool:
        return bool(self)

    def note(self, label: str = "") -> str:
        """给用户 / 日志看的中文说明（失败时说清是哪一段）。"""
        who = label or self.target or "会话"
        if self.partial:
            return f"发到 {who} 只成功了一部分：{self.error or '后面的没发出去'}"
        if self.status == SEND_FAILED:
            return f"发到 {who} 失败：{self.error or '平台没有接收'}"
        if self.status == SEND_UNKNOWN:
            return f"发到 {who} 的结果无法确认（不当作已送达）"
        return f"发到 {who} 已投出"

    def to_row(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "status": self.status,
            "platform_message_ids": list(self.platform_message_ids),
            "error": self.error,
            "segments": self.segments,
            "sent_segments": self.sent_segments,
            "attachments": self.attachments,
            "partial": self.partial,
            "operation_id": self.operation_id,
            "segment_receipts": [dict(item) for item in self.segment_receipts],
        }

    def pending_segments(self, messages: Any) -> list[str]:
        """这批话里**还没被平台收下**的那几段（重试只补这几段）。

        计划 §5 A2 的要求：重试不许把已经收下的段再发一遍。规则按回执里的
        ``sent_segments`` 算，不猜内容：

        - 整批都收下了（accepted / delivered 且 ``partial=False``）→ 空列表；
        - 只收下前 ``k`` 段（``partial=True``）→ 从第 ``k+1`` 段开始；
        - 明确失败才可重试；unknown 留待确认，不能盲发。
        - 有逐段回执时按实际 index 选择失败段，不假定成功段是前缀。
        """

        texts = [str(item) for item in (messages or []) if str(item).strip()]
        if not texts:
            return []
        if self.segment_receipts:
            failed = set()
            for item in self.segment_receipts:
                try:
                    index = int(item.get("index", -1))
                except (TypeError, ValueError):
                    continue
                if item.get("status") == SEND_FAILED and 0 <= index < len(texts):
                    failed.add(index)
            return [text for index, text in enumerate(texts) if index in failed]
        if self.status == SEND_UNKNOWN:
            return []
        if self.status in (SEND_ACCEPTED, SEND_DELIVERED) and not self.partial:
            return []
        try:
            sent = int(self.sent_segments or 0)
        except (TypeError, ValueError):
            sent = 0
        if sent <= 0:
            return texts
        return texts[sent:]


def as_receipt(
    value: Any, *, target: str = "", segments: int = 1, attachments: int = 0
) -> SendReceipt:
    """把旧适配器的 bool / 新回执 / 其它返回值统一成 :class:`SendReceipt`。

    兼容规则（计划 §5 A2）：旧返回 ``True`` → accepted，``False`` / ``None`` → failed；
    能确定消息 id 的适配器自己构造 ``SendReceipt`` 并标 delivered。
    """
    if isinstance(value, SendReceipt):
        if target and not value.target:
            value.target = target
        return value
    if value is True:
        return SendReceipt(
            target=target,
            status=SEND_ACCEPTED,
            segments=segments,
            sent_segments=segments,
            attachments=attachments,
        )
    if value is False or value is None:
        return SendReceipt(
            target=target,
            status=SEND_FAILED,
            segments=segments,
            error="平台没有接收",
            attachments=attachments,
        )
    # 其它真值（消息 id、平台返回对象）：按"收下了"记，不猜 delivered
    ok = bool(value)
    return SendReceipt(
        target=target,
        status=SEND_ACCEPTED if ok else SEND_FAILED,
        segments=segments,
        sent_segments=segments if ok else 0,
        attachments=attachments,
    )


@runtime_checkable
class MessagePort(Protocol):
    """向会话发送消息。"""

    async def send_text(self, session_id: str, messages: list[str]) -> SendReceipt | bool: ...

    async def send_record(self, session_id: str, audios: list[str]) -> SendReceipt | bool: ...
    """把语音发到会话里（合成语音的工具 / 指令生成的结果走这条路）。"""

    async def send_images(self, session_id: str, images: list[str]) -> SendReceipt | bool: ...
    """把图片发到会话里（工具 / 指令生成的结果图走这条路）。"""

    async def send_file(
        self, session_id: str, path: str, *, name: str = "", mime: str = ""
    ) -> SendReceipt | bool: ...
    """把**一个本机文件**当附件发到会话里（B4 的产物走这条路）。

    契约三条（实现方照做，B4 依赖它）：
    - ``path`` 是本机绝对路径；实现方自己确认它存在且是普通文件，不存在就回
      ``SendReceipt(status=SEND_FAILED)`` 并说明原因，**不许**悄悄发成一条空消息；
    - ``name`` 是给用户看的文件名（留空 = 用 ``path`` 的文件名），``mime`` 留空时
      由实现方按扩展名猜；
    - 返回值与 :meth:`send_images` 同一套口径：``accepted`` 不等于 ``delivered``，
      拿不准就 ``unknown``——**不许**把 unknown 说成成功。
    """

    async def set_group_card(self, session_id: str, card: str) -> CardResult: ...

    async def fetch_group_card(self, session_id: str) -> str: ...
    """读一下她当前在群里的名片（拿不到就返回空串）。"""

    async def poke(self, session_id: str, user_id: str) -> PokeResult: ...
    """戳一戳某个群友（QQ 系平台独有，失败时在 ``reason`` 里说明）。"""


@runtime_checkable
class CommandPort(Protocol):
    """把别的插件的指令转发出去（「指令触发」型动作用）。"""

    async def trigger(
        self, session_id: str, command: str, *, event: Any = None
    ) -> ToolCallResult: ...

    def session_with_context(self, candidates: list[str] | None = None) -> str:
        """最近收到过消息的那条会话：指令要借它的消息当上下文（拿不到返回空串）。"""

        ...


@runtime_checkable
class ToolPort(Protocol):
    """查询与调用 AstrBot 工具。"""

    def list_tools(self) -> list[ToolInfo]: ...

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult: ...


@runtime_checkable
class PersonaPort(Protocol):
    """读取会话当前人格。"""

    async def get_persona_text(self, session_id: str) -> str: ...


@runtime_checkable
class ClockPort(Protocol):
    """时间来源，便于测试注入固定时间。"""

    def now(self) -> float: ...

    def now_struct(self) -> Any: ...


# ===========================================================================
# B0 契约区（B 阶段新契约 —— 今天无人消费）
#
# 依据：`.vwtest/embedding-b-and-agent-capabilities-development.md` §11「B0：契约与观测」
# 与 `.vwtest/embedding-b-work-arrangement.md` 的 WP1。A 阶段不会用到这里的东西：
# 宿主适配层要到 B1 才实现它。数据结构（EmbeddingSpace / EmbeddingBatch）放在
# `core/models.py` 末尾的「B0 契约区」，这里只放端口协议。
# ===========================================================================

from .models import EmbeddingBatch, EmbeddingSpace  # noqa: E402


@runtime_checkable
class EmbeddingPort(Protocol):
    """向量化端口：把一批文本变成一个 :class:`EmbeddingBatch`（B0 只定契约，B1 才实现）。

    谁写：B1 的宿主适配层（把插件配置里选中的 embedding provider 包成这个端口）；
          B0 阶段只有测试替身 `tests/stub_ports.py` 的 `StubEmbedding` 实现它。
    谁读：B1 的索引队列与召回、B2/B3 的查询工具；**没配置 provider 时连实现都不注册**，
    调用方必须自己先判断「端口在不在」（WP4 的验收正是查「未配置时调用次数为 0」）。
    契约（照着实现，别自作主张）：
    - 成功：``ok=True``、``vectors`` 与 ``texts`` 一一对应且顺序不变、``space`` 是
      **实际**产出这批向量的空间（不是调用方想要的那个）；
    - 失败：**返回** ``ok=False`` 的批次并用 ``error`` 说清原因，不要靠抛异常表达
      「供应商报错 / 超时 / 配额用尽」—— 调用方要能把它当成普通结果处理，好决定降级；
    - **维度不符必须 ``ok=False``**，``error`` 里写清期望维度与实际维度，绝不能静默放行
      （静默放行 = 之后所有相似度都在算错的东西，而且没人知道）；
    - ``space.dim`` / ``provider_id`` 必须和实现真实一致：调用方会拿
      ``space.fingerprint`` 当缓存键、当库表分区键；
    - 空 ``texts`` 返回 ``ok=False``（调用方自己该先判空，别浪费一次请求）。
    ``provider_id`` 同时出现在参数和 ``space`` 里是**故意冗余**：参数是调用方点名的供应商
    （路由用），``space`` 是期望的向量空间（校验用）。两者不一致说明配置漂移，实现应当返回
    ``ok=False``，不许「按参数换一家、按 space 记元数据」。
    """

    async def embed(
        self,
        texts: list[str],
        *,
        provider_id: str,
        space: EmbeddingSpace,
    ) -> EmbeddingBatch:
        """把 ``texts`` 向量化；返回 :class:`EmbeddingBatch`（失败也走返回值，不抛异常）。"""

        ...


# ===========================================================================
# B0 / WP4 追加区：宿主 embedding 适配器
#
# 谁写：WP4（把宿主 ``EmbeddingProvider`` 包成 :class:`EmbeddingPort`）。
# 谁读：B1 的索引队列与召回；WP4 的 ``/{plugin}/embedding/status`` 端点借 ``get_dim``
#       探维度来算 ``EmbeddingSpace.fingerprint``。
# 规矩（与上面 :class:`EmbeddingPort` 的契约逐条对齐）：
# - **一切失败都走返回值**：供应商报错 / 超时 / 维度不符 / 拿到 NaN，全部变成
#   ``EmbeddingBatch.failure(中文原因)``，调用方永远不用 try；
# - **未配置 / 取不到实例时不碰宿主**：``provider is None`` 时一个宿主方法都不调，
#   ``provider_calls`` / ``attempts`` 保持 0（WP4 的验收查的就是这个数）；
# - **维度不符不许静默截断或补零**：先探 ``get_dim()``，与 ``space.dim`` 不符直接失败；
# - 只读：不落盘、不发消息、不改任何既有契约（既有类一个都没动）。
# ===========================================================================

import math  # noqa: E402
import time  # noqa: E402
from collections.abc import Sequence  # noqa: E402


async def _await_if_needed(value: Any) -> Any:
    """宿主方法有的返回协程、有的直接返回结果（各适配器写法不一），两种都要能用。"""

    if hasattr(value, "__await__"):
        return await value
    return value


def _vector_problem(vectors: Any, dim: int) -> str:
    """逐条逐分量体检：合法返回空串，否则返回中文原因（探针维度不合也归这里）。

    为什么不只靠 ``check_embedding_vectors``：那个校验为了判「全零」会在遇到第一个
    非零分量时提前收工（``core/models.py`` 里已冻结的写法），于是落在后面的 NaN
    就漏过去了。适配器是数据入口，这里**每个分量都看一遍**。
    """

    if isinstance(vectors, (str, bytes)) or not isinstance(vectors, Sequence):
        return f"vectors 需要「向量序列」，收到 {type(vectors).__name__}"
    if not vectors:
        return "embedding 结果是空的（供应商一条向量都没返回）"
    for index, vector in enumerate(vectors):
        if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
            return f"第 {index} 条不是向量（{type(vector).__name__}）"
        if len(vector) != int(dim):
            return (
                f"第 {index} 条向量维度 {len(vector)} 与空间声明的 {int(dim)} 不一致"
                "（不截断也不补零）"
            )
        for value in vector:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return f"第 {index} 条向量里有非数值分量 {value!r}"
            if not math.isfinite(number):
                return f"第 {index} 条向量含 NaN/Inf，不能入库也不能算相似度"
    return ""


class AstrBotEmbedding:
    """把宿主 ``EmbeddingProvider`` 包成 :class:`EmbeddingPort`（B0/WP4）。

    只读、无其它状态（除调用计数与最近一次错误）。``provider`` 允许是 ``None``
    （未配置 / 取不到实例），这时 :meth:`embed` 返回不可用语义的失败批次，
    **不调用任何宿主方法**。

    属性：
    - ``provider_calls``：宿主方法调用总次数（含 ``get_dim`` 探针；未配置时恒为 0）；
    - ``attempts``：真正发出去的向量化请求次数（探针不计；未配置时恒为 0）；
    - ``last_error``：最近一次失败的中文原因（成功一次就清空）。
    """

    #: 批量接口的参数（B0 钉死一处，免得调用点各写一套）
    BATCH_SIZE = 16
    TASKS_LIMIT = 3
    MAX_RETRIES = 3

    def __init__(self, provider: Any = None, *, provider_id: str = "") -> None:
        self.provider = provider
        self.provider_id = str(provider_id or "").strip()
        self.provider_calls = 0
        self.attempts = 0
        self.last_error = ""
        #: 维度是从哪儿问来的：``get_dim`` / ``provider_config`` / ``""``（还没问）
        self.dim_source = ""

    @property
    def available(self) -> bool:
        """宿主实例在不在（未配置 / 取不到实例 = False）。"""

        return self.provider is not None

    def unavailable_reason(self) -> str:
        """不可用时的中文原因；可用时返回空串（给状态端点用）。"""

        if self.provider is None:
            return "未配置 embedding 供应商（或取不到它的实例），语义检索不可用"
        return ""

    @staticmethod
    def _declared_dim(provider: Any) -> int:
        """供应商配置里自己填的维度（``provider_config["embedding_dimensions"]``）。

        为什么要有这条路：AstrBot 的 embedding 适配器都把维度存在这一项里
        （OpenAI / DashScope / Ollama / Gemini 都读它，见
        ``astrbot/core/provider/sources/openai_embedding_source.py`` 的 ``get_dim``），
        但**不是每个适配器都实现了 ``get_dim()``**。用户既然在供应商那边填了维度，
        就该用它——为了一句"拿不到 get_dim"把整个语义检索关掉，属于白关。
        """

        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return 0
        try:
            dim = int(config.get("embedding_dimensions"))
        except (TypeError, ValueError):
            return 0
        return dim if dim > 0 else 0

    @staticmethod
    def _can_embed(provider: Any) -> bool:
        """这个宿主实例有没有向量化能力（AstrBot 的 ``EmbeddingProvider`` 都实现它）。"""

        return callable(getattr(provider, "get_embedding", None))

    def get_dim(self) -> int:
        """探针式取维度：拿不到或非法就抛 :class:`ValueError`（中文原因）。

        顺序（用户 m26700 报过"供应商里明明填了维度"）：
        ``get_dim()`` → 供应商配置的 ``embedding_dimensions`` → 报错。

        **不许**在维度不符时静默截断或补零——那只会让之后所有相似度都算错；
        但"从配置里读到的维度"不是猜，是用户在宿主里亲口填的值。
        """

        if self.provider is None:
            raise ValueError("未配置 embedding 供应商（或取不到它的实例），问不到维度")
        name = type(self.provider).__name__
        getter = getattr(self.provider, "get_dim", None)
        if callable(getter):
            self.provider_calls += 1
            try:
                dim = int(getter())
            except Exception as exc:
                raise ValueError(f"宿主 get_dim() 调用失败：{exc}") from exc
            if dim > 0:
                self.dim_source = "get_dim"
                return dim
            # 有的适配器在没填维度时返回 0：这时再看配置里填的
            declared = self._declared_dim(self.provider)
            if declared:
                self.dim_source = "provider_config"
                return declared
            raise ValueError(f"宿主 get_dim() 返回了非法维度 {dim}（必须是正整数）")
        if not self._can_embed(self.provider):
            # 典型情形：把一个**聊天**供应商填成了 embedding 供应商（openai_chat_completion）。
            # 它既没有 get_dim() 也不会 get_embedding()，就算读了维度也算不出向量，
            # 所以这里给的是"该去哪儿改"，不是一句干巴巴的"问不到维度"。
            raise ValueError(
                f"宿主供应商 {name} 是聊天供应商（没有 get_dim()，也不能算向量）："
                "到 AstrBot「提供商」里新建一个 Embedding 类型的提供商"
                "（OpenAI Embedding / text-embedding-3-small / 维度 1536 之类），"
                "再到本插件的配置里把「embedding 供应商」指向它"
            )
        declared = self._declared_dim(self.provider)
        if declared:
            self.dim_source = "provider_config"
            return declared
        raise ValueError(
            f"宿主供应商 {name} 没有 get_dim()，配置里也没填 embedding_dimensions，"
            "无法确认向量维度（去 AstrBot 提供商里给这一项填上维度即可）"
        )

    async def embed(
        self,
        texts: list[str],
        *,
        provider_id: str,
        space: EmbeddingSpace,
    ) -> EmbeddingBatch:
        """向量化一批文本；**任何异常都不外抛**，最差也是 ``ok=False`` 的批次。"""

        started = time.monotonic()

        def failure(reason: str) -> EmbeddingBatch:
            self.last_error = str(reason)
            return EmbeddingBatch.failure(
                reason,
                space=space,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )

        try:
            # 1) 未配置 / 取不到实例：先失败，**一次宿主方法都不调**
            if self.provider is None:
                return failure(self.unavailable_reason())

            if isinstance(texts, (str, bytes)) or not isinstance(texts, (list, tuple)):
                return failure(f"texts 需要字符串列表，收到 {type(texts).__name__}")
            items = [str(item) for item in texts]
            if not items:
                return failure("texts 是空的，没有要向量化的内容（别浪费一次请求）")

            wanted = str(provider_id or "").strip()
            if not wanted:
                return failure("没有点名 embedding 供应商（provider_id 是空的）")
            if space is None:
                return failure("缺少 embedding 空间（provider_id/model/dim 未知）")
            declared = str(getattr(space, "provider_id", "") or "").strip()
            if wanted != declared:
                return failure(
                    f"点名的供应商 {wanted} 与向量空间申报的 {declared} 不一致"
                    "（配置漂移，不按任一方放行）"
                )
            if self.provider_id and self.provider_id != wanted:
                return failure(
                    f"适配器绑的是供应商 {self.provider_id}，这次却点名 {wanted}"
                    "（不是同一家，不能混用）"
                )

            # 2) 探针：先问维度，不符就明确报错——**不发请求、不截断、不补零**
            try:
                actual_dim = self.get_dim()
            except Exception as exc:
                return failure(str(exc))
            wanted_dim = int(getattr(space, "dim", 0) or 0)
            if actual_dim != wanted_dim:
                return failure(
                    f"宿主供应商 {wanted} 的向量维度 {actual_dim} 与空间 "
                    f"{space} 声明的 {wanted_dim} 不一致（不截断也不补零）"
                )

            # 3) 真的去要向量：优先宿主的批量接口，退而求其次用单条接口
            method_name = ""
            for name in ("get_embeddings_batch", "get_embeddings", "get_embedding"):
                if callable(getattr(self.provider, name, None)):
                    method_name = name
                    break
            if not method_name:
                return failure(
                    f"宿主供应商 {type(self.provider).__name__} 没有可用的向量化方法"
                )
            caller = getattr(self.provider, method_name)
            self.provider_calls += 1
            self.attempts += 1
            try:
                if method_name == "get_embeddings_batch":
                    raw = caller(
                        list(items),
                        batch_size=self.BATCH_SIZE,
                        tasks_limit=self.TASKS_LIMIT,
                        max_retries=self.MAX_RETRIES,
                    )
                    vectors = await _await_if_needed(raw)
                elif method_name == "get_embeddings":
                    vectors = await _await_if_needed(caller(list(items)))
                else:
                    vectors = [
                        await _await_if_needed(caller(text)) for text in items
                    ]
            except Exception as exc:
                return failure(
                    f"调用宿主 {method_name}() 失败：{type(exc).__name__}: {exc}"
                )

            # 4) 逐条逐分量体检（维度 / NaN / ±Inf / 空批次 / 非数值 / 条数），
            #    坏数据在这里就降级成失败批次，绝不带半截向量出去。
            problem = _vector_problem(vectors, wanted_dim)
            if problem:
                return failure(problem)
            batch = EmbeddingBatch.success(
                vectors,
                space,
                texts=items,
                latency_ms=(time.monotonic() - started) * 1000.0,
                usage={
                    "provider_id": wanted,
                    "method": method_name,
                    "texts": len(items),
                },
            )
            self.last_error = "" if batch.ok else batch.error
            return batch
        except Exception as exc:  # 兜底：契约要求「任何异常都转成失败批次」
            return failure(f"向量化失败（{type(exc).__name__}）：{exc}")
