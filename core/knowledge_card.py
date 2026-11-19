"""知识卡：她查过的梗 / 词 / 事，记下来，下次能拿出来用。

用户原话：「查了某个梗什么意思，查完就过去了，聊天记录刷掉她就不记得，也不会活用。」
所以这一层只做三件事：

1. **写**：一次搜索类工具**真拿到结果**时，把那段结果本身裁成一张卡
   （``summary`` 前 200 字、``excerpt`` 前 800 字）。**不调模型**——摘要是从结果正文里
   截出来的，天然与证据一致；省 token，也不会被模型改写成"看着像那么回事"的假话。
2. **查**：拿当前这句用户消息提关键词，按命中个数召回（``min_overlap`` 默认 2）。
3. **讲**：渲染成「# 你查过的事」那一段，措辞是"你自己记下的"，并明确写"用不上就别硬提"。

三条纪律：

* **summary 必须能在 excerpt 里逐字找到**（``accept_summary``，与 ``consolidate_jobs``
  里 ``_match_source`` 同款核对话术），否则整张卡不写，只记一条 rejected；
* **只认搜索类工具**（``web_search`` / ``deepseek_web_search`` / ``search_web``）：
  读正文（``web_fetch``）、查天气之类的工具不算"查过的事"；
* 这个模块**只做纯计算**：不碰库、不碰引擎、不发请求（写库在 ``core/db.py``，
  触发点在 ``core/engine.py``）。唯一的例外是文件末尾那节「编辑器接口」——
  ``list_cards`` / ``delete_cards`` 只拼 ``knowledge_card`` 一张表的 SQL，执行用的
  连接由调用方传进来，本模块自己不建连接、不读配置。

命名：``core/knowledge.py`` 已经是「能力经验」（这类事怎么做成的），与此处无关，
所以这一层单独一个文件叫 ``knowledge_card``。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .search import clean_passage, unwrap_payload_text

#: 算「查过的事」的工具名。只认这三种，不做子串匹配——
#: ``web_search_tavily`` 这类带后缀的按前缀认（见 :func:`is_search_tool`）。
SEARCH_TOOL_NAMES = ("web_search", "deepseek_web_search", "search_web")

DEFAULT_MAX_SUMMARY_CHARS = 200
DEFAULT_MAX_EXCERPT_CHARS = 800
MAX_KEYWORDS = 12
MIN_SUMMARY_CHARS = 8
"""摘要短于这么多字就没有信息量（"无"、"404"），不值得占一行提示词。"""

SENTENCE_MIN_CHARS = 40
"""按句末标点截断后至少要留下这么多字，否则退到硬截。

句子截断是为了别把结论截掉（用户担心的"断章取义"）；可要是第一个句号来得太早
（"好的。"），只留半句反而更糟，不如硬截并在结尾标一句"后略"。
"""

SUMMARY_TAIL = "…（后略）"
"""硬截时的结尾标记：让读到的她知道这句是被截断的，别当成完整结论去引用。"""

_SENTENCE_ENDS = "。！？；!?;\n"

VAGUE_MIN_CHARS = 4
"""``subject`` 归一化（去空白、去标点、小写）后短于这么多字，就当没问出具体的东西。"""

_VAGUE_FILLERS = (
    "这个",
    "那个",
    "这是",
    "那是",
    "什么叫",
    "是什么",
    "什么是",
    "什么意思",
    "啥意思",
    "干什么用",
    "怎么回事",
    "为什么",
    "怎么样",
    "怎么办",
    "怎么用",
    "咋回事",
    "然后",
    "这是啥",
    "什么",
    "啥",
    "怎么",
    "怎样",
    "咋",
    "意思",
    "梗",
    "东西",
    "玩意",
    "回事",
    "用处",
    "叫",
    "是",
    "的",
    "啊",
    "呀",
    "吧",
    "呢",
    "嘛",
    "吗",
    "这",
    "那",
    "它",
)
"""把这类疑问套话从 subject 里剥掉；剥完不剩东西就说明这句问的是"这是啥"。"""

NOTE_HEADER = "# 你查过的事"
NOTE_LEAD = (
    "（下面这些是你自己以前查过、顺手记下的，不是系统塞给你的资料。"
    "用得上的时候当自己的记性顺口提一句；用不上就别硬提。）"
)

_PARAM_KEYS = (
    "query",
    "q",
    "keyword",
    "keywords",
    "search_query",
    "searchQuery",
    "text",
    "input",
)

# 结构化结果里"标题 / 正文"的字段名（与 ``core/search.py`` 的口径一致）
_TITLE_KEYS = ("title", "name", "headline")
_BODY_KEYS = ("snippet", "content", "summary", "description", "text")
_LIST_KEYS = ("results", "items", "data", "list", "organic")

#: 整句都是这些字面量时不算标题（``parse_search_results`` 的兜底标题）
_FALLBACK_TITLES = frozenset({"未结构化的返回"})

_TAG_RE = re.compile(r"<[^>]{1,200}>")
_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#39|#34);", re.IGNORECASE)
_ENTITY_MAP = {
    "nbsp": " ",
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "#39": "'",
    "#34": '"',
}
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://[^\s，。；、）)\]\"'<>]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_JUNK_RE = re.compile(r"[\s\W_]+", re.UNICODE)

#: 疑问 / 功能词做成的二元组：几乎每句都有，靠它们匹配等于没匹配。
_STOP_BIGRAMS = frozenset(
    {
        "什么",
        "怎么",
        "为什",
        "么意",
        "意思",
        "是什",
        "一下",
        "这个",
        "那个",
        "哪个",
        "哪些",
        "是不",
        "不是",
        "有没",
        "没有",
        "时候",
        "知道",
        "告诉",
        "请问",
        "可以",
        "怎样",
        "如何",
        "为何",
        "一个",
        "我们",
        "你们",
        "他们",
        "她们",
        "真的",
        "就是",
        "还是",
        "但是",
        "因为",
        "所以",
        "如果",
        "现在",
        "今天",
        "明天",
        "昨天",
        "那个",
        "这是",
        "它的",
    }
)
_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "what",
        "why",
        "how",
        "http",
        "https",
        "www",
        "com",
        "html",
        "org",
        "net",
    }
)
#: 虚词：任何含这些字的二元组都不当关键词（"的意""了吗""么梗"）
_PARTICLES = frozenset("的了着过吗呢吧啊嘛哦呀哈么")


def is_search_tool(name: Any) -> bool:
    """这个工具算不算「查网页」（只认 :data:`SEARCH_TOOL_NAMES` 及其带后缀的变体）。"""

    text = str(name or "").strip().lower()
    if not text:
        return False
    for wanted in SEARCH_TOOL_NAMES:
        if text == wanted or text.startswith(wanted + "_") or text.endswith("_" + wanted):
            return True
    return False


def subject_from(params: Any, hint: Any = "") -> str:
    """这一轮查的是什么：优先取动作里那句 intent，其次取参数里的查询词。

    取不到就返回空串（``build_card`` 会退到结果标题，再不行就不写卡）。
    """

    text = _clean_inline(hint)
    if text:
        return _clip(text, 60)
    if isinstance(params, Mapping):
        for key in _PARAM_KEYS:
            value = params.get(key)
            if isinstance(value, (list, tuple)):
                value = " ".join(str(item or "") for item in value)
            found = _clean_inline(value)
            if found:
                return _clip(found, 60)
        for value in params.values():
            if isinstance(value, str):
                found = _clean_inline(value)
                if len(found) >= 2:
                    return _clip(found, 60)
    return ""


def subject_key(subject: Any) -> str:
    """``subject`` 归一化（去空白/标点、统一小写）后的 sha1 前 16 位（同一个人问同一件事只留一条）。"""

    text = _JUNK_RE.sub("", str(subject or "").lower())
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def is_vague_subject(subject: Any) -> bool:
    """这句 question 能不能当卡片的``subject``（"这是啥""什么意思"当不了）。

    两条判据：归一化后**短于** :data:`VAGUE_MIN_CHARS`，或者剥掉疑问套话之后什么都不剩。
    后者是为了挡住"这个梗什么意思""叫什么名字"这类——字不少，但一个具体的词都没有。
    """

    text = _JUNK_RE.sub("", str(subject or "").lower())
    if not text:
        return True
    if len(text) < VAGUE_MIN_CHARS:
        return True
    rest = text
    for filler in _VAGUE_FILLERS:
        rest = rest.replace(filler, "")
    # 剥完连两个字都不剩，说明这句除了套话什么都没有（"什么意思""这个梗咋回事"）；
    # 还剩两三个字的（"另一个梗"）仍算有指向，别误杀。
    return len(rest) < 2


def keywords_for(*texts: Any, limit: int = MAX_KEYWORDS) -> list[str]:
    """从这几段文字里提关键词：中文二元组（去停用词）+ 英文词，去重后最多 ``limit`` 个。

    先出现的先留下——调用方把"她自己问的那句 / 查的词"放前面，标题与正文放后面，
    这样上限截断时丢的是正文里的边角词。
    """

    budget = max(1, int(limit or MAX_KEYWORDS))
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        item = str(token or "").strip().lower()
        if len(item) < 2 or len(found) >= budget or item in seen:
            return
        if item in _STOP_BIGRAMS or item in _STOP_WORDS:
            return
        if any(char in _PARTICLES for char in item):
            return
        seen.add(item)
        found.append(item)

    for text in texts:
        raw = str(text or "")
        if not raw.strip():
            continue
        for run in _CJK_RE.findall(raw):
            for index in range(len(run) - 1):
                add(run[index : index + 2])
        for word in _WORD_RE.findall(raw):
            add(word)
        if len(found) >= budget:
            break
    return found


def readable_text(raw: Any) -> str:
    """把工具返回整理成一段能当摘要用的人话（去标签、去导航、压空白）。

    结构化返回（``{"results": [...]}``）按行取标题与正文，用「；」连起来——
    比把整段 JSON 当摘要有用得多，而且一个字都不是编的。
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    payload = _as_json(text)
    if payload is not None:
        rows = _payload_rows(payload)
        parts: list[str] = []
        for row in rows[:3]:
            if not isinstance(row, Mapping):
                continue
            title = _clean_inline(_first_value(row, _TITLE_KEYS))
            body = _squash(_clean_inline(_first_value(row, _BODY_KEYS)))
            if title and body:
                parts.append(f"{title}：{body}")
            elif body:
                parts.append(body)
            elif title:
                parts.append(title)
        if parts:
            return _squash("；".join(parts))
        unwrapped = _squash(unwrap_payload_text(text))
        if unwrapped:
            return unwrapped
    return _squash(clean_passage(text) or text)


def extract_source(raw: Any, *, url: Any = "", title: Any = "") -> tuple[str, str]:
    """从工具结果里认出来源链接与标题：结构化字段优先，其次正文里第一个链接。"""

    text = str(raw or "").strip()
    found_url = _clean_inline(url)
    found_title = _clean_inline(title)
    if found_title in _FALLBACK_TITLES:
        found_title = ""
    if text:
        payload = _as_json(text)
        row: Any = None
        if isinstance(payload, Mapping):
            row = payload
            rows = _payload_rows(payload)
            if rows:
                row = rows[0]
        elif isinstance(payload, list):
            rows = _payload_rows(payload)
            row = rows[0] if rows else None
        if isinstance(row, Mapping):
            if not found_url:
                found_url = _clean_inline(
                    row.get("url") or row.get("link") or row.get("href") or ""
                )
            if not found_title:
                found_title = _clean_inline(_first_value(row, _TITLE_KEYS))
        if not found_url:
            match = _URL_RE.search(text)
            if match:
                found_url = match.group(0).rstrip(".,;:!?)'\"》」】")
    if found_title in _FALLBACK_TITLES:
        found_title = ""
    return _clip(found_url, 500), _clip(found_title, 80)


def summarize(body: Any, limit: int = DEFAULT_MAX_SUMMARY_CHARS) -> str:
    """从正文里取一句摘要：**优先截到句末**，实在没有标点才硬截并标"后略"。

    硬截 200 字最容易出的事故是把结论截掉（结论通常在段末），她以后引用这张卡就会
    断章取义。所以先在 ``limit`` 以内找最后一个句末标点，截到那儿为止；但若那样
    只剩 :data:`SENTENCE_MIN_CHARS` 字不到（第一个句号来得太早），宁可硬截加尾巴。
    """

    text = _squash(body)
    try:
        cap = max(1, int(limit or DEFAULT_MAX_SUMMARY_CHARS))
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_SUMMARY_CHARS
    if len(text) <= cap:
        return text
    window = text[:cap]
    cut = max(window.rfind(char) for char in _SENTENCE_ENDS)
    if cut >= 0:
        sentence = window[: cut + 1].strip()
        if len(sentence) >= SENTENCE_MIN_CHARS:
            return sentence
    return window.rstrip() + SUMMARY_TAIL


def accept_summary(summary: Any, excerpt: Any) -> bool:
    """摘要能不能在原文里找到（``consolidate_jobs._match_source`` 同款核对话术）。

    逐字出现，或者（较长时）开头 12 个字出现——后者容忍"被截断在句子中间"的那种。
    """

    quote = _squash(summary)
    body = _squash(excerpt)
    if quote.endswith(SUMMARY_TAIL):
        # 「…（后略）」是我们加的标记，不是原文的一部分：核对的应当是它前面那截
        quote = quote[: -len(SUMMARY_TAIL)].strip()
    if not quote or not body:
        return False
    if quote in body:
        return True
    return len(quote) >= 4 and quote[:12] in body


def build_card(
    raw: Any,
    *,
    subject: Any = "",
    title: Any = "",
    url: Any = "",
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    max_age_days: float = 90.0,
    now: float = 0.0,
) -> tuple[dict[str, Any] | None, str]:
    """把一次真实结果裁成一张卡；返回 ``(卡片草案, 拒收原因)``。

    草案为 ``None`` 时原因非空（``empty`` / ``too_short`` / ``evidence_mismatch`` /
    ``no_subject`` / ``vague_subject``），调用方据此记一条 ``knowledge`` 的
    rejected（``vague_subject`` 记 skipped），不写表。

    ``subject`` 这句问句太泛（"这是啥""什么意思"）时不拿它当标题——那种卡片以后
    既检索不出也说不清——改用结果标题；结果标题也没有就不建这张卡。
    """

    body = readable_text(raw)
    if not body:
        return None, "empty"
    excerpt = body[: max(1, int(max_excerpt_chars or DEFAULT_MAX_EXCERPT_CHARS))]
    summary = summarize(body, int(max_summary_chars or DEFAULT_MAX_SUMMARY_CHARS))
    if len(summary) < MIN_SUMMARY_CHARS:
        return None, "too_short"
    if not accept_summary(summary, excerpt):
        # 走到这儿说明摘要不是从这段正文里原样截的（比如两个上限配反了）：
        # 宁可不写，也不留一条跟证据对不上的"知识"
        return None, "evidence_mismatch"
    source_url, source_title = extract_source(raw, url=url, title=title)
    asked = _clean_inline(subject)
    label = "" if is_vague_subject(asked) else asked
    if not label:
        label = source_title
    if not label:
        # 问句太泛、又没有标题可顶：这张卡以后既检索不出也说不清，不建
        return None, "vague_subject" if asked else "no_subject"
    if is_vague_subject(label):
        return None, "vague_subject"
    stamp = float(now or 0.0) or time.time()
    days = float(max_age_days or 0.0)
    expires_at = stamp + days * 86400.0 if days > 0 else 0.0
    return (
        {
            "subject": _clip(label, 60),
            "subject_key": subject_key(label),
            "summary": summary,
            "keywords": keywords_for(label, source_title, body[:120]),
            "source_url": source_url,
            "source_title": source_title,
            "excerpt": excerpt,
            "expires_at": expires_at,
        },
        "",
    )


def render_section(
    cards: Iterable[Mapping[str, Any]],
    *,
    now: float = 0.0,
    max_cards: int = 3,
) -> str:
    """渲染「# 你查过的事」那一段；一条都渲染不出来就返回空串（整段消失）。"""

    try:
        limit = max(1, int(max_cards or 3))
    except (TypeError, ValueError):
        limit = 3
    lines: list[str] = []
    for card in list(cards or [])[:limit]:
        if not isinstance(card, Mapping):
            continue
        subject = _clean_inline(card.get("subject"))
        summary = _squash(card.get("summary"))
        if not subject or not summary:
            continue
        when = _month_day(card.get("updated_at") or card.get("created_at") or 0.0)
        source = (
            _clean_inline(card.get("source_title"))
            or _host_of(card.get("source_url"))
            or "查到的网页"
        )
        tail = f"（来源：{source}" + (f"，{when}" if when else "") + "）"
        lines.append(f"- {subject}：{summary}{tail}")
    if not lines:
        return ""
    return "\n".join([NOTE_HEADER, NOTE_LEAD, *lines])


def host_of(url: Any) -> str:
    """链接的主机名（注入提示词时比整条 URL 省地方）。"""

    return _host_of(url)


# ---------------------------------------------------------------------------
# 小工具（纯函数）
# ---------------------------------------------------------------------------


def _as_json(text: str) -> Any:
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _payload_rows(payload: Any) -> list[Any]:
    """结构化返回里的结果列表：直接是列表，或者藏在 ``results`` 这类键下面。"""

    if isinstance(payload, list):
        return [row for row in payload]
    if isinstance(payload, Mapping):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
    return []


def _first_value(row: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            value = " ".join(str(item or "") for item in value)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _month_day(stamp: Any) -> str:
    try:
        value = float(stamp or 0.0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        moment = time.localtime(value)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{moment.tm_mon}-{moment.tm_mday:02d}"


def _host_of(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text if "://" in text else "http://" + text).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host[:60]


def _clean_inline(value: Any) -> str:
    """去标签、还原实体、压成一行，并去掉首尾的引号/括号。"""

    text = str(value or "")
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(lambda match: _ENTITY_MAP.get(match.group(1).lower(), " "), text)
    text = _WS_RE.sub(" ", text).strip()
    return text.strip(" \"'“”‘’「」【】()（）[]{}<>\t")


def _squash(value: Any) -> str:
    """核对证据用的归一化：与 :func:`_clean_inline` 同口径，但不吃首尾括号。"""

    text = str(value or "")
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(lambda match: _ENTITY_MAP.get(match.group(1).lower(), " "), text)
    return _WS_RE.sub(" ", text).strip()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


# ---------------------------------------------------------------------------
# 编辑器接口：分页查询 / 删除（只碰 ``knowledge_card`` 这一张表）
# ---------------------------------------------------------------------------
#
# 为什么 SQL 在这里而不是 ``core/db.py``：db.py 的 ``list_knowledge_cards`` 是"按会话
# 列全表"，没有分页也没有关键字过滤，而它的签名已经有人在用（不许动）。编辑器要的是
# "分页 + 搜索"，属于展示层需求，所以在这里拼语句，**执行**交给调用方传进来的 store
# （运行时是 ``Database``，测试里也可以直接给 sqlite3 连接）。
#
# 两条纪律：
# * ``list_cards`` 只读（``SELECT``），GET 请求里不许产生任何写；
# * ``delete_cards`` 按 id 删，不存在的 id 跳过（幂等），绝不删表 / 清空。

CARD_COLUMNS = (
    "id",
    "session_id",
    "subject",
    "subject_key",
    "summary",
    "keywords",
    "source_url",
    "source_title",
    "excerpt",
    "confidence",
    "created_at",
    "updated_at",
    "last_used_at",
    "use_count",
    "expires_at",
    "status",
)
"""与 ``core/db.py`` 的 ``knowledge_card`` 表一一对应（顺序即返回顺序）。"""

CARD_LIST_MAX_LIMIT = 500
CARD_DELETE_CHUNK = 400
"""一条 ``DELETE`` 最多带多少个 id（SQLite 变量上限远大于此，分块只是为了稳）。"""


def _fetch(store: Any, sql: str, params: Iterable[Any]) -> list[Any]:
    """只读执行：``Database`` 走 ``_query``，别的连接对象走 ``execute``。"""

    query = getattr(store, "_query", None)
    if callable(query):
        return list(query(sql, tuple(params)))
    cursor = store.execute(sql, tuple(params))
    return list(cursor.fetchall())


def _write(store: Any, sql: str, params: Iterable[Any]) -> int:
    """写执行：``Database._execute`` 自带 commit；返回影响行数（拿不到就是 0）。"""

    execute = getattr(store, "_execute", None) or getattr(store, "execute", None)
    if not callable(execute):
        return 0
    cursor = execute(sql, tuple(params))
    try:
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _like_pattern(text: str) -> str:
    """``q`` → ``LIKE`` 模式：把用户输入里的 ``%`` / ``_`` 转义掉（配 ``ESCAPE '\\'``）。"""

    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(*, q: str = "", session_id: str = "", include_expired: bool = False) -> tuple[str, list[Any]]:
    """拼 ``WHERE``（过滤器：状态 / 会话 / 关键字）。"""

    clauses: list[str] = []
    params: list[Any] = []
    if not include_expired:
        clauses.append("status = 'active'")
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id))
    text = str(q or "").strip()
    if text:
        clauses.append("(subject LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\')")
        params.extend([_like_pattern(text)] * 3)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _card_view(row: Any) -> dict[str, Any]:
    """一行 → 给前端的 dict（``keywords`` 解成 list、数值统一成 int/float）。"""

    try:
        data: dict[str, Any] = dict(row)
    except (TypeError, ValueError):
        data = {}
    keywords = data.get("keywords")
    if isinstance(keywords, str):
        parsed = _as_json(keywords)
        data["keywords"] = [str(item) for item in parsed] if isinstance(parsed, list) else []
    elif isinstance(keywords, (list, tuple)):
        data["keywords"] = [str(item) for item in keywords]
    else:
        data["keywords"] = []
    for name in ("confidence", "created_at", "updated_at", "last_used_at", "expires_at"):
        try:
            data[name] = float(data.get(name) or 0.0)
        except (TypeError, ValueError):
            data[name] = 0.0
    for name in ("id", "use_count"):
        try:
            data[name] = int(data.get(name) or 0)
        except (TypeError, ValueError):
            data[name] = 0
    # 固定字段与顺序：前端不用猜这一行有没有某个键。
    return {name: data.get(name) for name in CARD_COLUMNS}


def list_cards(
    store: Any,
    *,
    q: str = "",
    session_id: str = "",
    limit: int = 200,
    offset: int = 0,
    include_expired: bool = False,
) -> dict[str, Any]:
    """分页 + 关键字查询，返回 ``{"total": int, "items": [dict, ...]}``。

    ``total`` 是**同一套过滤条件**下的总数（不是本页条数），前端据此翻页。
    ``q`` 同时对 ``subject`` / ``summary`` / ``keywords`` 做 ``LIKE``（SQLite 默认
    对 ASCII 不分大小写，中文就是子串匹配）。
    """

    try:
        page = int(limit)
    except (TypeError, ValueError):
        page = 200
    page = max(1, min(CARD_LIST_MAX_LIMIT, page))
    try:
        skip = int(offset)
    except (TypeError, ValueError):
        skip = 0
    skip = max(0, skip)
    clause, params = _where(q=q, session_id=session_id, include_expired=include_expired)
    total = 0
    rows = _fetch(store, f"SELECT COUNT(*) FROM knowledge_card{clause}", params)
    if rows:
        try:
            total = int(rows[0][0] or 0)
        except (TypeError, ValueError, IndexError):
            total = 0
    items = [
        _card_view(row)
        for row in _fetch(
            store,
            f"SELECT {', '.join(CARD_COLUMNS)} FROM knowledge_card{clause}"
            " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, page, skip],
        )
    ]
    return {"total": max(0, total), "items": items}


def delete_cards(store: Any, ids: Iterable[Any]) -> int:
    """按 id 删卡，返回真的删掉了几行（不存在 / 已经删掉的 id 自动跳过 → 幂等）。

    只删 ``knowledge_card`` 里的指定行：不删表、不清空、不按条件批量删。传进来的
    不是正整数（``"abc"`` / ``None`` / ``-1``）一律忽略。
    """

    wanted: list[int] = []
    seen: set[int] = set()
    for value in ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            wanted.append(number)
    deleted = 0
    for start in range(0, len(wanted), CARD_DELETE_CHUNK):
        chunk = wanted[start : start + CARD_DELETE_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        deleted += _write(store, f"DELETE FROM knowledge_card WHERE id IN ({marks})", chunk)
    return deleted
