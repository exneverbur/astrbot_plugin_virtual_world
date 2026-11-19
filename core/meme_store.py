"""表情包收藏的数据层（阶段 M1）：五态判定 + 记录管理 + 展示记录收尾。

这一层回答的是"**这条收藏现在到底能不能用**"，不是"该不该用"——后者是 M2 的
回复路径与闸门（见 :mod:`core.meme_settings`）的事。分开的理由：管理页要在
**总开关关着**的时候也能看清每一条的状态（文件在不在、被禁用了没有、记录还在不在），
而回复路径在总开关关着时连数据都不该碰。

五态（方案 §6）在这里落到一层薄薄的判定上，**关键是方向别搞反**：

* ``enabled``           记录在、没禁用、文件在、当前范围可用
* ``disabled``          记录在，但被停用了（文件还在）
* ``deleted``           记录被软删了，但**底层文件还在**
* ``missing``           记录在，但**底层文件不见了**（有人手工删了文件）
* ``scope_unavailable`` 记录在、文件在，但当前会话/来源范围用不了它

``deleted`` 与 ``missing`` 是两个相反方向的坏消息，回执文案也完全不同（"收藏记录
已经删了，底层文件还在" vs "文件不见了"），所以绝不能合并成一个"不可用"。

身份口径（§1）：资产身份 = 内容 sha256 = 资产库的 ``media_id``
（:func:`core.media_store.content_id`）。这个模块**不自己算哈希**，也不拿 URL / 平台
文件 id 当身份。

**M4 补的那一半（语义预选与软排序，方案 §5/§10）**：:meth:`MemeStore.preselect`
与 :meth:`MemeStore.soft_rank` 是**纯逻辑**——这一层自己绝不调模型、绝不发向量请求；
查询向量与候选向量都由调用方（引擎）交进来，本层只做"打几分、怎么排"。铁律是
**向量只用来排序/预选，绝不扩权**：停用 / 缺失 / 记录删了 / 没授权的条目在进
向量打分之前就被五态闸门挡住了（:meth:`MemeStore._eligible_rows`），向量再像也进不来。
没配 provider / 没有向量空间时**如实降级**（``coverage`` 里写清用了哪一档、为什么），
绝不假装语义可用。

**M5 补的那一半（资产事实与变体）**：:func:`asset_facts` 现在会真的去解码（Pillow，
可选依赖），把 ``width`` / ``height`` / ``frame_count`` / ``is_animated`` 读成真值；
:meth:`MemeStore.probe_media` / :meth:`MemeStore.backfill_asset_facts` 负责探测与补齐
既有行（幂等、只补空的、可 dry-run）；:meth:`MemeStore.find_variants` 按感知哈希 /
像素哈希 / 帧数差异登记**疑似变体**——只登记关系，**绝不合并条目**（方案 §1 铁律）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import meme_images
from .db import (
    MEME_ENTRY_STATUS_DELETED,
    MEME_ENTRY_STATUS_DISABLED,
    MEME_ENTRY_STATUS_ENABLED,
    MEME_EXPOSURE_DELIVERED,
    MEME_EXPOSURE_FAILED,
    MEME_PIN_REF_PREFIX,
    MEME_USAGE_ATTEMPTED,
    MEME_USAGE_SENT,
    build_occurrence_id,
    cleanup_meme_assets,
    cleanup_meme_exposures,
    cleanup_meme_usage,
    format_meme_id,
    get_meme_asset,
    get_meme_entry,
    list_meme_entries,
    list_meme_sources,
    list_meme_usage,
    list_meme_variants,
    parse_occurrence_id,
    unpack_vector,
)
from .embedding_index import rank_by_cosine
from .meme_settings import (
    AVAILABILITY_LABELS,
    AVAILABILITY_STATES,
    COOLDOWN,
    CONFIG_DISABLED,
    DAY_LIMITED,
    GATE_OK,
    RATE_LIMITED,
    SCOPE_ANY,
    SEND_LIMIT,
    STATE_DELETED,
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_MISSING,
    STATE_UNKNOWN,
    SCOPE_UNAVAILABLE,
    gate_reason_text,
    meme_scope_intersection,
    meme_settings,
)
from .ports import SEND_ACCEPTED, SEND_DELIVERED, SEND_UNKNOWN

# 记录层能报的三种状态（与五态里的前三个同名，另两个由文件/范围决定）
RECORD_STATES = (
    MEME_ENTRY_STATUS_ENABLED,
    MEME_ENTRY_STATUS_DISABLED,
    MEME_ENTRY_STATUS_DELETED,
)

# 入站图片统一用「图N」编号（方案 §12：不引入 image_N）
IMAGE_REF_PREFIX = "图"

# 她**图库里**的候选预览另起一套编号（用户 2026-10-23，问题4）：候选图不是对方
# 发来的图，绝不能和「图N」共用前缀，否则主模型会把"自己收藏的图"当成"收到的图"。
CANDIDATE_REF_PREFIX = "候选"

# 三个表情包动作：``collect_meme`` 是 M2 的，``find_meme`` / ``send_meme`` 是 M3 的。
# 它们**都不在世界动作表里**（老安装的 world.json 里永远不会有），所以由引擎按总开关
# 自己收口（见 ``engine._meme_action_ids``）。
COLLECT_ACTION_ID = "collect_meme"
SEARCH_ACTION_ID = "find_meme"
SEND_ACTION_ID = "send_meme"
#: 攒图评审（用户 2026-10-24）：一次调用决定这一批候选收哪些、一个不收也行。
#: 它只在评审那一次的内部调用里出现，主回复轮里没有（见 ``core/meme_collect.py``）。
COLLECT_BATCH_ACTION_ID = "collect_meme_batch"
#: **不进** ``MEME_ACTION_IDS``：那是"主回复轮可解析的动作"，批量收藏只活在评审那一次
#: 内部调用里，进了解析白名单反而会让主回复轮也能写它（写出来没人执行）。
MEME_ACTION_IDS: tuple[str, ...] = (
    COLLECT_ACTION_ID,
    SEARCH_ACTION_ID,
    SEND_ACTION_ID,
)

# 展示记账的阶段名：一次"出现"可以分几个阶段提交给模型（M2 只有主回复这一段）
PHASE_REPLY = "reply"
#: M3 的另一相位：这一轮的内部续接（``find_meme`` 的候选预览进的就是这一次请求）。
#: 它不写 ``image_exposures``（候选不是"别人发的某一次出现"，没有 occurrence 坐标），
#: 留着这个常量是为了让"相位"这件事在日志与后续 M4 预选里有一个统一的名字。
PHASE_SEARCH = "search"

# ---- 检索口径（M3 只做关键词；语义那半是 M4，绝不在这里偷偷实现向量）----
SEARCH_MODE_KEYWORD = "keyword"
#: 中间档：关键词打底 + 向量软排（``retrieval_mode`` 是 ``auto`` 时走它）。
SEARCH_MODE_KEYWORD_SEMANTIC = "keyword_semantic"
#: 纯向量预选 + 关键词复核（``retrieval_mode = embedding`` 时走它）。
SEARCH_MODE_EMBEDDING = "embedding"
SEARCH_OK = "ok"
SEARCH_NO_MATCH = "no_match"
SEARCH_UNAVAILABLE = "unavailable"
SEARCH_BUDGET_EXHAUSTED = "search_budget_exhausted"

# ---- M4：语义这一档跑到哪一步了（``coverage["semantic"]`` 的取值）----
#: 这一档就没打算跑（``retrieval_mode = keyword``）。
SEMANTIC_OFF = "off"
#: **M3 遗留字面量**。M3 的 ``coverage["semantic"]`` 用它表示"语义还没接上"，
#: 那条契约不许破（``tests/test_meme_send.py::test_semantic_mode_is_reported_as_pending_not_done``），
#: 所以 M4 沿用这个字符串，但把含义收紧成"**这次调用没拿到语义通道**"：
#: 具体是没给空间 / 没给查询向量 / 索引里没有向量，一律写在 ``semantic_reason`` 里，
#: 绝不拿它冒充"语义跑过了"。
SEMANTIC_PENDING = "pending_m4"
#: 真用上了向量（有多少条参与打分看 ``semantic_scored``）。
SEMANTIC_USED = "used"
#: 想用但用不上，已如实退回关键词（原因见 ``semantic_reason``）。
SEMANTIC_DEGRADED = "degraded"
#: 用户点名要的语义根本没法用（比如强制 ``embedding`` 又没配服务商）→ 直接报不可用。
SEMANTIC_UNAVAILABLE = "unavailable"

#: 语义档位用不上的原因码 → 中文（``coverage["semantic_reason_text"]``，页面/日志共用）
SEMANTIC_REASON_LABELS: dict[str, str] = {
    "": "语义通道可用",
    "no_channel": "调用方没给语义通道（查询向量 / 候选向量）",
    "no_space": "没有向量空间（不知道是哪个 provider / 模型 / 维度）",
    "no_provider": "没配嵌入服务商",
    "no_query_vector": "这一句没有查询向量",
    "no_vectors": "候选里一条向量都没有（索引还没建出来）",
    "no_registry": "库里还没有 meme_vectors 注册表（M4 的表待加）",
    "registry_error": "读向量注册表出错",
    "space_mismatch": "向量的空间与当前空间对不上（换过 provider / 模型 / 维度）",
    "disabled": "总开关关着",
    "keyword_mode": "检索口径配成纯关键词",
}

#: 软排序（中间档）的默认权重。**关键词是主**：向量的权重刻意小于关键词，
#: 于是"关键词命中很强"的候选不会被一个只在语义上沾边的候选挤掉（方案 §5）。
SOFT_RANK_WEIGHTS: dict[str, float] = {
    "keyword": 1.0,
    "vector": 0.5,
    "recency": 0.2,
    "usage": 0.15,
    "preference": 0.1,
}

#: 纯向量档（``embedding``）的权重：向量是主，关键词只用来复核与并列时的取舍。
EMBEDDING_RANK_WEIGHTS: dict[str, float] = {
    "keyword": 0.35,
    "vector": 1.0,
    "recency": 0.15,
    "usage": 0.1,
    "preference": 0.1,
}

#: 关键词原始分 → [0,1] 的饱和点：命中"名称 3 分 + 含义 2.5 分"已经很强，
#: 12 分以上一律当满分（不设这个上限的话，长语义条目会靠字数压死短条目）。
KEYWORD_SCORE_SATURATION = 12.0

#: 最近使用次数 → [0,1] 的饱和点：成功发出去 3 次就算"她常用的"。
USAGE_SATURATION = 3.0

#: 时间衰减的半衰期（天）：14 天前的收藏，时间这一项的权重减半。
RECENCY_HALF_LIFE_DAYS = 14.0

#: 纯向量档的相似度下限（余弦）。出处：短文本 embedding 的余弦分布经验——
#: 同义改写一般 0.6 以上、不相关多在 0.1~0.3，取 **0.30** 作为保守下界；
#: 中间档（软排）**不设下限**（软的：像不像只影响排序，不决定去留）。
SEMANTIC_MIN_SCORE = 0.30

#: 一次语义预选最多看多少条候选（与 :meth:`MemeStore.search` 的全表上限同量级）。
MAX_SEMANTIC_POOL = 1000

#: 一次软排序最多为多少条去数"最近用过几次"（每条一次小查询，别让它变成 N+1 爆炸）。
USAGE_LOOKUP_LIMIT = 60

#: 变体扫描的配对上限（O(n²) 的护栏；超了如实截断，不装作扫完了）。
MAX_VARIANT_PAIRS = 4000

#: 默认关联的 `meme_vectors` 注册表读取函数名（DDL 与函数在报告"待加清单"里）。
MEME_VECTOR_LIST_FN = "list_meme_vectors_for_entries"

#: 检索查询词切成多少个 token（够用就行：切太多等于把每个字都拿去撞字段）
QUERY_TOKEN_LIMIT = 24

#: 每个字段的权重：名称/含义最像"她想表达什么"，编号是硬指认，笔记与标签是补充。
MATCH_FIELD_WEIGHTS: dict[str, float] = {
    "entry_id": 6.0,
    "name": 3.0,
    "meaning": 2.5,
    "use_when": 2.0,
    "personal_note": 1.5,
    "tags": 1.5,
    # **负向**（用户 2026-11-13）：``avoid_when`` 写的是"什么时候别用这张"，
    # 命中它说明这张图恰恰不该发。以前它按 +1.0 算，等于"越不该用越靠前"，
    # 实测输入「不想撒娇」时 15 条"撒娇"图全部命中、排在最前。
    "avoid_when": -2.0,
}

#: 字段 -> 中文（"为什么像"那句话与日志共用一份）
MATCH_FIELD_LABELS: dict[str, str] = {
    "name": "名称",
    "meaning": "含义",
    "use_when": "什么时候用",
    "avoid_when": "什么时候别用",
    "personal_note": "她的笔记",
    "tags": "标签",
    "entry_id": "编号",
}

#: 配额窗口：小时配额用滚动一小时，按天配额由引擎给"世界本地日零点"（见下）
HOUR_SECONDS = 3600.0
DAY_SECONDS = 86400.0

#: 一条候选默认最多带几张预览（真正的上限是配置里的 max_candidate_images_per_prompt）
DEFAULT_CANDIDATE_LIMIT = 5

#: 一个 ``claimed`` 超过这么久没被收尾（delivered/failed）就当过期认领：
#: 认领之后请求半路夭折（睡前门禁 / 被中断 / 限流提前返回）不该永久钉死这一次出现。
CLAIM_STALE_SECONDS = 120.0

# 预算砍图的原因码（coverage 里如实回报，不闷掉）
DROP_OVER_IMAGE_MAX = "over_image_max"
DROP_OVER_MEME_MAX = "over_meme_image_max"
DROP_OVER_CANDIDATE_LIMIT = "over_candidate_limit"
DROP_PROMPT_LIMIT = "prompt_limit"
DROP_ASSET_TOO_LARGE = "asset_too_large"
DROP_PREVIEW_TOO_LARGE = "preview_too_large"
DROP_PREVIEW_BYTES = "preview_bytes"

#: 原因码 → 中文（coverage 文案与日志共用）
BUDGET_DROP_LABELS = {
    DROP_OVER_IMAGE_MAX: "超出本轮直接附图上限",
    DROP_OVER_MEME_MAX: "超出每轮补附上限",
    DROP_OVER_CANDIDATE_LIMIT: "超出候选预览上限",
    DROP_PROMPT_LIMIT: "超出本轮图片总数上限",
    DROP_ASSET_TOO_LARGE: "单张超过单图字节上限",
    DROP_PREVIEW_TOO_LARGE: "预览图超过预览单图上限",
    DROP_PREVIEW_BYTES: "预览总字节不够",
}

__all__ = [
    "RECORD_STATES",
    "BUDGET_DROP_LABELS",
    "CLAIM_STALE_SECONDS",
    "CANDIDATE_REF_PREFIX",
    "COLLECT_ACTION_ID",
    "DAY_SECONDS",
    "DEFAULT_CANDIDATE_LIMIT",
    "EMBEDDING_RANK_WEIGHTS",
    "HOUR_SECONDS",
    "IMAGE_REF_PREFIX",
    "KEYWORD_SCORE_SATURATION",
    "MATCH_FIELD_LABELS",
    "MATCH_FIELD_WEIGHTS",
    "MAX_SEMANTIC_POOL",
    "MAX_VARIANT_PAIRS",
    "MEME_ACTION_IDS",
    "MEME_VECTOR_LIST_FN",
    "PHASE_REPLY",
    "PHASE_SEARCH",
    "QUERY_TOKEN_LIMIT",
    "RECENCY_HALF_LIFE_DAYS",
    "SEARCH_ACTION_ID",
    "SEARCH_BUDGET_EXHAUSTED",
    "SEARCH_MODE_EMBEDDING",
    "SEARCH_MODE_KEYWORD",
    "SEARCH_MODE_KEYWORD_SEMANTIC",
    "SEARCH_NO_MATCH",
    "SEARCH_OK",
    "SEARCH_UNAVAILABLE",
    "SEMANTIC_DEGRADED",
    "SEMANTIC_MIN_SCORE",
    "SEMANTIC_OFF",
    "SEMANTIC_PENDING",
    "SEMANTIC_REASON_LABELS",
    "SEMANTIC_UNAVAILABLE",
    "SEMANTIC_USED",
    "SEND_ACTION_ID",
    "SOFT_RANK_WEIGHTS",
    "USAGE_LOOKUP_LIMIT",
    "USAGE_SATURATION",
    "MemeStore",
    "SemanticChannel",
    "allocate_image_budget",
    "availability_label",
    "budget_coverage_text",
    "collect_receipt",
    "format_candidate_ref",
    "format_image_ref",
    "label_candidates",
    "match_why",
    "meme_availability",
    "meme_pin_ref",
    "occurrence_id_for",
    "parse_image_ref",
    "query_tokens",
    "search_receipt",
    "semantic_channel",
    "send_rejection_receipt",
    "send_result_receipt",
    "soft_rank_candidates",
]


def meme_availability(
    entry: Mapping[str, Any] | None,
    *,
    file_present: bool,
    scope_allowed: bool = True,
) -> str:
    """一条收藏当前的可用状态（:data:`core.meme_settings.AVAILABILITY_STATES` 之一）。

    参数里没有"去读文件"这一步：``file_present`` 由调用方给（资产库 / 文件系统），
    于是这个函数是**纯函数**，能在测试里把五种状态都摆出来。

    判定顺序很重要：

    1. 文件不在 → ``missing``（**最先判**：文件是这条收藏的物理存在，文件没了，
       记录是什么状态都无从落地）；
    2. 范围不允许 → ``scope_unavailable``（与记录状态无关，范围是硬门槛）；
    3. 记录不在（或已被软删）→ ``deleted``（"记录没了但底层文件还在"这一态）；
    4. 被停用 → ``disabled``；
    5. 都过 → ``enabled``。

    于是"记录在 + 文件在"的每一种记录状态都能单独看出来：``enabled`` /
    ``disabled`` / ``deleted`` 三态都可达，加上 ``missing`` 与
    ``scope_unavailable`` 正好五态。
    """

    if not file_present:
        return STATE_MISSING
    if not scope_allowed:
        return SCOPE_UNAVAILABLE
    if entry is None:
        return STATE_DELETED
    status = str(entry.get("status") or "").strip()
    if status == MEME_ENTRY_STATUS_DELETED:
        return STATE_DELETED
    if status == MEME_ENTRY_STATUS_DISABLED:
        return STATE_DISABLED
    return STATE_ENABLED


def availability_label(state: str) -> str:
    """可用状态 → 中文（页面与回执共用一份）。"""

    return AVAILABILITY_LABELS.get(state, state or "")


def occurrence_id_for(session_id: Any, seq: Any, index: Any = 0) -> str:
    """一次"出现"的 ID（``"{session}#{seq}#{index}"``）。

    别人重发同一张图 = 新的 ``seq`` = **新的 occurrence**（收藏实体那边仍按
    ``media_id`` 去重）：所以这里刻意不让调用方"按图片去重"。
    """

    return build_occurrence_id(session_id, seq, index)


def format_image_ref(index: Any) -> str:
    """图片序号 → 编号（``1`` → ``"图1"``；非正数给空串）。"""

    try:
        number = int(index)
    except (TypeError, ValueError):
        return ""
    return f"{IMAGE_REF_PREFIX}{number}" if number > 0 else ""


def format_candidate_ref(index: Any) -> str:
    """候选序号 → 编号（``1`` → ``"候选1"``；非正数给空串）。"""

    try:
        number = int(index)
    except (TypeError, ValueError):
        return ""
    return f"{CANDIDATE_REF_PREFIX}{number}" if number > 0 else ""


def parse_image_ref(value: Any) -> int:
    """模型给的 ``image_ref`` → 序号（``"图2"`` / ``"第 2 张"`` / ``2`` 都认）。

    解析不出来返回 ``0``——调用方**必须**按"解析不到"明确失败，不猜不编
    （方案 §6：``image_ref`` 只有照抄本轮清单里那一个合法来源）。
    """

    text = str(value or "").strip()
    if not text:
        return 0
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        return 0
    try:
        number = int(digits)
    except ValueError:  # pragma: no cover - 上面已经限定成数字了
        return 0
    return number if number > 0 else 0


def meme_pin_ref(entry_id: Any) -> str:
    """收藏记录在资产库上的 pin 引用（``meme_0042`` → ``"meme:meme_0042"``）。

    钉住的意义：资产库 GC 只淘汰没人引用的文件，收藏指向的图不能被它顺手删掉
    （M1 漏掉的那一步，M2 补上）。
    """

    key = str(entry_id or "").strip()
    return f"{MEME_PIN_REF_PREFIX}{key}" if key else ""


def asset_facts(
    media_store: Any,
    media_id: Any,
    *,
    probe: bool = True,
    max_bytes: int = meme_images.DEFAULT_MAX_BYTES,
    max_frames: int = meme_images.DEFAULT_MAX_FRAMES,
) -> dict[str, Any]:
    """资产库里的实情 → ``meme_assets`` 那一行要的字段（取不到就只给身份）。

    收藏分两条路进来（聊天里收下、用户在管理页上传），两边都要给 ``meme_assets``
    填一行，否则卡片与详情里的「类型 / 大小 / 动图」全是空的——这条路径原先一个
    调用点都没有（``MemeStore.collect`` 只在拿到 ``asset=`` 时才写），M2 遗留。

    **M5 起真的去解码**（``probe=True``，Pillow 可选依赖）：从资产库读出字节，
    把 ``width`` / ``height`` / ``frame_count`` / ``is_animated`` 读成真值——
    M1~M4 期间这三列恒为 0、动图只能按 ``image/gif`` 猜。规矩：

    * 只有**真探测成功**才写 width/height/frame_count；探不到（没装 Pillow / 字节
      取不出来 / 坏图 / 截断 / 超大）就**保持老口径**（只给 mime + size +
      按 mime 猜的 ``is_animated``），一个尺寸都不编（方案 §1）；
    * 探测成功时 ``is_animated`` 用**真帧数**（``frame_count > 1``），不再看扩展名——
      WebP 动图与多帧 PNG 从此也能认出来；
    * 探测失败的原因不塞进这份 facts（调用方要原因就调
      :meth:`MemeStore.probe_media`，那里给 ``ok=False`` + 原因码）。
    """

    facts: dict[str, Any] = {"content_hash": str(media_id or "")}
    key = str(media_id or "").strip()
    if not key:
        return facts
    try:
        manifest = media_store.manifest() if media_store is not None else {}
    except Exception:
        manifest = {}
    mime = ""
    asset = manifest.get(key) if isinstance(manifest, dict) else None
    if isinstance(asset, Mapping):
        mime = str(asset.get("mime") or "")
        facts["mime"] = mime
        facts["size_bytes"] = int(asset.get("size") or 0)
        # 谁把它收进来的记在 meme_sources.origin_kind 上，这里不重复记标签
        facts["is_animated"] = mime == "image/gif"
    if probe:
        probed = _probe_media_bytes(
            media_store, key, max_bytes=max_bytes, max_frames=max_frames
        )
        if probed.get("ok"):
            facts["width"] = int(probed.get("width") or 0)
            facts["height"] = int(probed.get("height") or 0)
            facts["frame_count"] = int(probed.get("frame_count") or 0)
            facts["is_animated"] = bool(probed.get("is_animated"))
            if not mime and probed.get("mime"):
                facts["mime"] = str(probed.get("mime"))
            if "size_bytes" not in facts and probed.get("size_bytes"):
                facts["size_bytes"] = int(probed.get("size_bytes") or 0)
    return facts


def _probe_media_bytes(
    media_store: Any,
    media_id: str,
    *,
    max_bytes: int = meme_images.DEFAULT_MAX_BYTES,
    max_frames: int = meme_images.DEFAULT_MAX_FRAMES,
    hashes: bool = False,
) -> dict[str, Any]:
    """资产库字节 → :func:`core.meme_images.probe_bytes` 的事实（**只读**）。

    单独抽出来是因为两个调用点（``asset_facts`` 与 ``MemeStore.probe_media``）
    的输入形状不同，但"字节从哪来、拿不到怎么办"必须只有一份：拿不到字节时回
    ``ok=False`` + ``no_bytes``，绝不当成"零尺寸的图"。
    """

    if media_store is None:
        return {
            "ok": False,
            "reason": "no_bytes",
            "reason_text": meme_images.PROBE_REASON_TEXTS["no_bytes"],
            "media_id": str(media_id or ""),
        }
    reader = getattr(media_store, "open_bytes", None)
    data = None
    if callable(reader):
        try:
            data = reader(str(media_id or ""))
        except Exception:
            data = None
    if data is None:
        return {
            "ok": False,
            "reason": "no_bytes",
            "reason_text": meme_images.PROBE_REASON_TEXTS["no_bytes"],
            "media_id": str(media_id or ""),
        }
    facts = meme_images.probe_bytes(
        data, max_bytes=max_bytes, max_frames=max_frames, hashes=hashes
    )
    facts["media_id"] = str(media_id or "")
    return facts


def _image_size(item: Mapping[str, Any]) -> int:
    """一条候选图占多少字节（没写就是 0 = 不参与字节判断）。

    允许传裸的地址字符串（``"http://…"``）：那种情况下"大小未知"，按 0 算，
    绝不因为取不到尺寸就把图砍掉。
    """

    if not isinstance(item, Mapping):
        return 0
    for name in ("size_bytes", "bytes", "size"):
        if name in item:
            try:
                value = int(item.get(name) or 0)
            except (TypeError, ValueError):
                value = 0
            return max(0, value)
    return 0


def _record_drop(
    dropped: list[dict[str, Any]], item: Mapping[str, Any], tier: str, reason: str
) -> None:
    url = ""
    media_id = ""
    if isinstance(item, Mapping):
        url = str(item.get("url") or "")
        media_id = str(item.get("media_id") or "")
    else:
        url = str(item or "")
    dropped.append(
        {
            "tier": tier,
            "reason": reason,
            "reason_label": BUDGET_DROP_LABELS.get(reason, reason),
            "url": url,
            "media_id": media_id,
        }
    )


def allocate_image_budget(
    primary: Any = (),
    overflow: Any = (),
    previews: Any = (),
    *,
    image_max: Any = 3,
    meme_image_max: Any = 0,
    max_candidate_images_per_prompt: Any = 0,
    max_prompt_images: Any = 10,
    max_asset_bytes: Any = 0,
    max_preview_bytes: Any = 0,
) -> dict[str, Any]:
    """统一图片预算分配器（方案 §4）：一个池子按优先级**逐级**消费。

    优先级（**前一级不可被后一级挤掉**）:

    1. ``primary``  当前用户/发送范围内的真实图片（≤ ``image_max``）；
    2. ``overflow`` 超限补附进来的表情包（≤ ``meme_image_max``）；
    3. ``previews`` 收藏候选预览（≤ ``max_candidate_images_per_prompt``，M4 才会用）。

    三级共用 ``max_prompt_images`` 张数上限、``max_asset_bytes`` 单图字节
    （直接附图与补附用原文件）与 ``max_preview_bytes`` 预览总字节。

    返回 ``{"primary", "overflow", "previews", "selected", "dropped", "coverage"}``：
    ``selected`` 是三级按优先级拼起来的最终入选列表（**顺序按级**，调用方要按
    时间序重排后自己再编号）；``dropped`` 逐条给原因码，``coverage`` 给每级的
    请求数/保留数——预算不够时如实回报，不假装没事。
    """

    primary_items = list(primary or ())
    overflow_items = list(overflow or ())
    preview_items = list(previews or ())

    image_cap = max(0, int(image_max or 0))
    meme_cap = max(0, int(meme_image_max or 0))
    candidate_cap = max(0, int(max_candidate_images_per_prompt or 0))
    prompt_cap = max(0, int(max_prompt_images or 0))
    asset_cap = max(0, int(max_asset_bytes or 0))
    preview_bytes_cap = max(0, int(max_preview_bytes or 0))

    dropped: list[dict[str, Any]] = []
    kept_primary: list[Any] = []
    kept_overflow: list[Any] = []
    kept_previews: list[Any] = []

    for item in primary_items:
        size = _image_size(item)
        if len(kept_primary) >= image_cap:
            _record_drop(dropped, item, "primary", DROP_OVER_IMAGE_MAX)
        elif asset_cap and size > asset_cap:
            _record_drop(dropped, item, "primary", DROP_ASSET_TOO_LARGE)
        else:
            kept_primary.append(item)

    room = max(0, prompt_cap - len(kept_primary))
    for item in overflow_items:
        size = _image_size(item)
        if len(kept_overflow) >= meme_cap:
            _record_drop(dropped, item, "overflow", DROP_OVER_MEME_MAX)
        elif len(kept_overflow) >= room:
            _record_drop(dropped, item, "overflow", DROP_PROMPT_LIMIT)
        elif asset_cap and size > asset_cap:
            _record_drop(dropped, item, "overflow", DROP_ASSET_TOO_LARGE)
        else:
            kept_overflow.append(item)

    room = max(0, prompt_cap - len(kept_primary) - len(kept_overflow))
    preview_bytes = 0
    for item in preview_items:
        size = _image_size(item)
        if len(kept_previews) >= candidate_cap:
            _record_drop(dropped, item, "previews", DROP_OVER_CANDIDATE_LIMIT)
        elif len(kept_previews) >= room:
            _record_drop(dropped, item, "previews", DROP_PROMPT_LIMIT)
        elif preview_bytes_cap and size > preview_bytes_cap:
            _record_drop(dropped, item, "previews", DROP_PREVIEW_TOO_LARGE)
        elif preview_bytes_cap and preview_bytes + size > preview_bytes_cap:
            _record_drop(dropped, item, "previews", DROP_PREVIEW_BYTES)
        else:
            kept_previews.append(item)
            preview_bytes += size

    coverage = {
        "primary": {
            "offered": len(primary_items),
            "kept": len(kept_primary),
            "dropped": len(primary_items) - len(kept_primary),
        },
        "overflow": {
            "offered": len(overflow_items),
            "kept": len(kept_overflow),
            "dropped": len(overflow_items) - len(kept_overflow),
        },
        "previews": {
            "offered": len(preview_items),
            "kept": len(kept_previews),
            "dropped": len(preview_items) - len(kept_previews),
        },
        "prompt_limit": prompt_cap,
        "image_max": image_cap,
        "meme_image_max": meme_cap,
        "max_candidate_images_per_prompt": candidate_cap,
        "used": len(kept_primary) + len(kept_overflow) + len(kept_previews),
        "preview_bytes": preview_bytes,
    }
    return {
        "primary": kept_primary,
        "overflow": kept_overflow,
        "previews": kept_previews,
        "selected": [*kept_primary, *kept_overflow, *kept_previews],
        "dropped": dropped,
        "coverage": coverage,
    }


def budget_coverage_text(coverage: Mapping[str, Any] | None) -> str:
    """coverage → 一句话（日志/``note_vision`` 的 detail 与测试断言共用）。"""

    data = dict(coverage or {})
    primary = dict(data.get("primary") or {})
    overflow = dict(data.get("overflow") or {})
    previews = dict(data.get("previews") or {})
    text = (
        f"图片预算：直接附图 {int(primary.get('kept') or 0)}/{int(primary.get('offered') or 0)}，"
        f"补附 {int(overflow.get('kept') or 0)}/{int(overflow.get('offered') or 0)}，"
        f"候选预览 {int(previews.get('kept') or 0)}/{int(previews.get('offered') or 0)}"
        f"（总数上限 {int(data.get('prompt_limit') or 0)}）"
    )
    return text


def collect_receipt(
    *,
    state: Any,
    entry_id: Any = "",
    name: Any = "",
    created: bool = False,
    reason: Any = "",
) -> str:
    """收藏的分态回执（方案 §6）：**先判文件与范围，再判记录状态**。

    内容去重 ≠ 收藏可用 ≠ 授权：所以"文件在但记录删了""记录在但被停用""文件
    不见了"必须回不同的话，不能糊成一句"收不了"。
    """

    key = str(entry_id or "").strip()
    title = str(name or "").strip()
    suffix = f"（名称：{title}）" if title else ""
    current = str(state or "").strip()
    if current == STATE_ENABLED:
        if created:
            return f"收下了，编号 {key}{suffix}。"
        return f"这张已经收过了，编号 {key}{suffix}，没有再收一遍。"
    if current == STATE_DISABLED:
        return f"这张之前收过（{key}），但现在被停用了，这次没有动它。"
    if current == STATE_DELETED:
        return "底层文件还在，但收藏记录已经删了，这次没有新建。"
    if current == STATE_MISSING:
        return "这张图的底层文件不见了，收不了。"
    if current == SCOPE_UNAVAILABLE:
        return "这张图在当前的会话/来源范围里收不了。"
    if str(reason or "") == "no_media":
        return "这张图没有可用的内容身份（插件没能算出它的内容哈希），收不了。"
    return f"这次没有收成（{str(reason or '原因不明')}）。"


# --------------------------------------------------------------------------- #
# M3 检索：查询词切分、字段打分、候选编号（全是纯函数，测试直接摆数据）
# --------------------------------------------------------------------------- #

#: 切词用的分隔符（中英文标点与空白）：查询词是人话，按标点切开就够。
#: **不引分词库**——新依赖是红线，中文用 2 字滑窗补召回。
_QUERY_SEPARATORS = frozenset(
    " \t\r\n,，、。;；:：/\\|!！?？()（）[]【】{}<>\"'“”‘’「」『』…—-~·+*#@&_=^"
)


def query_tokens(intent: Any, *, limit: int = QUERY_TOKEN_LIMIT) -> list[str]:
    """一句人话 → 检索 token（中文按 2 字滑窗，英文按整词，全部小写）。

    为什么要滑窗：中文没有空格，"想撒娇的时候"整句拿去撞收藏里的"撒娇"撞不上，
    切成 2 字片段才有召回。片段只有两三个字，撞错了也不致命——权重最高的字段是
    名称 / 含义 / 编号，噪声片段通常撞不上它们。

    单字 token（比如"猫"）只在整句切完什么都不剩时才留：单字太宽，
    列表里会全是"猫"字开头那张，等于没检索。
    """

    text = " ".join(str(intent or "").split()).strip().lower()
    if not text:
        return []
    segments: list[str] = []
    current = ""
    for char in text:
        if char in _QUERY_SEPARATORS:
            if current:
                segments.append(current)
                current = ""
            continue
        current += char
    if current:
        segments.append(current)
    tokens: list[str] = []
    for segment in segments:
        if len(segment) >= 2:
            tokens.append(segment)
        for index in range(len(segment) - 1):
            tokens.append(segment[index : index + 2])
    if not tokens:
        tokens = [text]
    wanted = max(1, int(limit))
    unique: dict[str, None] = {}
    for token in tokens:
        if token and token not in unique:
            unique[token] = None
        if len(unique) >= wanted:
            break
    return list(unique)


def _match_row(
    row: Mapping[str, Any], tokens: Any
) -> tuple[float, list[dict[str, Any]]]:
    """一条收藏与查询词的匹配分与命中明细（纯函数）。

    打分只看"命中几个不同的 token × 字段权重"：命中越多、字段越像"她想表达什么"
    （名称 / 含义 > 用途 > 笔记标签）分越高；``avoid_when`` 命中是**减分**（那张图
    正是"这时候别用"的）。**不做同义词、不做向量**。
    """

    fields = (
        ("name", str(row.get("name") or "")),
        ("meaning", str(row.get("meaning") or "")),
        ("use_when", str(row.get("use_when") or "")),
        ("avoid_when", str(row.get("avoid_when") or "")),
        ("personal_note", str(row.get("personal_note") or "")),
        # 两种输入形状都要认：库里的行叫 ``asset_tags``，已经拼好的候选叫 ``tags``
        # （M4 的预选会把"候选条目"再喂进来复核一遍，不认这个别名就会丢标签分）
        ("tags", str(row.get("asset_tags") or row.get("tags") or "")),
        ("entry_id", str(row.get("entry_id") or "")),
    )
    score = 0.0
    hits: list[dict[str, Any]] = []
    for key, raw in fields:
        text = raw.strip().lower()
        if not text:
            continue
        matched = list(
            dict.fromkeys(
                str(token) for token in list(tokens or ()) if str(token) and str(token) in text
            )
        )
        if not matched:
            continue
        weight = MATCH_FIELD_WEIGHTS.get(key, 1.0)
        score += weight * len(matched)
        hits.append(
            {
                "field": key,
                "label": MATCH_FIELD_LABELS.get(key, key),
                "tokens": matched,
                "weight": weight,
                "negative": weight < 0,
            }
        )
    return score, hits


def match_why(hits: Any) -> str:
    """命中明细 → 一句话"为什么像"（候选清单里每条都带）。"""

    parts: list[str] = []
    for hit in list(hits or ())[:3]:
        if not isinstance(hit, Mapping):
            continue
        shown = "、".join(
            f"「{token}」" for token in list(hit.get("tokens") or ())[:2]
        )
        if shown:
            parts.append(f"{str(hit.get('label') or '')}里有{shown}")
    return f"为什么像：{'；'.join(parts)}" if parts else ""


def label_candidates(candidates: Any, *, attached: Any = ()) -> list[dict[str, Any]]:
    """给候选编「候选N」：**只有真进了预览的那几条才有编号**（方案 §4）。

    ``attached`` 是真正进了这次请求的 ``entry_id`` 集合（第③级预算留下来的那些）。
    没编号的候选照旧列出来（靠真实编号 ``meme_0xx`` 指认），只是不能写「候选N」——
    "没真正进入模型请求的图不给编号"这条对候选预览同样成立。

    **前缀是「候选」不是「图」**（用户 2026-10-23，问题4）：候选图来自她自己的
    图库、对方根本没发过，和用户附来的「图N」共用前缀会让模型把两者混成一件事。
    编号按候选顺序连续给：``候选1`` 就是拼图/清单里的第一张。
    """

    wanted = {str(item) for item in list(attached or ()) if str(item)}
    out: list[dict[str, Any]] = []
    number = 0
    for item in list(candidates or ()):
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        entry_id = str(row.get("entry_id") or "")
        if entry_id and entry_id in wanted:
            number += 1
            row["label"] = format_candidate_ref(number)
        else:
            row["label"] = ""
        out.append(row)
    return out


def search_receipt(result: Mapping[str, Any] | None) -> str:
    """``find_meme`` 的分态回执（§6 风格）：没命中 / 用不了 / 轮数用完各说各的。

    **不许混成一句"没有收藏"**：那是三件完全不同的事——没撞上、检索根本没法用、
    这一轮的检索额度已经用完了。
    """

    data = dict(result or {})
    reason = str(data.get("reason") or "")
    candidates = [item for item in list(data.get("candidates") or ()) if item]
    coverage = dict(data.get("coverage") or {})
    if reason == CONFIG_DISABLED:
        return "表情包功能现在关着。"
    if reason == SEARCH_NO_MATCH:
        return "没找到合适的收藏（照你说的意思一条都没撞上）。"
    if reason == SEARCH_UNAVAILABLE:
        return f"这次找不了：{str(data.get('note') or '检索现在用不了')}。"
    if reason == SEARCH_BUDGET_EXHAUSTED:
        return "这次回复里的找图次数已经用完了，这一轮不再找了。"
    listed = "、".join(
        f"{str(item.get('label') or item.get('entry_id') or '')}"
        f"（{str(item.get('name') or '没名字')}）"
        for item in candidates[:5]
    )
    tail = "（只列了前几条，后面的没带出来）" if coverage.get("truncated") else ""
    return f"找到 {len(candidates)} 条候选：{listed}{tail}。"


def send_rejection_receipt(
    *, reason: Any, entry_id: Any = "", name: Any = "", limit: Any = 0
) -> str:
    """发送前被拦下的分态回执：**没命中 / 不可用 / 额度用完**三族话必须分得开。

    额度那一族还分三种（这一轮 / 这一小时 / 今天），因为它们对应三种不同的处置：
    等一下再说、下个小时再说、明天再说——糊成一句"发不了"她就只能瞎猜。
    """

    key = str(entry_id or "").strip()
    title = str(name or "").strip()
    if key:
        who = f"（编号 {key}" + (f"，名称：{title}）" if title else "）")
    else:
        who = ""
    current = str(reason or "")
    if current == CONFIG_DISABLED:
        return "表情包功能现在关着。"
    if current == STATE_UNKNOWN:
        return "发不了：查不到这条收藏，编号要照抄清单里给的（比如 meme_000028）。"
    if current in (STATE_DISABLED, STATE_DELETED, STATE_MISSING, SCOPE_UNAVAILABLE):
        # 可用状态的文案只有一份（``_state_reason``）：停用 / 记录删了 / 文件没了
        # 三个方向完全不同的坏消息，不能糊成一句"发不了"。
        return f"发不了这条收藏{who}：{_state_reason(current)}。"
    if current == SEND_LIMIT:
        return f"这一次回复已经发过表情包了（一轮最多 {int(limit or 0)} 张），这张先不发。"
    if current == RATE_LIMITED:
        return f"这一小时发的表情包到上限了（{int(limit or 0)} 张），这张先不发。"
    if current == DAY_LIMITED:
        return f"今天发的表情包到上限了（{int(limit or 0)} 张），这张先不发。"
    if current == COOLDOWN:
        return "同一张图刚发过、还在冷却里，这张先不发。"
    return f"这张发不了（{gate_reason_text(current) or current or '原因不明'}）。"


def send_result_receipt(
    *, status: Any, entry_id: Any = "", name: Any = "", error: Any = ""
) -> str:
    """真发出去之后的回执：``accepted`` / ``delivered`` / ``failed`` / ``unknown`` 分开说。

    方案 §6：``accepted`` ≠ 对方已看到，``unknown`` 不算成功、也不盲目重发——
    所以这里一句"发出去了"都不敢含糊过去。
    """

    key = str(entry_id or "").strip()
    title = str(name or "").strip()
    label = key + (f"「{title}」" if title else "")
    current = str(status or "")
    if current == SEND_DELIVERED:
        return f"发出去了：{label}（平台给了送达凭据）。"
    if current == SEND_ACCEPTED:
        return f"发出去了：{label}（平台收下了，但还不算对方已经看到）。"
    if current == SEND_UNKNOWN:
        return (
            f"发出去没发出去没法确认：{label}"
            "（平台没给凭据，这条不算成功，也不会自动重发）。"
        )
    detail = str(error or "").strip()
    return f"没发出去：{label}" + (f"（{detail}）" if detail else "") + "。"


# --------------------------------------------------------------------------- #
# M4：语义通道、软排序（全是纯函数 / 纯数据，**这一层自己绝不调模型**）
# --------------------------------------------------------------------------- #


def _number(value: Any, default: float = 0.0) -> float:
    """任意值 → 有限浮点（认不出来 / NaN / ±Inf 回默认）。"""

    if isinstance(value, bool) or value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return number


def _clamp01(value: Any) -> float:
    """夹到 ``[0, 1]``（软排序的每一项都要先归一，权重才有意义）。"""

    return max(0.0, min(1.0, _number(value, 0.0)))


def _vector_components(value: Any) -> tuple[float, ...]:
    """任意"疑似向量" → 浮点元组；认不出来回空元组（**不猜**）。

    认三种形状：``bytes``（``core.db.pack_vector`` 打的小端 float32）、浮点序列、
    以及 ``None``。空 / 全零 / 非有限 / 认不出来一律回空——零向量跟谁算余弦都是
    0/0，收进来只会让整轮打分变成 NaN。
    """

    values: list[float] = []
    candidate: Any = value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            candidate = unpack_vector(bytes(value))
        except Exception:
            return ()
    if not isinstance(candidate, (list, tuple)):
        return ()
    for item in candidate:
        if isinstance(item, bool):
            return ()
        try:
            number = float(item)
        except (TypeError, ValueError):
            return ()
        if number != number or number in (float("inf"), float("-inf")):
            return ()
        values.append(number)
    if not values or not any(values):
        return ()
    return tuple(values)


@dataclass(frozen=True)
class SemanticChannel:
    """一次检索用的语义通道：**查询向量 + 候选向量，全是调用方给的**。

    为什么叫"通道"而不是"索引"：这一层不认识 embedding 供应商、不建连接、不算
    向量（方案 §12.1 第 3 条：meme 向量独立成表、授权独立；算向量是引擎的事）。
    它只是一份**只读的输入**：这句话的查询向量、以及"哪条收藏的向量是什么"。

    字段：

    * ``space``：向量空间（``core.models.EmbeddingSpace`` 或任何带 ``fingerprint``
      的对象）。指纹进 ``meme_vectors`` 的主键，换 provider / 模型 / 维度 = 换空间。
    * ``provider_id``：嵌入服务商（空 = 没配）。
    * ``query_vector``：这一句的查询向量（**调用方算好交进来**）。
    * ``vectors``：``{entry_id: 向量}``（``bytes`` 或浮点序列都收）。
    * ``recipe_version``：索引文本配方（与 ``topic_vectors.recipe_version`` 同义）。
    * ``reason``：不可用时的原因码（见 :data:`SEMANTIC_REASON_LABELS`）。

    ``available`` 只要求"有空间 + 有查询向量"：候选向量可以事后由
    :meth:`MemeStore.load_vectors` 从注册表里补（表还没加时如实降级）。
    """

    space: Any = None
    provider_id: str = ""
    query_text: str = ""
    query_vector: tuple[float, ...] = ()
    vectors: Mapping[str, Any] = field(default_factory=dict)
    recipe_version: str = ""
    reason: str = ""

    @property
    def fingerprint(self) -> str:
        """向量空间指纹（没有空间就是空串）。"""

        return str(getattr(self.space, "fingerprint", "") or "")

    @property
    def available(self) -> bool:
        """这个通道现在能不能真的参与打分。"""

        return bool(self.fingerprint) and bool(self.query_vector)

    @property
    def reason_text(self) -> str:
        """原因码 → 中文（可用时给"语义通道可用"）。"""

        code = self.reason if not self.available else ""
        return SEMANTIC_REASON_LABELS.get(code, code or "")

    def to_dict(self) -> dict[str, Any]:
        """诊断用的一份（不含向量本体——那是几 KB 的浮点，日志里没用）。"""

        return {
            "space": self.fingerprint,
            "provider_id": self.provider_id,
            "query_text": self.query_text,
            "query_dim": len(self.query_vector),
            "vectors": len(self.vectors),
            "recipe_version": self.recipe_version,
            "reason": self.reason if not self.available else "",
            "available": self.available,
        }


def semantic_channel(
    space: Any = None,
    *,
    provider_id: Any = None,
    query_text: Any = "",
    query_vector: Any = None,
    vectors: Any = None,
    query_vectors: Any = None,
    recipe_version: Any = "",
    reason: Any = "",
) -> SemanticChannel:
    """拼一份语义通道（纯函数）：把"调用方给的东西"归一成 :class:`SemanticChannel`。

    ``query_vectors`` 是"按文本查查询向量"的表（``{文本: 向量}``）——**查询文本参数
    就是给它用的**：这一层不算向量，所以"这一句的向量"只能由调用方（或它的缓存）
    提供；表里没有这一句就当"没查询向量"如实降级，绝不退而求其次拿别的句子的向量。

    原因码按"最先缺的那样东西"给：没空间 → ``no_space``，没 provider →
    ``no_provider``（空间自带 ``provider_id`` 时不用另传），没查询向量 →
    ``no_query_vector``。``reason=`` 可以显式覆盖（调用方已经知道为什么）。
    """

    if isinstance(space, SemanticChannel):
        return space
    fingerprint = str(getattr(space, "fingerprint", "") or "")
    vendor = str(provider_id or getattr(space, "provider_id", "") or "").strip()
    vector = _vector_components(query_vector)
    if not vector and isinstance(query_vectors, Mapping):
        text = " ".join(str(query_text or "").split())
        for key in (text, text.lower(), str(query_text or "")):
            if key and key in query_vectors:
                vector = _vector_components(query_vectors.get(key))
                break
    table: dict[str, Any] = {}
    if isinstance(vectors, Mapping):
        table = {str(key): value for key, value in vectors.items() if str(key)}
    explicit = str(reason or "").strip()
    if explicit:
        code = explicit
    elif not fingerprint:
        code = "no_space"
    elif not vendor:
        code = "no_provider"
    elif not vector:
        code = "no_query_vector"
    else:
        code = ""
    return SemanticChannel(
        space=space,
        provider_id=vendor,
        query_text=" ".join(str(query_text or "").split()),
        query_vector=vector,
        vectors=table,
        recipe_version=str(recipe_version or ""),
        reason=code,
    )


def soft_rank_candidates(
    candidates: Any,
    *,
    weights: Mapping[str, Any] | None = None,
    now: Any = 0.0,
    half_life_days: Any = RECENCY_HALF_LIFE_DAYS,
) -> dict[str, Any]:
    """把"关键词命中分 + 向量相似度 + 时间 / 使用次数"柔和地合成一个排序（纯函数）。

    **"软"体现在三条**（方案 §5 的"软排序"要求）：

    1. **不丢人**：关键词没命中的候选**照样留在结果里**（关键词项记 0 分），
       排序只是把"更像"的挪前面；向量相似也不是拦路的闸门。
    2. **不越权**：带着 ``state`` 且不是 ``enabled``、或 ``usable is False`` 的条目
       **一律剔除**并记进 ``excluded``——软排序不许把停用 / 已删 / 文件缺失的收藏
       重新拉回候选（方案 §1「向量索引只是检索副本，不是授权权威」）。
       没有 ``state`` 字段的条目按"调用方已经筛过"处理（:meth:`MemeStore.search`
       就是这么传的），既不加人也不放行任何东西。
    3. **可解释**：每条的 ``rank_parts`` 把四项分量摊开写清，分数不是黑箱。

    分量与归一（权重见 :data:`SOFT_RANK_WEIGHTS` / :data:`EMBEDDING_RANK_WEIGHTS`）：

    * ``keyword``：``min(1, 关键词分 / KEYWORD_SCORE_SATURATION)``——不设饱和点的话，
      长语义条目会靠字数压死短条目；
    * ``vector``：余弦相似度夹到 ``[0, 1]``（**负相似算 0**：方向相反不算像，
      也不倒扣——余弦差是"不像"，不是"像反面"）；
    * ``recency``：``0.5 ** (age_days / half_life_days)``（半衰期见
      :data:`RECENCY_HALF_LIFE_DAYS`；``updated_at`` / ``now`` 没给就是 0 分）；
    * ``usage``：``min(1, recent_uses / USAGE_SATURATION)``。**"最近用过几次"由调用方
      从 ``meme_usage`` 里数好放在 ``recent_uses`` 上**（本函数不查库）。次数多排前
      面是"她真的会拿它出去用"的温和偏置，权重刻意很小；想"别老发同一张"那是
      ``repeat_cooldown`` 的事，不是软排序的事。
    * ``preference``：条目自带的软偏好，夹到 ``[-1, 1]``（负数会往下压）。

    排序键是 ``(-rank_score, -关键词分, -向量相似, -updated_at, entry_id)``：
    全序、与输入顺序无关，同一批数据在任何机器上给同一个顺序。
    """

    data = dict(weights or SOFT_RANK_WEIGHTS)
    ranked: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    keyword_hits = 0
    semantic_only = 0
    for item in list(candidates or ()):
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        state = str(row.get("state") or "")
        if (state and state != STATE_ENABLED) or row.get("usable") is False:
            excluded.append(
                {
                    "entry_id": str(row.get("entry_id") or ""),
                    "state": state or (STATE_DISABLED if row.get("usable") is False else ""),
                    "reason": "unavailable_not_rankable",
                }
            )
            continue
        keyword_score = max(0.0, _number(row.get("score"), 0.0))
        vector_score = _clamp01(row.get("vector_score"))
        stamp = _number(row.get("updated_at"), 0.0)
        moment = _number(now, 0.0)
        fresh = stamp > 0 and moment > 0
        # "刚刚改过" = 衰减到 1.0（不是 0）：时间项的语义是"越新越值钱"，
        # 拿 0 当"没有时间信息"的哨兵时千万别把最新的那条也一起归零。
        age_days = max(0.0, (moment - stamp) / DAY_SECONDS) if fresh else 0.0
        life = max(0.0001, _number(half_life_days, RECENCY_HALF_LIFE_DAYS))
        parts = {
            "keyword": _clamp01(keyword_score / KEYWORD_SCORE_SATURATION),
            "vector": vector_score,
            "recency": _clamp01(0.5 ** (age_days / life)) if fresh else 0.0,
            "usage": _clamp01(_number(row.get("recent_uses"), 0.0) / USAGE_SATURATION),
            "preference": max(-1.0, min(1.0, _number(row.get("preference"), 0.0))),
        }
        total = 0.0
        for name, part in parts.items():
            total += _number(data.get(name), 0.0) * part
        row["rank_score"] = round(total, 6)
        row["rank_parts"] = {name: round(part, 6) for name, part in parts.items()}
        row["keyword_score"] = keyword_score
        row["vector_score"] = vector_score if row.get("vector_score") is not None else None
        if keyword_score > 0:
            keyword_hits += 1
        elif vector_score > 0:
            semantic_only += 1
        ranked.append(row)
    ranked.sort(
        key=lambda row: (
            -_number(row.get("rank_score"), 0.0),
            -_number(row.get("keyword_score"), 0.0),
            -_number(row.get("vector_score"), 0.0),
            -_number(row.get("updated_at"), 0.0),
            str(row.get("entry_id") or ""),
        )
    )
    return {
        "ranked": ranked,
        "excluded": excluded,
        "coverage": {
            "scored": len(ranked),
            "excluded_unavailable": len(excluded),
            "keyword_hits": keyword_hits,
            "semantic_only": semantic_only,
            "weights": {name: _number(data.get(name), 0.0) for name in SOFT_RANK_WEIGHTS},
        },
    }


class MemeStore:
    """表情包收藏的记录层：一个薄包装，把配置、资产库与 ``core.db`` 接在一起。

    ``db`` 必须是**同步**的 ``core.db.Database``：这一层的接口全是同步的
    （M2 的回复路径也要直接用它），所以调用方手里若是 ``AsyncDatabase``，
    要交 ``async_db.raw`` 进来，再由调用方自己用 ``asyncio.to_thread`` 挪出
    事件循环。**别把 ``AsyncDatabase`` 直接交进来**——那上面只有
    ``call/run/transaction/raw``，没有下面这些 meme 方法，真机上会
    ``AttributeError``（单元测试里手工构造的是同步库，看不出来）。
    ``media_store`` 给了才能判断"底层文件还在不在"；不给时
    一切"文件在不在"都按**在**算（页面之外的纯数据用法更方便）。
    """

    def __init__(
        self,
        db: Any,
        *,
        world: Any = None,
        settings: Any = None,
        media_store: Any = None,
        clock: Any = None,
    ) -> None:
        self._db = db
        self._world = world
        self._settings = settings
        self._media_store = media_store
        self._clock_source = clock

    def _clock(self) -> float:
        """现在（秒）。给了 ``clock`` 就用它——测试要造"过期认领"得能控时间。"""

        if self._clock_source is not None:
            return float(self._clock_source())
        return time.time()

    # ---------------- 配置 ----------------

    @property
    def settings(self) -> Any:
        """生效配置（没显式给就按当前世界现算一份；只读）。"""

        if self._settings is None:
            self._settings = meme_settings(self._world)
        return self._settings

    def refresh(self, world: Any) -> Any:
        """换一份世界配置（``reload_config`` 之后调）。"""

        self._world = world
        self._settings = None
        return self.settings

    # ---------------- 文件与范围 ----------------

    def file_present(self, media_id: Any) -> bool:
        """底层图片文件在不在（没给资产库就按"在"算）。"""

        if self._media_store is None:
            return True
        key = str(media_id or "").strip()
        if not key:
            return False
        return self._media_store.open_bytes(key) is not None

    def scope_allowed(self, entry_id: Any, *, scope_kind: str, scope_id: str = "") -> bool:
        """这条收藏在当前会话里算不算数——**永远算数**。

        用户裁定（m20「表情包去掉会话限制吧 直接通用 没必要搞成这样」）：收下的表情
        就是通用的，到哪个会话都能用。所以这里一律回 ``True``，范围维度不再参与可用
        状态判定（``meme_sources.allowed_scopes`` / ``scope_id`` 两个字段与
        ``meme_scope_intersection()`` 都留着不动：存档与老数据照旧读得出来，
        真要恢复"按会话限制"只需把这一行改回去）。
        """

        del entry_id, scope_kind, scope_id
        return True

    def pin_entry(self, entry_id: Any, *, media_id: Any = None) -> bool:
        """把收藏指向的图片钉在资产库上（没给资产库就当钉成功）。

        ``MediaStore`` 的 GC 只淘汰没人引用的文件，收藏记录自己就是一条引用；
        不钉的话，收藏还在、底层文件被 GC 顺手删掉，就变成"missing"那一态了。
        """

        key = str(entry_id or "").strip()
        if not key:
            return False
        if self._media_store is None:
            return True
        asset = str(media_id or "").strip()
        if not asset:
            row = self._db.get_meme_entry(key) or {}
            asset = str(row.get("media_id") or "").strip()
        if not asset:
            return False
        return bool(self._media_store.pin(asset, meme_pin_ref(key)))

    def unpin_entry(self, entry_id: Any, *, media_id: Any = None) -> bool:
        """解掉收藏指向的图片的钉（删除/清理时用；幂等）。"""

        key = str(entry_id or "").strip()
        if not key:
            return False
        if self._media_store is None:
            return True
        asset = str(media_id or "").strip()
        if not asset:
            row = self._db.get_meme_entry(key) or {}
            asset = str(row.get("media_id") or "").strip()
        if not asset:
            return False
        return bool(self._media_store.unpin(asset, meme_pin_ref(key)))

    # ---------------- 五态判定 ----------------

    def availability(
        self, entry_id: Any, *, scope_kind: str = "", scope_id: str = ""
    ) -> dict[str, Any]:
        """按 ID 判一条收藏的可用状态（含"记录没了但文件还在"这一态）。

        返回 ``{"state", "label", "entry", "file_present", "reason"}``。
        ``scope_kind`` 留空表示**不按范围过滤**（管理页就是这种用法：管理员要看到
        全部收藏，哪怕它现在哪个群都用不了）。
        """

        key = str(entry_id or "").strip()
        row = self._db.get_meme_entry(key) if key else None
        media_id = str((row or {}).get("media_id") or "").strip()
        present = self.file_present(media_id) if media_id else False
        allowed = True
        if scope_kind:
            allowed = self.scope_allowed(key, scope_kind=scope_kind, scope_id=scope_id)
        state = meme_availability(row, file_present=present, scope_allowed=allowed)
        return {
            "state": state,
            "label": availability_label(state),
            "entry": row,
            "file_present": present,
            "reason": _state_reason(state),
        }

    def entry_view(
        self, entry_id: Any, *, scope_kind: str = "", scope_id: str = ""
    ) -> dict[str, Any] | None:
        """管理页要的"一条收藏的完整样子"（含状态、资产、来源、变体、用法）。"""

        key = str(entry_id or "").strip()
        row = self._db.get_meme_entry(key) if key else None
        if row is None:
            return None
        media_id = str(row.get("media_id") or "").strip()
        present = self.file_present(media_id) if media_id else False
        allowed = True
        if scope_kind:
            allowed = self.scope_allowed(key, scope_kind=scope_kind, scope_id=scope_id)
        state = meme_availability(row, file_present=present, scope_allowed=allowed)
        asset = self._db.get_meme_asset(media_id) if media_id else None
        return {
            "entry": row,
            "state": state,
            "state_label": availability_label(state),
            "state_reason": _state_reason(state),
            "file_present": present,
            "media_id": media_id,
            "asset": asset,
            "pinned": bool(row.get("pinned")),
            "sources": self._db.list_meme_sources(key),
            "variants": self._db.list_meme_variants(key),
            "usage": self._db.list_meme_usage(entry_id=key, limit=20),
        }

    def entry_views(
        self,
        *,
        scope_id: Any = None,
        status: Any = None,
        include_deleted: bool = True,
        limit: int = 200,
        offset: int = 0,
        scope_kind: str = "",
        order: str = "collected",
        desc: bool = True,
    ) -> list[dict[str, Any]]:
        """列收藏（每条都带上可用状态与资产信息）。

        ``order``/``desc`` 直接透给 :meth:`db.list_meme_entries`（用户 m21273 要的排序：
        收藏时间 / 已用次数，正序倒序都能切，默认收藏时间倒序）。

        ``include_deleted`` 默认 **True**：管理页要能看到"记录删了但文件还在"
        的那些，否则清理就无从下手。
        """

        rows = self._db.list_meme_entries(
            scope_id=scope_id,
            status=status,
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
            order=order,
            desc=desc,
        )
        views: list[dict[str, Any]] = []
        for row in rows:
            key = str(row.get("entry_id") or "")
            media_id = str(row.get("media_id") or "").strip()
            present = self.file_present(media_id) if media_id else False
            allowed = True
            if scope_kind:
                allowed = self.scope_allowed(
                    key, scope_kind=scope_kind, scope_id=""
                )
            state = meme_availability(row, file_present=present, scope_allowed=allowed)
            item = dict(row)
            item["state"] = state
            item["state_label"] = availability_label(state)
            # 列表里就带上"为什么不能用"，页面点开详情不用再要一次请求（用户 m21273）。
            item["state_reason"] = _state_reason(state)
            item["file_present"] = present
            item["pinned"] = bool(row.get("pinned"))
            try:
                item["use_count"] = int(row.get("use_count") or 0)
            except (TypeError, ValueError):
                item["use_count"] = 0
            views.append(item)
        return views

    # ---------------- 收藏与语义 ----------------

    def find_by_media(
        self, media_id: Any, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        """按内容身份找已有的收藏（**相同字节必须是同一个 media_id**）。

        ``include_deleted=True`` 时连软删的行也找出来：``collect_image`` 与
        ``image_status`` 必须能看见它，否则"记录删了但文件还在"这条回执（方案 §6）
        永远走不到，回收站里的图会被当成从没见过的图重收一遍。
        """

        key = str(media_id or "").strip()
        if not key:
            return None
        row = self._db.find_meme_entry_by_media(key)
        if row is not None or not include_deleted:
            return row
        rows = self._db.list_meme_entries(media_id=key, include_deleted=True, limit=1)
        return dict(rows[0]) if rows else None

    def image_status(
        self, media_id: Any, *, scope_kind: str = "", scope_id: str = ""
    ) -> dict[str, Any]:
        """一张图在本轮的收藏情况（提示词里「已收藏，编号 meme_0xx」由它给）。

        **状态一律查库**：模型不许凭"好像见过"自己编编号（方案 §5）。返回
        ``{"media_id","collected","usable","entry_id","name","state","label",
        "reason","entry"}``；没收藏过时 ``collected=False``、其余为空。
        """

        key = str(media_id or "").strip()
        result: dict[str, Any] = {
            "media_id": key,
            "collected": False,
            "usable": False,
            "entry_id": "",
            "name": "",
            "state": "",
            "label": "",
            "reason": "",
            "entry": None,
        }
        if not key:
            return result
        row = self.find_by_media(key, include_deleted=True)
        if row is None:
            return result
        entry_id = str(row.get("entry_id") or "")
        present = self.file_present(key)
        allowed = True
        if scope_kind:
            allowed = self.scope_allowed(
                entry_id, scope_kind=scope_kind, scope_id=scope_id
            )
        state = meme_availability(row, file_present=present, scope_allowed=allowed)
        result.update(
            {
                "collected": True,
                "usable": state == STATE_ENABLED,
                "entry_id": entry_id,
                "name": str(row.get("name") or ""),
                "state": state,
                "label": availability_label(state),
                "reason": _state_reason(state),
                "entry": row,
            }
        )
        return result

    def reference_caption(
        self, media_id: Any, *, scope_kind: str = "", scope_id: str = ""
    ) -> str:
        """已收藏的图**不再重转述**：写一行引用（meme 编号 + 内容 hash）。

        条目没有可用语义（``meaning`` / ``use_when`` 都空）时返回空串——调用方
        按空串**回退转述**："收藏过但没写语义"不能当成画面描述用（设计 §13.1）。
        """

        status = self.image_status(media_id, scope_kind=scope_kind, scope_id=scope_id)
        if not status.get("usable"):
            return ""
        entry = dict(status.get("entry") or {})
        meaning = str(entry.get("meaning") or "").strip()
        use_when = str(entry.get("use_when") or "").strip()
        if not meaning and not use_when:
            return ""
        entry_id = str(status.get("entry_id") or "")
        name = str(status.get("name") or "")
        digest = str(status.get("media_id") or "")[:12]
        head = f"已收藏的图 {entry_id}"
        if name:
            head += f"（{name}）"
        head += f"｜内容 {digest}"
        return f"{head}：{meaning or use_when}"

    # ---------------- M3/M4：检索（关键词打底，语义软排） ----------------

    def _wanted_limit(self, limit: Any, settings: Any) -> int:
        """这次取回几条候选：``limit=None`` 用配置，``0`` 就是 **0**（真的不预选）。

        M3 的写法是 ``int(limit or 配置)``——``limit=0`` 被当成"没给"而落回配置，
        于是"预选数量配成 0"这个边界永远试不出来（M4 验收点之一：预选数量要含 0
        与边界）。这里改成按"是不是 ``None``"判，0 与负数一律夹成 0。
        """

        if limit is None:
            raw = _number(getattr(settings, "search_candidate_count", 0), 0.0)
            if raw <= 0:
                raw = float(DEFAULT_CANDIDATE_LIMIT)
            wanted = int(raw)
        else:
            wanted = int(_number(limit, 0.0))
        return max(0, min(wanted, 100))

    def _eligible_rows(
        self, *, scope_kind: str = "", scope_id: str = ""
    ) -> tuple[list[dict[str, Any]], int]:
        """列出"现在真能用"的收藏行（检索与语义预选**共用**的唯一入口）。

        返回 ``(行列表, 被挡掉的条数)``。挡掉的 = 五态里除 ``enabled`` 之外的全部
        （停用 / 文件缺失 / 范围不可用）；**软删的行根本不在列表里**
        （``list_meme_entries`` 默认 exclude_deleted），所以它连"跳过"都不算——
        这是 M3 的口径，M4 不动它。

        为什么把这段抽出来：向量预选也必须走同一道闸门。"向量只用来排序，绝不扩权"
        这句话落成代码，就是"任何条目进向量打分之前先过这里"。
        """

        rows: list[dict[str, Any]] = []
        skipped = 0
        for row in self._db.list_meme_entries(
            include_deleted=False, limit=MAX_SEMANTIC_POOL
        ):
            media_id = str(row.get("media_id") or "").strip()
            present = self.file_present(media_id) if media_id else False
            allowed = True
            if scope_kind:
                allowed = self.scope_allowed(
                    str(row.get("entry_id") or ""),
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                )
            if meme_availability(row, file_present=present, scope_allowed=allowed) != (
                STATE_ENABLED
            ):
                # 不可用的条目连"像不像"都不判：它根本发不出去，列出来只会骗她
                skipped += 1
                continue
            rows.append(row)
        return rows, skipped

    def _candidate_item(
        self, row: Mapping[str, Any], tokens: Any, *, state: str = STATE_ENABLED
    ) -> dict[str, Any]:
        """一条收藏行（或一条已经拼好的候选）→ 候选条目。

        ``state`` 一定要带上：:meth:`soft_rank` 靠它把"不可用"的条目挡在软排序之外
        （软排序不许把停用 / 已删的收藏重新拉回候选）。两种输入形状都要认（库里的行
        用 ``asset_tags`` / ``asset_size_bytes``，拼好的候选用 ``tags`` / ``size_bytes``），
        否则 M4 的预选把候选再喂进来复核一次时会悄悄丢字段。
        """

        score, hits = _match_row(row, tokens)
        return {
            "entry_id": str(row.get("entry_id") or ""),
            "media_id": str(row.get("media_id") or "").strip(),
            "name": str(row.get("name") or ""),
            "meaning": str(row.get("meaning") or ""),
            "use_when": str(row.get("use_when") or ""),
            "avoid_when": str(row.get("avoid_when") or ""),
            "personal_note": str(row.get("personal_note") or ""),
            "tags": str(row.get("asset_tags") or row.get("tags") or ""),
            "status": str(row.get("status") or ""),
            "revision": int(row.get("revision") or 0),
            "preference": float(row.get("preference") or 0.0),
            "updated_at": float(row.get("updated_at") or 0.0),
            "score": float(score),
            "why": match_why(hits),
            "hits": hits,
            "state": str(row.get("state") or state or STATE_ENABLED),
            # 预览的字节数：第③级预算按它算（M3 先用原图当预览，压图是 M5）
            "size_bytes": int(row.get("asset_size_bytes") or row.get("size_bytes") or 0),
            # 编号由 label_candidates() 在"预览真附上了"之后才给（§4）
            "label": "",
        }

    def search(
        self,
        intent: Any,
        *,
        limit: Any = None,
        scope_kind: str = "",
        scope_id: str = "",
        now: Any = 0.0,
        semantic: Any = None,
        min_score: Any = None,
        weights: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按一句"想表达的意思"检索（M3 关键词 + M4 语义，方案 §5/§6/§10）。

        三种口径（``retrieval_mode`` → 实际走哪一档）：

        * ``keyword``：纯关键词（**M3 的原口径，排序一个字没改**）。语义项报 ``off``。
        * ``auto`` / ``keyword_semantic``：**关键词打底 + 向量软排**——关键词命中的
          候选一条都不会因为向量而不见（它们整组排在语义-only 候选前面），向量只在
          组内重排，并把"关键词没命中但语义很像"的条目补在后面（方案 §5 的"软"）。
        * ``embedding``：**纯向量预选 + 关键词复核**——先按向量取回，再跑一遍关键词
          给每条写出"为什么像"；强制这一档又没配服务商、或调用方给不出能用的向量
          通道时**直接报 ``unavailable``**（一条关键词候选都不给：点名的就是纯向量，
          悄悄换掉是骗人；这是 M3 定下的口径，M4 不动）。

        ``semantic`` 是语义通道（:class:`SemanticChannel`，或能拼出它的映射）：这一层
        **自己绝不调模型**，查询向量与候选向量都由调用方（引擎）交进来。没给通道 /
        没有向量空间 / 索引里没有向量 → **如实降级**：退回关键词，并在 ``coverage``
        里写清 ``semantic`` 走到了哪一步、``semantic_reason`` 为什么。

        返回 ``{"ok","reason","note","candidates","coverage"}``；``reason`` 区分
        ``no_match`` / ``unavailable``（可能还有 ``config_disabled``）。**只看可用
        条目**：被停用 / 记录删了 / 文件不见了的收藏既不进关键词候选，也不会因为向量
        相似而被放进来（§1："向量索引只是检索副本，不是授权权威"）。
        """

        settings = self.settings
        mode = str(
            getattr(settings, "resolved_retrieval_mode", "")
            or settings.retrieval_mode
            or SEARCH_MODE_KEYWORD
        )
        if not settings.enabled:
            return self._search_result(
                reason=CONFIG_DISABLED,
                note=gate_reason_text(CONFIG_DISABLED),
                coverage={
                    "mode": mode,
                    "semantic": SEMANTIC_OFF,
                    "semantic_reason": "disabled",
                    "semantic_reason_text": SEMANTIC_REASON_LABELS["disabled"],
                },
            )
        gate = settings.gate_provider()
        if gate != GATE_OK:
            # 用户点名要语义、环境却给不了 → 如实报"用不了"，绝不偷偷换关键词
            return self._search_result(
                reason=SEARCH_UNAVAILABLE,
                note=gate_reason_text(gate),
                coverage={
                    "mode": mode,
                    "semantic": SEMANTIC_UNAVAILABLE,
                    "semantic_reason": "no_provider",
                    "semantic_reason_text": SEMANTIC_REASON_LABELS["no_provider"],
                },
            )
        tokens = query_tokens(intent)
        coverage: dict[str, Any] = {
            "mode": mode,
            "semantic": SEMANTIC_OFF,
            "semantic_reason": "keyword_mode" if mode == SEARCH_MODE_KEYWORD else "",
            "semantic_reason_text": SEMANTIC_REASON_LABELS["keyword_mode"],
            "tokens": len(tokens),
            "matched": 0,
            "returned": 0,
            "truncated": False,
            "unavailable_skipped": 0,
            "limit": 0,
            "scored": 0,
            "semantic_scored": 0,
            "semantic_only": 0,
            "below_min_score": 0,
            "no_vector": 0,
            "space": "",
            "provider_id": "",
            "min_score": _number(
                SEMANTIC_MIN_SCORE if min_score is None else min_score, SEMANTIC_MIN_SCORE
            ),
        }
        if not tokens:
            return self._search_result(
                reason=SEARCH_NO_MATCH,
                note="没给检索词",
                coverage=coverage,
            )
        wanted = self._wanted_limit(limit, settings)
        coverage["limit"] = wanted
        if wanted <= 0:
            # 预选数量配成 0 = 这一轮不预选（边界：0 就是 0，不许偷偷给一条）
            return self._search_result(
                reason=SEARCH_NO_MATCH,
                note="候选数配成了 0，这一轮不预选",
                coverage=coverage,
            )
        eligible, skipped = self._eligible_rows(
            scope_kind=scope_kind, scope_id=scope_id
        )
        coverage["unavailable_skipped"] = skipped
        pool = [self._candidate_item(row, tokens) for row in eligible]
        matched = [item for item in pool if _number(item.get("score")) > 0]
        coverage["matched"] = len(matched)

        if mode == SEARCH_MODE_KEYWORD:
            candidates = self._keyword_order(matched)[:wanted]
            coverage["scored"] = len(matched)
            coverage["returned"] = len(candidates)
            coverage["truncated"] = len(matched) > len(candidates)
            return self._finish_search(candidates, coverage, note="关键词匹配")

        channel = self._resolve_channel(semantic, intent)
        if not channel.available:
            return self._degraded_search(
                matched, coverage, wanted=wanted, channel=channel, mode=mode
            )
        preselected = self.preselect(
            channel,
            intent,
            limit=max(1, len(pool)),
            candidates=pool,
            min_score=coverage["min_score"],
            keyword_tokens=tokens,
        )
        if not preselected.get("ok"):
            # 有空间、有查询向量，但索引里没有可比向量 → 一样如实降级（关键词候选
            # 是真实可用的；方案 §9"无 embedding 时回退关键词"说的就是这一步）
            return self._degraded_search(
                matched,
                coverage,
                wanted=wanted,
                channel=self._same_channel(
                    channel, reason=str(preselected.get("reason") or "no_vectors")
                ),
                mode=mode,
            )
        coverage["space"] = channel.fingerprint
        coverage["provider_id"] = channel.provider_id
        inner = dict(preselected.get("coverage") or {})
        coverage["semantic_scored"] = int(inner.get("scored") or 0)
        coverage["below_min_score"] = int(inner.get("below_min_score") or 0)
        coverage["no_vector"] = int(inner.get("no_vector") or 0)
        # "最近用过几次"由这一层从 meme_usage 里数好再交给纯排序函数（软排序不查库）
        ranked_pool = self._fill_usage(preselected["candidates"], now=now)
        semantic_only = [
            item
            for item in ranked_pool
            if _number(item.get("score")) <= 0
            and str(item.get("entry_id") or "") not in {str(row.get("entry_id") or "") for row in matched}
        ]
        if mode == SEARCH_MODE_EMBEDDING:
            # 纯向量档：整个预选集合一起排（预选本来就是靠向量进来的），向量是主、
            # 关键词只作复核与解释——**关键词没命中的条目在这里一条都不许丢**，
            # 它们正是"纯向量"要找的东西。
            ranked = self.soft_rank(
                ranked_pool, weights=weights or EMBEDDING_RANK_WEIGHTS, now=now
            )
            ordered = list(ranked["ranked"])
        else:
            # 中间档：**关键词命中的候选一条都不许丢**——包括那些"向量根本不够像、
            # 没进预选"的（关键词打底的意思就是它靠关键词进来，不靠向量）。
            # 向量只用来在组内重排；语义-only 的补在后面，这就是"不因为关键词没
            # 命中就整条丢掉"。
            scored_by_entry = {
                str(item.get("entry_id") or ""): item for item in ranked_pool
            }
            keyword_items: list[dict[str, Any]] = []
            for item in matched:
                row = dict(item)
                scored = scored_by_entry.get(str(row.get("entry_id") or ""))
                if scored is not None:
                    row["vector_score"] = scored.get("vector_score")
                    row["vector_note"] = scored.get("vector_note")
                keyword_items.append(row)
            keyword_group = self.soft_rank(
                self._fill_usage(keyword_items, now=now),
                weights=weights or SOFT_RANK_WEIGHTS,
                now=now,
            )
            semantic_group = self.soft_rank(
                semantic_only, weights=weights or SOFT_RANK_WEIGHTS, now=now
            )
            ordered = list(keyword_group["ranked"]) + list(semantic_group["ranked"])
        candidates = ordered[:wanted]
        coverage["semantic"] = SEMANTIC_USED
        coverage["semantic_reason"] = ""
        coverage["semantic_reason_text"] = SEMANTIC_REASON_LABELS[""]
        coverage["scored"] = len(ordered)
        coverage["semantic_only"] = len(semantic_only)
        coverage["returned"] = len(candidates)
        coverage["truncated"] = len(ordered) > len(candidates)
        note = (
            "关键词打底 + 向量软排"
            if mode == SEARCH_MODE_KEYWORD_SEMANTIC
            else "向量预选 + 关键词复核"
        )
        note += f"（语义参与打分 {coverage['semantic_scored']} 条）"
        return self._finish_search(candidates, coverage, note=note)

    @staticmethod
    def _resolve_channel(semantic: Any, intent: Any) -> SemanticChannel:
        """``search(semantic=…)`` 的入参 → 语义通道（没给就是"没通道"）。"""

        if isinstance(semantic, SemanticChannel):
            return semantic
        if not isinstance(semantic, Mapping):
            return semantic_channel(
                None, query_text=intent, reason="no_channel"
            )
        return semantic_channel(
            semantic.get("space"),
            provider_id=semantic.get("provider_id"),
            query_text=intent,
            query_vector=semantic.get("query_vector"),
            vectors=semantic.get("vectors"),
            query_vectors=semantic.get("query_vectors"),
            recipe_version=semantic.get("recipe_version") or "",
        )

    @staticmethod
    def _same_channel(channel: SemanticChannel, *, reason: str) -> SemanticChannel:
        """同一份通道换个原因码（降级文案要写清"为什么没跑成"）。"""

        return semantic_channel(
            channel.space,
            provider_id=channel.provider_id,
            query_text=channel.query_text,
            query_vector=channel.query_vector,
            vectors=channel.vectors,
            recipe_version=channel.recipe_version,
            reason=reason,
        )

    def _keyword_order(self, items: Any) -> list[dict[str, Any]]:
        """关键词候选的排序（**M3 的原口径**，一个字没改）：分 → 偏好 → 时间 → 编号。"""

        rows = [dict(item) for item in list(items or ()) if isinstance(item, Mapping)]
        rows.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                -float(item.get("preference") or 0.0),
                -float(item.get("updated_at") or 0.0),
                str(item.get("entry_id") or ""),
            )
        )
        return rows

    def _finish_search(
        self, candidates: Any, coverage: dict[str, Any], *, note: str
    ) -> dict[str, Any]:
        """检索收尾：候选为空时报 ``no_match``（**不是** unavailable，那是另一件事）。"""

        rows = list(candidates or ())
        coverage["returned"] = len(rows)
        if not rows:
            return self._search_result(
                reason=SEARCH_NO_MATCH,
                note="一条都没撞上" if coverage.get("semantic") != SEMANTIC_USED else note,
                coverage=coverage,
            )
        return self._search_result(
            reason=SEARCH_OK, note=note, coverage=coverage, candidates=rows
        )

    def _degraded_search(
        self,
        matched: Any,
        coverage: dict[str, Any],
        *,
        wanted: int,
        channel: SemanticChannel,
        mode: str,
    ) -> dict[str, Any]:
        """语义这一档用不上 → 按口径**如实**收场（方案 §9/§6）。

        两种口径**分开处理**，因为"我点名的就是你"和"你替我挑"是两件事：

        * 用户点名 ``embedding``（纯向量）却拿不到向量 → **直接报 ``unavailable``，
          一条关键词候选都不给**：他点名的就是纯向量检索，悄悄换成关键词等于骗人
          （这是 M3 就定下的口径，M4 一个字不改）。原因写进 ``note`` 与
          ``semantic_reason``。
        * 中间档（``auto`` / ``keyword_semantic``）→ 退回关键词候选，``coverage``
          里报 ``pending_m4``（M3 的既有字面量，含义在 M4 收紧为"这次调用没拿到
          能用的语义通道"）+ ``semantic_reason`` 写清为什么。
        """

        coverage["semantic_reason"] = str(channel.reason or "no_channel")
        coverage["semantic_reason_text"] = channel.reason_text
        coverage["semantic_scored"] = 0
        coverage["space"] = channel.fingerprint
        coverage["provider_id"] = channel.provider_id
        if mode == SEARCH_MODE_EMBEDDING:
            coverage["semantic"] = SEMANTIC_UNAVAILABLE
            coverage["scored"] = 0
            coverage["returned"] = 0
            return self._search_result(
                reason=SEARCH_UNAVAILABLE,
                note=f"这次找不了：{channel.reason_text}（你点名的是纯向量检索，没拿它换关键词）",
                coverage=coverage,
            )
        rows = self._keyword_order(matched)
        candidates = rows[:wanted]
        # 中间档两种降级分开报：通道本身没给（``pending_m4``，M3 的既有字面量）与
        # "通道给了但向量读不出来"（``degraded``）——后者是我们能修的，别混成一句。
        coverage["semantic"] = SEMANTIC_DEGRADED if channel.available else SEMANTIC_PENDING
        coverage["scored"] = len(rows)
        coverage["truncated"] = len(rows) > len(candidates)
        note = f"关键词匹配（语义这一档没跑：{channel.reason_text}）"
        return self._finish_search(candidates, coverage, note=note)

    # ---------------- M4：语义预选与软排序 ----------------

    def preselect(
        self,
        space: Any = None,
        query_text: Any = "",
        *,
        limit: Any = None,
        candidates: Any = None,
        channel: SemanticChannel | None = None,
        query_vector: Any = None,
        vectors: Any = None,
        query_vectors: Any = None,
        provider_id: Any = None,
        recipe_version: Any = "",
        min_score: Any = None,
        scope_kind: str = "",
        scope_id: str = "",
        keyword_tokens: Any = None,
    ) -> dict[str, Any]:
        """把候选拿向量打分做**预选**（方案 §10 M4；纯逻辑，自己不调模型）。

        第一个参数既可以是一份 :class:`SemanticChannel`（``preselect(chan, "想撒娇")``），
        也可以是向量空间（再配 ``query_vector=`` / ``vectors=`` / ``query_vectors=``）——
        两种写法最后都归一成同一个通道。

        **候选池**：``candidates=`` 给了就用它（中间档传的就是"关键词粗筛过 / 已过
        五态闸门的候选"），没给就自己走 :meth:`_eligible_rows` 取全部可用条目（纯向量
        档要的就是这个：关键词没命中的条目也得进池子）。无论哪种，条目都要过五态
        闸门——**向量只用来排序/预选，绝不扩权**。

        **向量从哪来**：``vectors`` 给了就用（``{entry_id: 向量}``），没给就试着从
        ``meme_vectors`` 注册表读（:meth:`load_vectors`；表还没加时如实回
        ``no_registry``）。查询向量**必须在通道里**：这一层算不了它。

        余弦打分复用既有设施 :func:`core.embedding_index.rank_by_cosine`（M4 不另造一套），
        换算方式是把 ``entry_id`` 映射成那个函数认的 ``topic_id``。

        返回 ``{"ok","reason","reason_text","candidates","coverage"}``：``ok=False``
        时 ``reason`` 是原因码（``no_space`` / ``no_query_vector`` / ``no_vectors`` /
        ``no_registry``…），候选**空着**而不是"随便挑几条"——没有一个可信的查询向量
        时，返回什么都比返回空更危险。

        ``coverage`` 里几个数是"这一轮语义到底看进去多少"：``offered`` 池子大小、
        ``scored`` 真参与余弦打分的条数、``no_vector`` 池子里没有可用向量的条数
        （``bad_vector`` 解不开 / ``dim_mismatch`` 维度不符都算在里面）、
        ``below_min_score`` 算出来了但低于下限的条数、``preselected`` 最终入选条数。
        """

        chan = channel if isinstance(channel, SemanticChannel) else None
        if chan is None:
            chan = semantic_channel(
                space,
                provider_id=provider_id,
                query_text=query_text,
                query_vector=query_vector,
                vectors=vectors,
                query_vectors=query_vectors,
                recipe_version=recipe_version,
            )
        floor = _number(
            SEMANTIC_MIN_SCORE if min_score is None else min_score, SEMANTIC_MIN_SCORE
        )
        wanted = None if limit is None else max(0, int(_number(limit, 0.0)))
        coverage: dict[str, Any] = {
            "mode": SEARCH_MODE_EMBEDDING,
            "semantic": SEMANTIC_UNAVAILABLE,
            "semantic_reason": str(chan.reason or "no_channel"),
            "semantic_reason_text": chan.reason_text,
            "space": chan.fingerprint,
            "provider_id": chan.provider_id,
            "recipe_version": chan.recipe_version,
            "scored": 0,
            "offered": 0,
            "preselected": 0,
            "below_min_score": 0,
            "no_vector": 0,
            "bad_vector": 0,
            "dim_mismatch": 0,
            "unavailable_skipped": 0,
            "limit": 0 if wanted is None else wanted,
            "min_score": floor,
        }
        if not chan.available:
            return self._preselect_result(
                coverage=coverage, reason=str(chan.reason or "no_channel")
            )
        if candidates is None:
            raw_rows, skipped = self._eligible_rows(
                scope_kind=scope_kind, scope_id=scope_id
            )
            coverage["unavailable_skipped"] = skipped
        else:
            raw_rows = [
                dict(row) for row in list(candidates or ()) if isinstance(row, Mapping)
            ]
        coverage["offered"] = len(raw_rows)
        if not raw_rows:
            return self._preselect_result(coverage=coverage, reason="no_vectors")
        tokens = (
            list(keyword_tokens) if keyword_tokens is not None else query_tokens(query_text)
        )
        entry_ids = [
            str(row.get("entry_id") or "")
            for row in raw_rows
            if str(row.get("entry_id") or "")
        ]
        table: dict[str, Any] = {}
        if isinstance(chan.vectors, Mapping):
            table = {str(key): value for key, value in chan.vectors.items()}
        if not table:
            table, load_reason = self.load_vectors(chan, entry_ids)
            if not table:
                coverage["semantic_reason"] = str(load_reason or "no_vectors")
                coverage["semantic_reason_text"] = SEMANTIC_REASON_LABELS.get(
                    str(load_reason), str(load_reason)
                )
                return self._preselect_result(
                    coverage=coverage, reason=str(load_reason or "no_vectors")
                )
        by_entry = {str(row.get("entry_id") or ""): row for row in raw_rows}
        vector_rows: list[dict[str, Any]] = []
        for entry_id, value in table.items():
            row = by_entry.get(str(entry_id))
            if row is None:
                # 向量的主人已经不在这场检索里（停用 / 删了 / 不在池子里）→ 直接丢，
                # 连"像不像"都不算。索引是副本，不是授权。
                continue
            components = _vector_components(value)
            if not components:
                coverage["bad_vector"] += 1
                continue
            if len(components) != len(chan.query_vector):
                coverage["dim_mismatch"] += 1
                continue
            vector_rows.append({"topic_id": str(entry_id), "vector": list(components)})
        coverage["no_vector"] = len(
            [
                entry_id
                for entry_id in entry_ids
                if entry_id not in {str(row["topic_id"]) for row in vector_rows}
            ]
        )
        if not vector_rows:
            return self._preselect_result(coverage=coverage, reason="no_vectors")
        # 先全量排序、再按下限过滤、最后才截断：反过来的话，top-k 里全部低于下限就会
        # 空手而归，而其实池子里有合规的候选（宁可少给，也不许因为截断顺序骗人）。
        ranked = rank_by_cosine(chan.query_vector, vector_rows, limit=len(vector_rows))
        coverage["scored"] = len(ranked)
        kept: list[dict[str, Any]] = []
        for entry_id, score in ranked:
            if _number(score) < floor:
                coverage["below_min_score"] += 1
                continue
            row = by_entry.get(str(entry_id))
            if row is None:
                continue
            item = self._candidate_item(row, tokens)
            item["vector_score"] = round(_number(score), 6)
            item["vector_note"] = (
                f"语义相近（余弦 {_number(score):.3f}）"
                + ("；关键词也撞上了" if _number(item.get("score")) > 0 else "；关键词没撞上")
            )
            kept.append(item)
        kept.sort(
            key=lambda item: (
                -_number(item.get("vector_score"), 0.0),
                -_number(item.get("score"), 0.0),
                str(item.get("entry_id") or ""),
            )
        )
        preselected = kept if wanted is None else kept[:wanted]
        coverage["preselected"] = len(preselected)
        coverage["semantic"] = SEMANTIC_USED
        coverage["semantic_reason"] = ""
        coverage["semantic_reason_text"] = SEMANTIC_REASON_LABELS[""]
        coverage["truncated"] = len(kept) > len(preselected)
        if not preselected:
            return self._preselect_result(coverage=coverage, reason="no_vectors")
        return self._preselect_result(coverage=coverage, candidates=preselected)

    @staticmethod
    def _preselect_result(
        *,
        coverage: Mapping[str, Any],
        reason: str = "",
        candidates: Any = (),
    ) -> dict[str, Any]:
        """预选结果的最小形状（``reason`` 空 = 成了）。"""

        rows = [dict(item) for item in list(candidates or ()) if isinstance(item, Mapping)]
        code = str(reason or "")
        return {
            "ok": not code and bool(rows),
            "reason": code,
            "reason_text": SEMANTIC_REASON_LABELS.get(
                code, str(dict(coverage).get("semantic_reason_text") or code)
            ),
            "candidates": rows,
            "coverage": dict(coverage),
        }

    def load_vectors(
        self, channel: SemanticChannel, entry_ids: Any, *, recipe_version: Any = None
    ) -> tuple[dict[str, Any], str]:
        """从 ``meme_vectors`` 注册表读候选向量：``({entry_id: 向量}, 原因码)``。

        **表还没加时如实回 ``no_registry``**（方案 §12.1 第 3 条：meme 向量独立成表、
        授权独立；DDL 与函数在交付报告的"待加清单"里，不在这里偷偷建）。读取函数名与
        签名固定成一条契约（:data:`MEME_VECTOR_LIST_FN`）：

        ``list_meme_vectors_for_entries(entry_ids, *, provider_id, space_fingerprint,
        recipe_version, status="ready") -> [{"entry_id":…, "vector": bytes|list}, …]``

        读出来的行只认**空间对得上**的：指纹不一致的行直接丢（换过 provider / 模型 /
        维度之后，旧向量比没有向量更糟——它会以"当前收藏"的身份被召回）。
        """

        ids = [str(item) for item in list(entry_ids or ()) if str(item)]
        if not ids:
            return {}, "no_vectors"
        if not channel.fingerprint or not channel.provider_id:
            return {}, "no_space"
        reader = getattr(self._db, MEME_VECTOR_LIST_FN, None)
        if not callable(reader):
            return {}, "no_registry"
        try:
            rows = reader(
                ids,
                provider_id=channel.provider_id,
                space_fingerprint=channel.fingerprint,
                recipe_version=(
                    channel.recipe_version if recipe_version is None else str(recipe_version)
                ),
                status="ready",
            )
        except Exception:
            return {}, "registry_error"
        table: dict[str, Any] = {}
        for row in list(rows or ()):
            if not isinstance(row, Mapping):
                continue
            entry_id = str(row.get("entry_id") or row.get("media_id") or "")
            if not entry_id:
                continue
            fingerprint = str(row.get("space_fingerprint") or "")
            if fingerprint and fingerprint != channel.fingerprint:
                continue
            table[entry_id] = row.get("vector")
        if not table:
            return {}, "no_vectors"
        return table, ""

    def soft_rank(
        self,
        candidates: Any,
        *,
        weights: Mapping[str, Any] | None = None,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """柔和地合成一个排序（纯逻辑，**不查库、不算向量**；口径见
        :func:`soft_rank_candidates`）。

        "最近用过几次"由调用方从 ``meme_usage`` 里数好放在 ``recent_uses`` 上
        （:meth:`search` 用 :meth:`usage_counts` 自己数）；这里只做纯排序，方便页面
        与测试拿几行假数据摆开来验。
        """

        return soft_rank_candidates(candidates, weights=weights, now=now)

    def usage_counts(self, entry_ids: Any, *, now: Any = 0.0) -> dict[str, int]:
        """``{entry_id: 最近成功发出去几次}``——软排序的"使用次数"就是这么来的。

        "最近"的口径是 ``world.meme.recent_use_window``（默认 20）：看该条目**最近
        ``recent_use_window`` 条用法记录**里 ``kind='sent'`` 的有几条（方案 §9 里这一格
        就是给软排序用的）。只查前 :data:`USAGE_LOOKUP_LIMIT` 条候选，避免一次检索变成
        几十次小查询；数不出来（库出错、参数不对）就当 0——**软排序少一个偏置，
        好过整轮检索炸掉**。
        """

        ids = [str(item) for item in list(entry_ids or ()) if str(item)]
        window = max(1, int(_number(getattr(self.settings, "recent_use_window", 20), 20)))
        counts: dict[str, int] = {}
        for entry_id in ids[:USAGE_LOOKUP_LIMIT]:
            try:
                rows = self._db.list_meme_usage(
                    entry_id=entry_id, kind=MEME_USAGE_SENT, limit=window
                )
            except Exception:
                rows = []
            counts[entry_id] = len([row for row in (rows or ()) if isinstance(row, Mapping)])
        return counts

    def _fill_usage(self, items: Any, *, now: Any = 0.0) -> list[dict[str, Any]]:
        """给候选补上 ``recent_uses``（已经有的不覆盖；只给前若干条查库）。"""

        rows = [dict(item) for item in list(items or ()) if isinstance(item, Mapping)]
        pending = [
            str(row.get("entry_id") or "")
            for row in rows[:USAGE_LOOKUP_LIMIT]
            if str(row.get("entry_id") or "") and "recent_uses" not in row
        ]
        counts = self.usage_counts(pending, now=now) if pending else {}
        for row in rows:
            if "recent_uses" not in row:
                row["recent_uses"] = int(counts.get(str(row.get("entry_id") or ""), 0))
        return rows

    @staticmethod
    def _search_result(
        *,
        reason: str,
        note: str = "",
        coverage: Mapping[str, Any] | None = None,
        candidates: Any = (),
    ) -> dict[str, Any]:
        """检索结果的最小形状（``ok`` 只认"真拿到候选"）。"""

        rows = [dict(item) for item in list(candidates or ()) if isinstance(item, Mapping)]
        return {
            "ok": reason == SEARCH_OK and bool(rows),
            "reason": str(reason or ""),
            "note": str(note or ""),
            "candidates": rows,
            "coverage": dict(coverage or {}),
            "receipt": "",
        }

    # ---------------- M5：资产事实（真尺寸 / 真帧数 / 按需取帧） ----------------

    def _limits(self, max_bytes: Any, max_frames: Any) -> tuple[int, int]:
        """这一层探测用的字节 / 帧数上限（默认取生效配置）。

        ``0`` 一律当"不限"（与 ``allocate_image_budget`` 对单图字节上限的处理一致），
        所以这里**不用** ``or`` 兜底——那会把用户填的 0 悄悄换成默认值。
        """

        settings = self.settings
        if max_bytes is None:
            raw_bytes = getattr(settings, "max_asset_bytes", None)
            limit_bytes = (
                meme_images.DEFAULT_MAX_BYTES
                if raw_bytes is None
                else max(0, int(_number(raw_bytes, meme_images.DEFAULT_MAX_BYTES)))
            )
        else:
            limit_bytes = max(0, int(_number(max_bytes, 0.0)))
        if max_frames is None:
            raw_frames = getattr(settings, "max_asset_frames", None)
            limit_frames = (
                meme_images.DEFAULT_MAX_FRAMES
                if raw_frames is None
                else max(0, int(_number(raw_frames, meme_images.DEFAULT_MAX_FRAMES)))
            )
        else:
            limit_frames = max(0, int(_number(max_frames, 0.0)))
        return limit_bytes, limit_frames

    def _media_bytes(self, media_id: Any, media_store: Any = None) -> bytes | None:
        """从资产库读一张图的字节（没给资产库就用构造时那份；取不到回 ``None``）。"""

        store = media_store if media_store is not None else self._media_store
        if store is None:
            return None
        reader = getattr(store, "open_bytes", None)
        if not callable(reader):
            return None
        key = str(media_id or "").strip()
        if not key:
            return None
        try:
            data = reader(key)
        except Exception:
            return None
        return data if isinstance(data, (bytes, bytearray)) else None

    def probe_media(
        self,
        media_store: Any = None,
        media_id: Any = None,
        *,
        hashes: bool = False,
        max_bytes: Any = None,
        max_frames: Any = None,
    ) -> dict[str, Any]:
        """读资产字节 → 真实图片事实（尺寸 / 帧数 / 动图 / 可选哈希）。**只读，不写库。**

        ``probe_media(media_store, media_id)`` 与 ``probe_media(media_id)`` 两种叫法
        都认（第二个位置参数是真身；第一个位置给了字符串就自动当成 ``media_id``，
        资产库用构造时那份）——M5 的两个调用方（引擎、管理页）手里有什么就传什么。

        返回 :func:`core.meme_images.probe_bytes` 的那份事实 + ``media_id``：坏图 /
        截断 / 超大 / 没装 Pillow 一律 ``ok=False`` + 中文原因，**绝不抛、绝不编尺寸**。
        单张字节上限与帧数上限默认取 ``max_asset_bytes`` / ``max_asset_frames``。
        """

        if media_id is None and isinstance(media_store, (str, bytes)):
            media_id, media_store = media_store, None
        key = str(media_id or "").strip()
        limit_bytes, limit_frames = self._limits(max_bytes, max_frames)
        if not key:
            facts = meme_images.probe_bytes(
                b"", max_bytes=limit_bytes, max_frames=limit_frames, hashes=hashes
            )
            facts["media_id"] = ""
            return facts
        data = self._media_bytes(key, media_store)
        if data is None:
            return {
                "ok": False,
                "reason": "no_bytes",
                "reason_text": meme_images.PROBE_REASON_TEXTS["no_bytes"],
                "media_id": key,
                "width": 0,
                "height": 0,
                "frame_count": 0,
                "is_animated": False,
                "hashes": bool(hashes),
            }
        facts = meme_images.probe_bytes(
            data, max_bytes=limit_bytes, max_frames=limit_frames, hashes=hashes
        )
        facts["media_id"] = key
        return facts

    def backfill_asset_facts(
        self,
        *,
        media_store: Any = None,
        limit: int = 200,
        dry_run: bool = False,
        only_missing: bool = True,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """把既有行的资产事实补齐（**幂等、只补空的、可 dry-run**）。

        为什么需要它：M1~M4 入库的 ``meme_assets`` 行只有 mime / size，
        ``width`` / ``height`` / ``frame_count`` 全是 0，"补齐"这件事必须能重复跑、
        不能覆盖任何已有值。所以写回走的是 ``insert_meme_asset``——它从 M1 起就是
        "冲突只补空字段、不动 created_at"的语义，这里**不另写一套 UPDATE**。

        扫描对象是"想得起来的所有 ``media_id``"：``meme_assets`` 里的行 + 每条收藏
        （含软删，``include_deleted=True``）指向的图。后者能把"M2 遗留的、连资产行都
        没有的收藏"补出一行来（``created`` 计数）。

        * ``only_missing=True``（默认）：只动"没有尺寸"的行（``width`` 或 ``height``
          为 0 / 根本没这行）；已有尺寸的算 ``skipped``，一个字节都不重读。
        * ``dry_run=True``：**一个字节都不写**，``created`` / ``filled`` 是"将会怎样"，
          ``plan`` 列出前几条要改什么。
        * 探不出来的（坏图 / 截断 / 超大 / 没装 Pillow）记进 ``failures``（原因码 → 条数），
          既不写半行、也不编尺寸；Pillow 缺失时整个函数直接 ``ok=False`` 且不扫。
        * 幂等：连着跑两遍，第二遍应当全是 ``skipped`` / ``unchanged``。
        """

        result: dict[str, Any] = {
            "ok": True,
            "reason": "",
            "reason_text": "",
            "scanned": 0,
            "created": 0,
            "filled": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "failures": {},
            "plan": [],
            "dry_run": bool(dry_run),
            "only_missing": bool(only_missing),
        }
        if not meme_images.PILLOW_AVAILABLE:
            return {
                **result,
                "ok": False,
                "reason": "pillow_missing",
                "reason_text": meme_images.PROBE_REASON_TEXTS["pillow_missing"],
            }
        cap = max(1, int(_number(limit, 200)))
        targets: list[str] = []
        known: dict[str, dict[str, Any]] = {}
        for asset in self._db.list_meme_assets(limit=min(cap, 5000)):
            if not isinstance(asset, Mapping):
                continue
            key = str(asset.get("media_id") or "").strip()
            if key and key not in known:
                known[key] = dict(asset)
                targets.append(key)
        for entry in self._db.list_meme_entries(include_deleted=True, limit=min(cap, 1000)):
            key = str(entry.get("media_id") or "").strip()
            if key and key not in known:
                known[key] = {}
                targets.append(key)
        limit_bytes, limit_frames = self._limits(None, None)
        for key in targets[:cap]:
            result["scanned"] += 1
            before = known.get(key) or self._db.get_meme_asset(key) or {}
            has_size = bool(int(before.get("width") or 0)) and bool(
                int(before.get("height") or 0)
            )
            if only_missing and has_size:
                result["skipped"] += 1
                continue
            facts = self.probe_media(
                media_store, key, max_bytes=limit_bytes, max_frames=limit_frames
            )
            if not facts.get("ok"):
                code = str(facts.get("reason") or "decode_error")
                result["failed"] += 1
                result["failures"][code] = int(result["failures"].get(code) or 0) + 1
                continue
            is_new = not before
            changed = any(
                int(before.get(field) or 0) != int(facts.get(field) or 0)
                for field in ("width", "height", "frame_count")
            ) or str(before.get("mime") or "") != str(facts.get("mime") or "")
            if len(result["plan"]) < 20:
                result["plan"].append(
                    {
                        "media_id": key,
                        "created": is_new,
                        "width": int(facts.get("width") or 0),
                        "height": int(facts.get("height") or 0),
                        "frame_count": int(facts.get("frame_count") or 0),
                        "is_animated": bool(facts.get("is_animated")),
                        "mime": str(facts.get("mime") or ""),
                    }
                )
            if dry_run:
                if is_new:
                    result["created"] += 1
                elif changed:
                    result["filled"] += 1
                else:
                    result["unchanged"] += 1
                continue
            self._db.insert_meme_asset(
                media_id=key,
                mime=str(facts.get("mime") or "") or str(before.get("mime") or ""),
                size_bytes=int(facts.get("size_bytes") or before.get("size_bytes") or 0),
                content_hash=str(before.get("content_hash") or key),
                width=int(facts.get("width") or 0),
                height=int(facts.get("height") or 0),
                frame_count=int(facts.get("frame_count") or 0),
                is_animated=bool(facts.get("is_animated")),
                tags=str(before.get("tags") or ""),
                now=now,
            )
            after = self._db.get_meme_asset(key) or {}
            if is_new:
                result["created"] += 1
            elif any(
                int(before.get(field) or 0) != int(after.get(field) or 0)
                for field in ("width", "height", "frame_count")
            ) or str(before.get("mime") or "") != str(after.get("mime") or ""):
                result["filled"] += 1
            else:
                result["unchanged"] += 1
        return result

    def asset_profile(
        self,
        media_id: Any,
        *,
        media_store: Any = None,
        max_bytes: Any = None,
        max_frames: Any = None,
    ) -> dict[str, Any]:
        """一条资产的"图片画像"：真事实 + 感知哈希（变体判定的输入，**只读**）。

        探测成功才 ``ok=True``（变体判定只认这份）；探不到时仍然把注册表里的旧事实
        （尺寸 / 帧数）带出来给页面看，但 ``ok=False`` + 原因，**绝不拿旧事实去判"像"**。
        """

        key = str(media_id or "").strip()
        row = self._db.get_meme_asset(key) if key else None
        limit_bytes, limit_frames = self._limits(max_bytes, max_frames)
        facts = self.probe_media(
            media_store, key, hashes=True, max_bytes=limit_bytes, max_frames=limit_frames
        )
        profile: dict[str, Any] = {
            "media_id": key,
            "ok": bool(facts.get("ok")),
            "reason": str(facts.get("reason") or ""),
            "reason_text": str(
                facts.get("reason_text")
                or meme_images.PROBE_REASON_TEXTS.get(str(facts.get("reason") or ""), "")
            ),
            "mime": str(facts.get("mime") or (row or {}).get("mime") or ""),
            "size_bytes": int(facts.get("size_bytes") or (row or {}).get("size_bytes") or 0),
            "width": int(facts.get("width") or (row or {}).get("width") or 0),
            "height": int(facts.get("height") or (row or {}).get("height") or 0),
            "frame_count": int(facts.get("frame_count") or (row or {}).get("frame_count") or 0),
            "is_animated": bool(
                facts.get("is_animated")
                if facts.get("ok")
                else (row or {}).get("is_animated")
            ),
            "pixel_hash": str(facts.get("pixel_hash") or ""),
            "dhash": str(facts.get("dhash") or ""),
            "dhash_flat": bool(facts.get("dhash_flat")),
            "truncated": bool(facts.get("truncated")),
        }
        return profile

    def find_variants(
        self,
        *,
        entry_ids: Any = None,
        media_store: Any = None,
        limit: int = 200,
        dry_run: bool = False,
        dhash_max_distance: Any = None,
        max_pairs: Any = MAX_VARIANT_PAIRS,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """扫一遍收藏，把**疑似变体**登记进 ``meme_variants``（方案 §1 / §8）。

        "四种像"里这里管两种：**同一画面不同编码**（规范化解码像素哈希相等）与
        **近似变体**（64 位 dHash 汉明距离 ≤ ``dhash_max_distance``，默认
        :data:`core.meme_images.DHASH_MAX_DISTANCE`）；帧数差异**永远**写进差异说明，
        而且判出来的关系一律只是 ``suspected``。

        **铁律：绝不合并。** 这个函数只往 ``meme_variants`` 写关系行，**一个字节都不
        动** ``meme_entries`` / ``meme_assets``（换字图、不同帧数的 GIF 都得原样留着，
        合并只能由人来做）。``confirmed`` 也不在这里写——机器不许自己确认。

        ``dry_run=True`` 时只算不写。探不到画像（坏图 / 没装 Pillow）的按 ``unprobed``
        计数、**不猜**；配对超过 ``max_pairs`` 时如实标 ``truncated``（不装作扫完了）。
        """

        result: dict[str, Any] = {
            "ok": True,
            "reason": "",
            "scanned": 0,
            "profiled": 0,
            "unprobed": 0,
            "pairs": 0,
            "registered": 0,
            "existing": 0,
            "planned": [],
            "dry_run": bool(dry_run),
            "truncated": False,
            "dhash_max_distance": int(
                meme_images.DHASH_MAX_DISTANCE
                if dhash_max_distance is None
                else _number(dhash_max_distance, meme_images.DHASH_MAX_DISTANCE)
            ),
            "decoder_available": bool(meme_images.PILLOW_AVAILABLE),
        }
        if not meme_images.PILLOW_AVAILABLE:
            result.update(ok=False, reason="pillow_missing")
            return result
        rows: list[dict[str, Any]] = []
        if entry_ids is not None:
            for entry_id in list(entry_ids or ())[: max(1, int(_number(limit, 200)))]:
                row = self._db.get_meme_entry(str(entry_id or ""))
                if isinstance(row, Mapping):
                    rows.append(dict(row))
        else:
            rows = [
                dict(row)
                for row in self._db.list_meme_entries(
                    include_deleted=True, limit=max(1, min(int(_number(limit, 200)), 500))
                )
                if isinstance(row, Mapping)
            ]
        result["scanned"] = len(rows)
        profiles: dict[str, dict[str, Any]] = {}
        items: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            entry_id = str(row.get("entry_id") or "")
            media_id = str(row.get("media_id") or "").strip()
            if not entry_id or not media_id:
                continue
            if media_id not in profiles:
                profiles[media_id] = self.asset_profile(media_id, media_store=media_store)
            profile = profiles[media_id]
            if not profile.get("ok"):
                result["unprobed"] += 1
                continue
            items.append((entry_id, media_id, profile))
        result["profiled"] = len(items)
        cap = max(0, int(_number(max_pairs, MAX_VARIANT_PAIRS)))
        stop = False
        for index, (left_id, left_media, left) in enumerate(items):
            if stop:
                break
            for right_id, right_media, right in items[index + 1 :]:
                if result["pairs"] >= cap:
                    result["truncated"] = True
                    stop = True
                    break
                if left_id == right_id:
                    continue
                result["pairs"] += 1
                decision = meme_images.variant_decision(
                    left, right, dhash_max_distance=result["dhash_max_distance"]
                )
                if decision.get("relation") != "suspected":
                    continue
                if len(result["planned"]) < 20:
                    result["planned"].append(
                        {
                            "entry_a": sorted((left_id, right_id))[0],
                            "entry_b": sorted((left_id, right_id))[1],
                            "media_a": left_media,
                            "media_b": right_media,
                            "similarity": round(_number(decision.get("similarity")), 6),
                            "reason": str(decision.get("reason") or ""),
                            "diff_note": str(decision.get("diff_note") or ""),
                        }
                    )
                if dry_run:
                    result["registered"] += 1
                    continue
                written = self._db.add_meme_variant(
                    entry_a=left_id,
                    entry_b=right_id,
                    relation="suspected",
                    similarity=_number(decision.get("similarity")),
                    diff_note=str(decision.get("diff_note") or ""),
                    now=now,
                )
                if written.get("created"):
                    result["registered"] += 1
                elif written.get("ok"):
                    result["existing"] += 1
        return result

    def variant_relations(self, entry_id: Any) -> list[dict[str, Any]]:
        """一条收藏牵扯到的全部变体关系（两个方向都算，只读）。"""

        return self._db.list_meme_variants(str(entry_id or ""))

    def confirm_variant(
        self,
        entry_a: Any,
        entry_b: Any,
        *,
        note: Any = "",
        similarity: Any = 0.0,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """人工确认一条变体关系（``relation="confirmed"``）——**只有人能调**。

        机器侧（:meth:`find_variants`）永远只写 ``suspected``；"这两张确实是一个梗"
        得由人在管理页点头。已知限制：``core/db.py`` 现在的 ``add_meme_variant`` 对
        已存在的行**只回不改**，所以"把一条 suspected 升级成 confirmed"需要库里
        加一个更新函数（见交付报告的待加清单）；在那之前这条路径如实回
        ``relation_update_unsupported``，**绝不假装确认成功**。
        """

        existing = None
        pair = {str(entry_a or ""), str(entry_b or "")}
        if "" not in pair and len(pair) == 2:
            for row in self.variant_relations(entry_a):
                if {str(row.get("entry_a") or ""), str(row.get("entry_b") or "")} == pair:
                    existing = dict(row)
                    break
        if existing is not None:
            return {
                "ok": False,
                "reason": "relation_update_unsupported",
                "note": (
                    "库里已有一条疑似关系，但 core/db.py 还没有「更新变体关系」的函数，"
                    "这次没有改动它（见待加清单）"
                ),
                "variant": existing,
            }
        written = self._db.add_meme_variant(
            entry_a=entry_a,
            entry_b=entry_b,
            relation="confirmed",
            similarity=similarity,
            diff_note=str(note or "人工确认"),
            now=now,
        )
        return {
            "ok": bool(written.get("ok")),
            "reason": "" if written.get("ok") else str(written.get("reason") or ""),
            "variant": dict(written.get("variant") or {}),
        }

    def unregister_variant(self, entry_a: Any, entry_b: Any) -> dict[str, Any]:
        """取消一条变体关系（只在疑似误判时用；确认过的关系不该被静默丢掉）。"""

        removed = bool(self._db.remove_meme_variant(str(entry_a or ""), str(entry_b or "")))
        return {
            "ok": removed,
            "reason": "" if removed else "not_found",
            "entry_a": str(entry_a or ""),
            "entry_b": str(entry_b or ""),
        }

    def extract_frame(
        self,
        media_id: Any,
        *,
        index: Any = 0,
        middle: bool = False,
        media_store: Any = None,
        max_bytes: Any = None,
        max_frames: Any = None,
    ) -> dict[str, Any]:
        """**只取需要的那一帧**（GIF / WebP 多帧也一样）：卡片用第 0 帧，
        内容判定用 ``middle=True``（中间帧，方案 §10 M5）。

        实现上只 ``seek`` 目标帧一次、只 ``load()`` 一次——不把整条动图解进内存。
        帧号越界 / 坏图 / 截断 / 超大一律 ``ok=False`` + 原因，绝不抛。
        """

        key = str(media_id or "").strip()
        limit_bytes, limit_frames = self._limits(max_bytes, max_frames)
        data = self._media_bytes(key, media_store)
        if data is None:
            return {
                "ok": False,
                "reason": "no_bytes",
                "reason_text": meme_images.PROBE_REASON_TEXTS["no_bytes"],
                "index": 0,
                "frame_count": 0,
                "bytes": b"",
                "size_bytes": 0,
                "media_id": key,
            }
        wanted = index
        if middle:
            declared = meme_images.probe_bytes(
                data, max_bytes=limit_bytes, max_frames=limit_frames
            )
            if not declared.get("ok"):
                return {
                    "ok": False,
                    "reason": str(declared.get("reason") or "decode_error"),
                    "reason_text": str(declared.get("reason_text") or ""),
                    "index": 0,
                    "frame_count": 0,
                    "bytes": b"",
                    "size_bytes": 0,
                    "media_id": key,
                }
            wanted = meme_images.middle_frame_index(declared.get("frame_count"))
        frame = meme_images.extract_frame(
            data, wanted, max_bytes=limit_bytes, max_frames=limit_frames
        )
        frame["media_id"] = key
        return frame

    def candidate_preview_items(
        self, candidates: Any, *, media_source: Any = None
    ) -> list[dict[str, Any]]:
        """候选 → 第③级预算吃的预览图条目（方案 §4）。

        预览的地址**由调用方给**（引擎的 ``media_file_source``）：这一层不知道
        本机文件长什么样，也不该猜。取不到地址的候选**不出现在预览里**——它照样
        留在文字候选清单里（靠真实编号指认），只是没有图。
        """

        items: list[dict[str, Any]] = []
        for row in list(candidates or ()):
            if not isinstance(row, Mapping):
                continue
            media_id = str(row.get("media_id") or "").strip()
            if not media_id:
                continue
            url = ""
            if callable(media_source):
                try:
                    url = str(media_source(media_id) or "").strip()
                except Exception:
                    url = ""
            if not url:
                continue
            items.append(
                {
                    "url": url,
                    "media_id": media_id,
                    "entry_id": str(row.get("entry_id") or ""),
                    "label": str(row.get("label") or ""),
                    "size_bytes": int(row.get("size_bytes") or 0),
                    "tier": "previews",
                }
            )
        return items

    # ---------------- M3：发送前复核与配额 ----------------

    def send_view(
        self, entry_id: Any, *, scope_kind: str = "", scope_id: str = ""
    ) -> dict[str, Any]:
        """``send_meme`` 的发送前复核（§6）：真实编号 → 条目 + 五态 + 能不能发。

        闸门顺序照 §2.1：**总开关 → 条目可用状态**（范围维度已取消，见
        :meth:`scope_allowed`）。查不到编号与"查到了但现在不能用"必须分开报
        （``state_unknown`` vs 五态），否则她会以为"这张图我收过"。
        """

        result: dict[str, Any] = {
            "ok": False,
            "reason": STATE_UNKNOWN,
            "reason_text": gate_reason_text(STATE_UNKNOWN),
            "entry_id": "",
            "media_id": "",
            "name": "",
            "state": "",
            "label": "",
            "file_present": False,
            "entry": None,
        }
        config_gate = self.settings.gate_config()
        if config_gate != GATE_OK:
            result["reason"] = config_gate
            result["reason_text"] = gate_reason_text(config_gate)
            return result
        key = str(entry_id or "").strip()
        row = self._db.get_meme_entry(key) if key else None
        if row is None:
            return result
        media_id = str(row.get("media_id") or "").strip()
        present = self.file_present(media_id) if media_id else False
        allowed = True
        if scope_kind:
            allowed = self.scope_allowed(key, scope_kind=scope_kind, scope_id=scope_id)
        state = meme_availability(row, file_present=present, scope_allowed=allowed)
        reason = self.settings.gate_state(state)
        # ``gate_state`` 对五态里的四种回的就是状态本身（M1 的命名如此），
        # 所以文案要按"能翻出来的是状态就用状态文案"取，别把原始码丢给用户看。
        reason_text = (
            gate_reason_text(reason)
            if reason == GATE_OK
            else _state_reason(state) or gate_reason_text(reason)
        )
        result.update(
            {
                "ok": reason == GATE_OK,
                "reason": reason,
                "reason_text": reason_text,
                "entry_id": key,
                "media_id": media_id,
                "name": str(row.get("name") or ""),
                "state": state,
                "label": availability_label(state),
                "file_present": present,
                "entry": row,
            }
        )
        return result

    def sent_count(
        self,
        *,
        window_seconds: Any = None,
        since: Any = None,
        session_id: Any = "",
        now: Any = 0.0,
    ) -> int:
        """配额窗口内**平台确认收下**的表情包张数（§6：只有真发出去才算数）。

        ``since`` 给了就按它算（引擎用它给"世界本地日零点"），否则按
        ``window_seconds`` 从此刻往回数。数的是 ``meme_usage`` 里 ``sent`` 那些行：
        尝试与失败**不占额度**——她没发出去，就不该被自己的失败扣掉一张。
        """

        moment = float(now or self._clock())
        if since is not None:
            start = float(since)
        else:
            start = moment - max(0.0, float(window_seconds or 0.0))
        return int(
            self._db.count_meme_usage(
                kind=MEME_USAGE_SENT,
                session_id=str(session_id or ""),
                since=start,
            )
        )

    def record_usage(
        self,
        entry_id: Any,
        *,
        kind: Any = MEME_USAGE_ATTEMPTED,
        session_id: Any = "",
        phase_id: Any = "",
        outcome: Any = "",
        reason: Any = "",
        detail: Mapping[str, Any] | None = None,
        now: Any = 0.0,
    ) -> int:
        """记一次用法 / 回执（尝试 → 发出去了 → 平台回执）。

        §11：统计"收了多少、用过多少、误发多少"一律靠这些真实记录，不靠判定记录。
        """

        key = str(entry_id or "").strip()
        if not key:
            return 0
        return int(
            self._db.add_meme_usage(
                entry_id=key,
                session_id=str(session_id or ""),
                phase_id=str(phase_id or ""),
                kind=str(kind or MEME_USAGE_ATTEMPTED),
                outcome=str(outcome or ""),
                reason=str(reason or ""),
                detail=dict(detail or {}),
                now=now,
            )
        )

    def collect(
        self,
        *,
        media_id: Any,
        name: Any = "",
        scope_id: Any = "",
        persona_id: Any = "",
        meaning: Any = "",
        use_when: Any = "",
        avoid_when: Any = "",
        personal_note: Any = "",
        preference: Any = 0.0,
        source: Mapping[str, Any] | None = None,
        asset: Mapping[str, Any] | None = None,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """收藏一张图（已收藏过的那一条**不新建**，直接返回它）。

        返回 ``{"ok", "created", "entry_id", "state", "entry"}``；``created=False``
        表示"之前就收过了"——调用方按这个决定回什么话（§6 的第一态）。
        同一来源重复登记只留一行（幂等）。
        """

        key = str(media_id or "").strip()
        if not key:
            return {"ok": False, "reason": "no_media", "created": False, "entry_id": ""}
        existing = self.find_by_media(key)
        if existing is not None:
            entry = dict(existing)
            entry.pop("created", None)
            state = self._record_state(entry)
            entry_id = str(entry.get("entry_id") or "")
            if entry_id and state != STATE_DELETED:
                # 之前收过的（哪怕是被停用的）也补一次钉：M1 建的那批记录没有钉
                self.pin_entry(entry_id, media_id=key)
            return {
                "ok": True,
                "created": False,
                "entry_id": entry_id,
                "state": state,
                "entry": entry,
            }
        if asset:
            self._db.insert_meme_asset(
                media_id=key,
                mime=asset.get("mime", ""),
                size_bytes=asset.get("size_bytes", 0),
                content_hash=asset.get("content_hash", key),
                width=asset.get("width", 0),
                height=asset.get("height", 0),
                frame_count=asset.get("frame_count", 0),
                is_animated=asset.get("is_animated", False),
                tags=asset.get("tags", ""),
                now=now,
            )
        row = self._db.create_meme_entry(
            media_id=key,
            name=name,
            scope_id=scope_id,
            persona_id=persona_id,
            meaning=meaning,
            use_when=use_when,
            avoid_when=avoid_when,
            personal_note=personal_note,
            preference=preference,
            now=now,
        )
        created = bool(row.pop("created", True))
        entry_id = str(row.get("entry_id") or "")
        if entry_id:
            # 新收下的图钉住，别让资产库 GC 把收藏指向的文件删掉（M1 遗留）
            self.pin_entry(entry_id, media_id=key)
        if source and entry_id:
            self._db.add_meme_source(
                entry_id=entry_id,
                session_id=source.get("session_id", ""),
                message_id=source.get("message_id", ""),
                sender_id=source.get("sender_id", ""),
                origin=source.get("origin", ""),
                origin_kind=source.get("origin_kind", ""),
                allowed_scopes=source.get("allowed_scopes", SCOPE_ANY),
                occurred_at=source.get("occurred_at", 0.0),
                now=now,
            )
        return {
            "ok": True,
            "created": created,
            "entry_id": entry_id,
            "state": self._record_state(row),
            "entry": row,
        }

    def collect_image(
        self,
        *,
        media_id: Any,
        scope_kind: str = "",
        scope_id: Any = "",
        name: Any = "",
        meaning: Any = "",
        use_when: Any = "",
        avoid_when: Any = "",
        personal_note: Any = "",
        source: Mapping[str, Any] | None = None,
        asset: Mapping[str, Any] | None = None,
        now: Any = 0.0,
    ) -> dict[str, Any]:
        """``collect_meme`` 动作的数据侧：一张图 + 主模型这次写的语义 → 结果与回执。

        分态顺序（§6）：**先判文件与范围，再看记录在不在**——"已经收过了"
        "之前收过但被停用""记录删了但文件还在""文件不见了"四种回话完全不同，
        不能糊成一句"收不了"。已收藏的那条**不覆盖**（语义是第一人称写的，
        重收时按原有语义回话）。

        ``asset`` 是资产库里的实情（见 :func:`asset_facts`）：**两个调用方都要给**，
        不给的话新记录在 ``meme_assets`` 里没有那一行，卡片与详情里的
        「类型 / 大小 / 动图」就全是空的（M2 遗留，2026-10-16 真机踩到）。
        """

        key = str(media_id or "").strip()
        if not key:
            return {
                "ok": False,
                "created": False,
                "entry_id": "",
                "state": "",
                "entry": None,
                "receipt": collect_receipt(state="", reason="no_media"),
            }
        row = self.find_by_media(key, include_deleted=True)
        if row is not None:
            entry_id = str(row.get("entry_id") or "")
            present = self.file_present(key)
            allowed = True
            if scope_kind:
                allowed = self.scope_allowed(
                    entry_id, scope_kind=scope_kind, scope_id=scope_id
                )
            state = meme_availability(row, file_present=present, scope_allowed=allowed)
            entry_name = str(row.get("name") or "") or str(name or "")
            return {
                "ok": state == STATE_ENABLED,
                "created": False,
                "entry_id": entry_id,
                "state": state,
                "entry": row,
                "receipt": collect_receipt(
                    state=state, entry_id=entry_id, name=entry_name, created=False
                ),
            }
        if not self.file_present(key):
            return {
                "ok": False,
                "created": False,
                "entry_id": "",
                "state": STATE_MISSING,
                "entry": None,
                "receipt": collect_receipt(state=STATE_MISSING),
            }
        result = self.collect(
            media_id=key,
            name=name,
            scope_id=scope_id,
            meaning=meaning,
            use_when=use_when,
            avoid_when=avoid_when,
            personal_note=personal_note,
            source=source,
            asset=asset,
            now=now,
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "created": False,
                "entry_id": "",
                "state": "",
                "entry": None,
                "receipt": collect_receipt(
                    state="", reason=str(result.get("reason") or "")
                ),
            }
        entry = dict(result.get("entry") or {})
        entry_id = str(result.get("entry_id") or "")
        state = str(result.get("state") or STATE_ENABLED)
        created = bool(result.get("created"))
        return {
            "ok": True,
            "created": created,
            "entry_id": entry_id,
            "state": state,
            "entry": entry,
            "receipt": collect_receipt(
                state=state,
                entry_id=entry_id,
                name=str(entry.get("name") or name or ""),
                created=created,
            ),
        }

    def update(
        self,
        entry_id: Any,
        *,
        expected_revision: Any = None,
        now: Any = 0.0,
        **fields: Any,
    ) -> dict[str, Any]:
        """改语义字段（``name`` / ``meaning`` / ``use_when`` / ``avoid_when`` /
        ``personal_note`` / ``preference`` / ``pinned`` / ``scope_id`` /
        ``persona_id``）。值没变则 ``revision`` 不动。"""

        return self._db.update_meme_entry(
            str(entry_id or ""), expected_revision=expected_revision, now=now, **fields
        )

    def set_status(self, entry_id: Any, status: Any, *, now: Any = 0.0) -> dict[str, Any]:
        """启用 / 禁用 / 软删一条收藏（**只动记录，不碰底层文件**）。

        这就是"禁用/删除要区分收藏记录与底层文件"里"记录那一半"的落点：底层文件
        归资产库管，删文件是另一件事（见 :meth:`cleanup_assets`）。
        """

        wanted = str(status or "").strip()
        if wanted not in RECORD_STATES:
            return {"ok": False, "reason": "bad_status", "entry_id": str(entry_id or "")}
        row = self._db.set_meme_entry_status(str(entry_id or ""), wanted, now=now)
        if row is None:
            return {"ok": False, "reason": "not_found", "entry_id": str(entry_id or "")}
        media_id = str(row.get("media_id") or "").strip()
        if wanted == MEME_ENTRY_STATUS_DELETED:
            # 记录删了就不再钉着底层文件（她能恢复，恢复时再钉回来）
            self.unpin_entry(str(entry_id or ""), media_id=media_id)
        else:
            self.pin_entry(str(entry_id or ""), media_id=media_id)
        return {"ok": True, "entry": row, "status": wanted}

    def enable(self, entry_id: Any, **kwargs: Any) -> dict[str, Any]:
        return self.set_status(entry_id, MEME_ENTRY_STATUS_ENABLED, **kwargs)

    def disable(self, entry_id: Any, **kwargs: Any) -> dict[str, Any]:
        return self.set_status(entry_id, MEME_ENTRY_STATUS_DISABLED, **kwargs)

    def soft_delete(self, entry_id: Any, **kwargs: Any) -> dict[str, Any]:
        """软删：记录留着（可恢复），底层文件不动。"""

        return self.set_status(entry_id, MEME_ENTRY_STATUS_DELETED, **kwargs)

    # ---------------- 清理 ----------------

    def cleanup_exposures(self, *, now: Any = 0.0) -> int:
        """按 ``exposure_keep_days`` 清展示记录（``<= 0`` 表示不过期）。"""

        return self._db.cleanup_meme_exposures(
            keep_days=self.settings.exposure_keep_days, now=now
        )

    def maintenance(self, *, now: Any = 0.0) -> dict[str, int]:
        """按天维护：清过期的展示记录与陈旧的用法记录（**幂等**，可以重复调用）。

        M3 把它挂到引擎既有的日结路径（新的一天那一拍）上：``cleanup_exposures``
        从 M1 就写好了，但一直没人调，展示记录只会越攒越多（方案 §3.3 说好"随聊天
        留档一起过期"）。

        用法记录留得比展示记录久（至少 90 天）：小时 / 按天的配额要靠它数，
        展示记录过期了也不该让"这张图今天发过没有"这件事失忆。
        """

        keep_days = max(90, int(self.settings.exposure_keep_days or 30))
        return {
            "exposures": int(self.cleanup_exposures(now=now)),
            "usage": int(self.cleanup_usage(keep_days=keep_days, now=now)),
        }

    def cleanup_usage(self, *, keep_days: int = 90, now: Any = 0.0) -> int:
        """按天数清用法记录。"""

        return self._db.cleanup_meme_usage(keep_days=keep_days, now=now)

    def forget(self, entry_id: Any) -> dict[str, Any]:
        """只删**记录**（收藏/来源/变体/用法），把那块图该不该删算清楚，**不动文件**。

        用户 m21273：「删除好卡，你后台异步删就可以了，把删除记录结果返回就行」。
        所以页面这条路径先调这个（纯数据库动作，毫秒级），再由调用方把
        :meth:`discard_media` 丢到后台。返回值里 ``media_id`` / ``file_pending``
        说明后台还有没有活要干。
        """

        target = str(entry_id or "").strip()
        result: dict[str, Any] = {
            "ok": False,
            "id": target,
            "reason": "",
            "media_id": "",
            "file_pending": False,
            "file_removed": False,
            "assets": 0,
        }
        if not target:
            result["reason"] = "没给收藏编号"
            return result
        entry = None
        try:
            entry = self._db.get_meme_entry(target)
        except Exception:  # noqa: BLE001
            entry = None
        removed = bool(self._db.purge_meme_entry(target))
        if not removed:
            result["reason"] = f"没有这个收藏：{target}"
            return result
        media_id = str((entry or {}).get("media_id") or "").strip()
        result["media_id"] = media_id
        if media_id:
            # 记录没了，钉也要跟着解：否则底层的"被 pin 住不动"会把文件一直留下，
            # 页面就永远报"图片文件还被别处引用"（用户 2026-10-29：删就该连图一起删）。
            try:
                self.unpin_entry(target, media_id=media_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            result["assets"] = int(self._db.cleanup_meme_assets())
        except Exception:  # noqa: BLE001
            result["assets"] = 0
        if media_id:
            still_used = []
            try:
                still_used = [
                    row
                    for row in self._db.list_meme_entries(media_id=media_id, limit=2)
                    if str(row.get("entry_id") or "") != target
                ]
            except Exception:  # noqa: BLE001
                still_used = []
            result["file_pending"] = bool(not still_used and self._media_store is not None)
        result["ok"] = True
        result["note"] = "收藏记录删掉了。"
        return result

    def discard_media(self, media_id: Any) -> bool:
        """后台那步：把没人再用的图从资产库里删掉（失败只返回 False）。"""

        key = str(media_id or "").strip()
        if not key or self._media_store is None:
            return False
        try:
            return bool(self._media_store.discard(key))
        except Exception:  # noqa: BLE001
            return False

    def purge(self, entry_id: Any) -> dict[str, Any]:
        """**彻底删除**一条收藏：记录、来源、变体、用法，连底层图片文件一起。

        管理页走的是 ``forget`` + 后台 ``discard_media``（用户 m21273 要的"不卡"）；
        这个方法保留"同步删干净"的老语义，给脚本与测试用。
        """

        result = self.forget(entry_id)
        if result.get("ok") and result.get("file_pending"):
            result["file_removed"] = self.discard_media(result.get("media_id"))
        if result.get("ok"):
            result["note"] = (
                "收藏和图片文件都删掉了。"
                if result.get("file_removed")
                else "收藏删掉了；图片文件还被别处引用（或正在使用），先留着。"
            )
        return result

    def cleanup_assets(self, *, extra_keep: Any = ()) -> int:
        """清掉没有任何收藏引用的资产元数据行（**不动文件**）。"""

        return self._db.cleanup_meme_assets(extra_keep=tuple(extra_keep or ()))

    # ---------------- 展示记录 ----------------

    def occurrence_id(self, session_id: Any, seq: Any, index: Any = 0) -> str:
        return occurrence_id_for(session_id, seq, index)

    def parse_occurrence(self, occurrence_id: Any) -> dict[str, Any]:
        return parse_occurrence_id(occurrence_id)

    def can_reappear(self, occurrence_id: Any) -> bool:
        """这次出现的所有阶段里**没有任何 ``delivered``** 时才允许再补附。"""

        for row in self._db.find_exposure(str(occurrence_id or "")):
            if str(row.get("state")) == "delivered":
                return False
        return True

    def claim_exposures(
        self, items: Any, *, phase_id: Any = PHASE_REPLY, now: Any = 0.0
    ) -> list[dict[str, Any]]:
        """请求构造时认领展示：``pending|failed`` 可认领，``claimed|delivered`` 不行。

        并发靠 ``image_exposures`` 的主键与事务：同一个 occurrence 只有一个请求
        能认领成功（另一个拿到 ``reserved`` / ``already_delivered``）。认领失败的
        条目**不进这次请求**，调用方按 ``ok`` 过滤。

        ``reserved`` 的那条如果已经卡了 ``CLAIM_STALE_SECONDS`` 以上，按**过期认领**
        处理：先 ``release`` 回 ``pending`` 再重认一次。不这么做的话，认领之后请求
        半路夭折（睡前门禁 / 被中断 / 限流提前返回）会把这**一次出现**永久钉死，
        那张图再也补附不上去。
        """

        phase = str(phase_id or PHASE_REPLY)
        claims: list[dict[str, Any]] = []
        for item in list(items or ()):
            occurrence = str(item.get("occurrence_id") or "")
            if not occurrence:
                continue
            media_id = str(item.get("media_id") or "")
            stamp = item.get("now", now)
            result = self._db.claim_exposure(
                occurrence_id=occurrence,
                phase_id=phase,
                session_id=str(item.get("session_id") or ""),
                media_id=media_id or None,
                now=stamp,
            )
            if not result.get("ok") and str(result.get("reason") or "") == "reserved":
                row = dict(result.get("row") or {})
                claimed_at = float(row.get("claimed_at") or 0.0)
                if claimed_at and self._clock() - claimed_at > CLAIM_STALE_SECONDS:
                    self._db.release_exposure(
                        occurrence_id=occurrence, phase_id=phase, now=stamp
                    )
                    result = self._db.claim_exposure(
                        occurrence_id=occurrence,
                        phase_id=phase,
                        session_id=str(item.get("session_id") or ""),
                        media_id=media_id or None,
                        now=stamp,
                    )
            claims.append(
                {
                    "occurrence_id": occurrence,
                    "phase_id": phase,
                    "media_id": media_id,
                    "url": str(item.get("url") or ""),
                    "ok": bool(result.get("ok")),
                    "state": str(result.get("state") or ""),
                    "reason": str(result.get("reason") or ""),
                }
            )
        return claims

    def finish_exposures(
        self, claims: Any, *, state: Any, now: Any = 0.0
    ) -> int:
        """请求有结果了：成功→``delivered``（这次出现就此封死），失败→``failed``。

        ``failed`` **不算展示**：下一轮同一 occurrence 可以重试（方案 §3.3）。
        返回真正改动的行数。
        """

        wanted = str(state or "")
        if wanted not in (MEME_EXPOSURE_DELIVERED, MEME_EXPOSURE_FAILED):
            raise ValueError(f"unknown exposure state: {wanted}")
        changed = 0
        for claim in list(claims or ()):
            if not claim.get("ok"):
                continue
            if self._db.mark_exposure(
                occurrence_id=str(claim.get("occurrence_id") or ""),
                phase_id=str(claim.get("phase_id") or PHASE_REPLY),
                state=wanted,
                now=now,
            ):
                changed += 1
        return changed

    # ---------------- 数据总览（管理页顶部） ----------------

    def summary(self, *, scope_id: Any = None) -> dict[str, Any]:
        """收藏的计数与配置快照（管理页顶部与"本轮数量/预算"说明用）。"""

        settings = self.settings
        rows = self._db.list_meme_entries(
            scope_id=scope_id, include_deleted=True, limit=1000
        )
        counts = {state: 0 for state in AVAILABILITY_STATES}
        for row in rows:
            key = str(row.get("entry_id") or "")
            media_id = str(row.get("media_id") or "").strip()
            present = self.file_present(media_id) if media_id else False
            state = meme_availability(row, file_present=present)
            counts[state] = counts.get(state, 0) + 1
        stored = [int(r or 0) for r in (r.get("asset_size_bytes") or 0 for r in rows)]
        total_bytes = sum(value for value in stored if value)
        quota = int(settings.storage_quota_bytes or 0)
        return {
            "count": len(rows),
            "counts": counts,
            "labels": dict(AVAILABILITY_LABELS),
            "used_bytes": total_bytes,
            "quota_bytes": quota,
            "quota_ratio": (float(total_bytes) / quota) if quota > 0 else 0.0,
            "settings": settings.values,
            "sources": dict(settings.sources),
            "budget": {
                "max_prompt_images": settings.max_prompt_images,
                "meme_image_max": settings.meme_image_max,
                "max_candidate_images_per_prompt": (
                    settings.max_candidate_images_per_prompt
                ),
                "max_search_rounds_per_reply": settings.max_search_rounds_per_reply,
                "max_send_per_reply": settings.max_send_per_reply,
                "send_meme_per_hour": settings.send_meme_per_hour,
                "max_send_per_day": settings.max_send_per_day,
                "repeat_cooldown": settings.repeat_cooldown,
                "prefetch_total_budget_ms": settings.prefetch_total_budget_ms,
                "exposure_keep_days": settings.exposure_keep_days,
            },
            "exposures": {
                "pending": self._db.count_exposures(state="pending"),
                "claimed": self._db.count_exposures(state="claimed"),
                "delivered": self._db.count_exposures(state="delivered"),
                "failed": self._db.count_exposures(state="failed"),
            },
        }

    # ---------------- 内部 ----------------

    def _record_state(self, entry: Mapping[str, Any]) -> str:
        """只看记录本身的状态（不看文件、不看范围）——回执里"已经收过了"用。"""

        status = str(entry.get("status") or "").strip()
        if status == MEME_ENTRY_STATUS_DELETED:
            return STATE_DELETED
        if status == MEME_ENTRY_STATUS_DISABLED:
            return STATE_DISABLED
        return STATE_ENABLED


def _state_reason(state: str) -> str:
    """五态各自给一句"为什么"（回执与页面共用）。"""

    return {
        STATE_ENABLED: "可用",
        STATE_DISABLED: "这条收藏被停用了",
        STATE_DELETED: "收藏记录已经删了，底层图片还在",
        STATE_MISSING: "底层图片文件不见了",
        SCOPE_UNAVAILABLE: "当前会话/来源范围用不了这条收藏",
    }.get(state, "")


# ``meme_XXX`` 编号的格式化在这里再导出一份，页面上直接用它显示
format_entry_id = format_meme_id
