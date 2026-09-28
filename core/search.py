"""联网检索的结果处理：把搜索/抓取工具返回的文本整理成「证据」。

搜索工具各写各的：有的返回 JSON 列表，有的返回带链接的一行行文本，有的干脆是
一大段散文。这里统一整理成 ``Evidence``，去重、限量，再渲染成给模型看的证据块——
她能对着编号讲，日志里也能看出"这句话是照哪条说的"。

整理不出来时**不丢内容**：整段原文折成一条证据，标注"未结构化"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 一条证据默认最多留多少字（正文片段比摘要长一些）
SNIPPET_CHARS = 200
PASSAGE_CHARS = 1200

# 单独一段路径就到底的，基本都是栏目入口
HOMEPAGE_SEGMENTS = (
    "home",
    "index",
    "index.html",
    "zh",
    "cn",
    "en",
    "zhongwen",
    "simp",
    "news",
    "hot",
    "top",
)
# 两段路径、且就是栏目首页的
HOMEPAGE_PATHS = ("/zhongwen/simp", "/cn/index", "/zh/index")

# "- **URL**: https://…" 这种只说明了字段名、没有内容
_LABEL_ONLY = re.compile(
    r"^[\s\-\*#>·|:：]*(?:url|link|链接|网址|地址|来源|source|出处|title|标题|时间|date|"
    r"摘要|描述|正文|内容|snippet|summary|description)[\s\-\*:：]*$",
    re.IGNORECASE,
)
# "### 1. 标题" / "1. 标题" / "**标题**" 这类标题行
_TITLE_LINE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*\d{0,2}\s*[\.、\)]?\s*")
# 整行就是一句"没结果"的工具回话
_FAILED_MARKERS = (
    "extract_failed",
    "unable to extract",
    "failed to extract",
    "no content",
    "空内容",
)

_URL_RE = re.compile(r"https?://[^\s，。；、）)\]\"']+")

# 文本/聊天类站点：搜到了也只是登录墙或社交主页，抓不到正文
PORTAL_HOSTS = (
    "facebook.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "weibo.com",
    "zhihu.com/people",
    "tieba.baidu.com/f",
)

# 阅读工具返回 JSON 外壳时，正文挂在这些字段上（按优先级）
_PAYLOAD_TEXT_KEYS = (
    "content",
    "text",
    "markdown",
    "body",
    "description",
    "summary",
    "snippet",
)
_PAYLOAD_FIELD_RE = re.compile(
    # 结尾的引号可有可无：工具返回被截断时正文还是能抠出来的
    r'"(?:content|text|markdown|body|description|summary|snippet)"\s*:\s*'
    r'"((?:[^"\\]|\\.)*)"?'
)
# 整行只有链接（markdown 的 `[](https://…)` / `[标题](https://…)`）：正文里的导航条
_LINK_ONLY_RE = re.compile(r"\[[^\]]*\]\(\s*(?:https?://)?[^)]*\)")


@dataclass
class Evidence:
    """一条检索证据。"""

    title: str = ""
    url: str = ""
    snippet: str = ""
    published_at: str = ""
    source: str = ""
    """来自哪条查询（多查询时用得上）。"""

    passage: str = ""
    """阅读工具抓回来的正文片段（可选）。"""

    def render(self, index: int) -> str:
        """渲染成提示词里的一行（可以多行：带正文时正文另起一行）。"""

        head = f"{index}. "
        if self.title:
            head += f"[{self.title}] "
        if self.published_at:
            head += f"（{self.published_at}）"
        # 正文是导航页 / 抓取失败时退回摘要——别让"读到一半"把好材料盖掉
        body = evidence_text(self) or (self.passage or self.snippet or "").strip()
        head += body
        if self.url:
            head += f"\n   来源：{self.url}"
        return head.strip()


def parse_search_results(raw: str, *, source: str = "") -> list[Evidence]:
    """把一段工具返回整理成证据列表（尽力而为，最后一定至少有内容）。"""

    text = str(raw or "").strip()
    if not text:
        return []
    items = _parse_json_payload(text, source=source)
    if not items:
        items = _parse_lines(text, source=source)
    if not items:
        items = [
            Evidence(snippet=_clip(text, 600), source=source, title="未结构化的返回")
        ]
    return merge_evidence(items)


def _parse_json_payload(text: str, *, source: str) -> list[Evidence]:
    """工具直接返回 JSON 的情况：``[{title, url, snippet, ...}, ...]`` 或带列表的字典。"""

    if not text.lstrip().startswith(("{", "[")):
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("results", "items", "data", "list", "organic"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    items: list[Evidence] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(
            row.get("title") or row.get("name") or row.get("headline") or ""
        ).strip()
        url = str(row.get("url") or row.get("link") or row.get("href") or "").strip()
        snippet = str(
            row.get("snippet")
            or row.get("content")
            or row.get("summary")
            or row.get("description")
            or row.get("text")
            or ""
        ).strip()
        if not (title or url or snippet):
            continue
        items.append(
            Evidence(
                title=_clip(title, 80),
                url=url,
                snippet=_clip(snippet, SNIPPET_CHARS),
                published_at=str(
                    row.get("published_at") or row.get("date") or row.get("time") or ""
                ).strip()[:24],
                source=source,
            )
        )
    return items


def _parse_lines(text: str, *, source: str) -> list[Evidence]:
    """一行行文本的情况：按"标题行 + URL 行 + 摘要行"这种排版拆成一条条证据。

    搜索工具最常见的输出长这样::

        ## Search Results (5 results, 869ms)
        ### 1. Google 新闻
        - **URL**: https://news.google.com/home
        - 中国伊朗利用开源AI展开大规模影响力行动 · 《纽约时报》：…

    **真正的摘要写在 URL 的下一行**（有时前面还带 `- 描述：` 这种字段名）。
    以前只拿"URL 那一行剩下的字"当摘要，于是每条证据的正文都是 `**URL**` 三个字母——
    搜索明明带回了内容，压出来的摘要里却什么都没有。
    """

    lines = [
        " ".join(raw_line.split())
        for raw_line in str(text).splitlines()
        if raw_line.strip()
    ]
    lines = [line for line in lines if not _is_result_header(line)]
    items: list[Evidence] = []
    current: Evidence | None = None
    pending: list[str] = []
    want_snippet = False  # 上一行是 URL 行：下一行就是摘要

    def flush() -> None:
        nonlocal current, pending, want_snippet
        if current is not None:
            body = " ".join(part for part in pending if part).strip()
            if body and not _LABEL_ONLY.match(body):
                current.snippet = _clip(
                    f"{current.snippet} {body}".strip(), SNIPPET_CHARS
                )
            if current.url or current.title or current.snippet:
                items.append(current)
        current, pending, want_snippet = None, [], False

    def append_snippet(body: str) -> None:
        nonlocal want_snippet
        if current is None:
            return
        current.snippet = _clip(f"{current.snippet} {body}".strip(), SNIPPET_CHARS)
        want_snippet = False

    for index, stripped in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        match = _URL_RE.search(stripped)
        if match:
            url = match.group(0)
            rest = _strip_label(stripped.replace(url, " "))
            if current is None or current.url:
                title = _clean_title(pending[-1]) if pending else ""
                flush()
                current = Evidence(title=title, source=source)
            current.url = url
            if rest and not _LABEL_ONLY.match(rest):
                append_snippet(rest)
            else:
                want_snippet = True
            pending = []
            continue
        if _looks_like_title_line(stripped) and (
            current is None or current.url or current.title
        ):
            flush()
            pending = [stripped]
            continue
        if _URL_RE.search(next_line) and len(stripped) <= 60:
            # "百度热搜" + 下一行是链接 → 这一行是**下一条的标题**
            # （搜索工具那种"标题一行、链接一行"的排版）
            flush()
            pending = [stripped]
            continue
        body = _strip_label(stripped)
        if not body or _LABEL_ONLY.match(body):
            continue
        if want_snippet or (current is not None and current.url):
            append_snippet(body)
            continue
        pending.append(stripped)
    flush()
    if not items and pending:
        # 一段没有任何链接的文本：整段当一条证据（宁可留原文，也别丢）
        body = " ".join(pending).strip()
        if body:
            items.append(
                Evidence(
                    title=_clean_title(pending[0]),
                    snippet=_clip(body, 600),
                    source=source,
                )
            )
    return items


def _looks_like_title_line(line: str) -> bool:
    """``### 标题`` / ``**标题**`` 这类行：是新一条的开头，不是摘要。"""

    text = str(line or "").lstrip()
    if re.match(r"^#{1,6}\s+\S", text):
        return True
    return bool(re.match(r"^\*\*[^*]{2,}\*\*\s*$", text))


def _is_result_header(line: str) -> bool:
    """``## Search Results (5 results, 869ms)`` 这类纯头部行：不是内容。"""

    text = line.lstrip("#= ").strip()
    return bool(
        re.match(r"^search\s+results?\b", text, re.IGNORECASE)
        or re.match(r"^共?\s*\d+\s*条结果", text)
    )


def _strip_label(line: str) -> str:
    """去掉 ``- **描述**: 内容`` 里的字段名与修饰符，留下内容。"""

    text = re.sub(r"^[\s\-\*#>·|]+", "", str(line or ""))
    text = re.sub(
        r"^(?:url|link|链接|网址|地址|来源|source|出处|title|标题|时间|date|"
        r"摘要|描述|正文|内容|snippet|summary|description)\s*[:：]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip(" \t-—·|:：*#")


def _clean_title(line: str) -> str:
    """标题行去掉 ``### 1.`` 这类编号与 Markdown 修饰。"""

    text = _strip_label(line)
    text = re.sub(r"^\d{1,3}\s*[\.、\)]\s*", "", text)
    return _clip(text, 80)


def merge_evidence(items: list[Evidence], *, limit: int = 8) -> list[Evidence]:
    """去重（同一篇链接、标题很像的算一条）并限量。"""

    merged: list[Evidence] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in items or []:
        url_key = _url_key(item.url)
        title_key = _title_key(item.title)
        if url_key and url_key in seen_urls:
            continue
        if not url_key and title_key and title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        merged.append(item)
        if len(merged) >= max(1, int(limit)):
            break
    return merged


def render_evidence(items: list[Evidence], *, limit: int = 8, chars: int = 1600) -> str:
    """渲染成给模型看的证据块。"""

    picked = list(items or [])[: max(1, int(limit))]
    if not picked:
        return ""
    lines = [item.render(index) for index, item in enumerate(picked, start=1)]
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= chars else text[:chars] + "…"


def sources_of(items: list[Evidence], *, limit: int = 8) -> list[str]:
    """证据里出现过的链接（日志与调试输出用）。"""

    return [item.url for item in (items or []) if item.url][: max(1, int(limit))]


def looks_like_homepage(url: str) -> bool:
    """这个链接像不像"首页 / 栏目入口"——搜索经常先给你一堆这种，读了也没有正文。

    ``https://news.google.com/home``、``https://www.bbc.com/zhongwen/simp``、
    ``https://news.cctv.com/tech/index.shtml`` 这类都是：路径是栏目而不是某一篇。
    """

    text = str(url or "").strip().lower()
    if not text:
        return False
    if any(host in text for host in PORTAL_HOSTS):
        # 社交 / 问答站：要么登录墙，要么只是一条帖子的壳子，读了也没正文
        return True
    body = text.split("://", 1)[-1]
    path = "/" + body.split("/", 1)[1] if "/" in body else ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [item for item in path.split("/") if item]
    if not segments:
        return True  # 只有域名：就是首页
    if len(segments) == 1:
        return segments[0] in HOMEPAGE_SEGMENTS
    if f"/{segments[0]}/{segments[1]}" in HOMEPAGE_PATHS:
        return True
    # 目录页的特征：以 index / list / 栏目名结尾、或者路径里带 index.shtml 这类
    tail = segments[-1]
    if tail in HOMEPAGE_SEGMENTS or tail.startswith("index."):
        return True
    return tail in ("list", "channel", "category", "topics", "tag", "search")


def looks_like_nav(text: str) -> bool:
    """这段"正文"是不是其实就是导航 / 目录（一堆链接，没有句子）。

    读正文经常返回栏目页：内容是一串 ``[新闻](https://…) [国内](https://…)``，
    把它当材料喂给压缩模型，它只能得出"这里只有栏目介绍"这种空话。
    """

    body = str(text or "")
    if not body.strip():
        return True
    links = body.count("](http") + body.count("]（http")
    sentences = sum(body.count(ch) for ch in "。！？!?")
    if len(body) < 120:
        return links >= 2 and sentences == 0
    if links >= 8 and sentences <= 2:
        return True
    if links >= 4 and sentences == 0:
        return True
    return False


def failed_text(text: str) -> bool:
    """工具"成功了"但内容是失败说明（``extract_failed`` 这种）。"""

    body = str(text or "").strip().lower()
    if not body:
        return True
    if len(body) > 200:
        return False
    return any(marker in body for marker in _FAILED_MARKERS)


def evidence_text(item: Evidence) -> str:
    """这条证据真正能用的内容：正文（像正文时）优先，否则用搜索摘要。

    读到导航页 / 抓取失败时**不要把搜索摘要一起丢掉**——摘要里往往就有答案。
    """

    passage = str(item.passage or "").strip()
    snippet = str(item.snippet or "").strip()
    if passage and not looks_like_nav(passage) and not failed_text(passage):
        return passage
    if snippet and not failed_text(snippet):
        return snippet
    return ""


def unwrap_payload_text(text: str) -> str:
    """阅读工具返回 JSON 外壳时，取出里面真正的正文。

    ``anysearch_extract`` 这类工具返回 ``{"url": …, "title": …, "content": …}``：
    把整段 JSON 当正文交下去，等于让压缩模型去读转义过的 ``\\n`` 和字段名——
    材料里于是出现 ``{"url":"https://…`` 这种开头，模型还得自己猜哪个字段是正文。
    """

    raw = str(text or "").strip()
    if not raw or not raw.startswith(("{", "[")):
        return raw
    try:
        data: Any = json.loads(raw)
    except Exception:
        data = None
    body = _payload_body(data)
    if body.strip():
        return body.strip()
    # JSON 被截断、或者字段名不认识：按字段名把正文抠出来（并把 ``\n`` 还原）
    match = _PAYLOAD_FIELD_RE.search(raw)
    if match:
        try:
            value = str(json.loads(f'"{match.group(1)}"'))
        except Exception:
            # 截断在转义符中间：还原不了就只把最常见的 ``\n`` 换回真换行
            value = match.group(1).replace("\\n", "\n")
        if value.strip():
            return value.strip()
    return raw


def _payload_body(data: Any) -> str:
    """从解析好的 JSON 里找出正文（认不出就返回空串）。"""

    if isinstance(data, dict):
        for key in _PAYLOAD_TEXT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                if value.lstrip().startswith(("{", "[")):
                    return unwrap_payload_text(value)
                return value
        for value in data.values():
            if isinstance(value, (dict, list)):
                inner = _payload_body(value)
                if inner.strip():
                    return inner
        return ""
    if isinstance(data, list):
        parts = [
            part for part in (_payload_body(entry) for entry in data) if part.strip()
        ]
        return "\n".join(parts)
    return ""


def clean_passage(text: str) -> str:
    """去掉正文里的导航条：整行只有链接的那些行，对模型没有任何信息量。

    网页抓回来的正文常常以一串 ``[](https://…/main.html?nav=1)`` 开头，
    除了占字数，只会让压缩模型以为"这一篇全是入口"。
    """

    raw = str(text or "")
    if not raw.strip():
        return ""
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{"):
            # 空行、以及还带着 JSON 外壳的行：都不是正文
            continue
        without_links = _LINK_ONLY_RE.sub("", stripped).strip(" \t-—·|:：*#")
        if not without_links:
            continue
        kept.append(_one_space(without_links))
    if not kept:
        # 整篇都是链接 / 外壳：不如把原文留下来，让"这像不像导航页"的判断去决定去留
        return _one_space(raw)
    return "\n".join(kept)


def query_key(text: str) -> str:
    """查询词的归一化指纹：只留中英文数字，用来判断"这句是不是问过"。"""

    return "".join(char for char in str(text or "").lower() if char.isalnum())


def queries_too_similar(left: str, right: str) -> bool:
    """两条查询词问的是不是同一件事（用来掐掉重复的补查）。

    「异环 1.4版本 祷歌为谁而诵 更新公告」和「…更新内容」这种：字面不同，问的是同一件事；
    而「异环 1.4 新角色」和「异环 1.4 新场景」共享一个前缀、问的却是两件事，不能被误伤。
    所以比的是"短的那条有多少被包含"（覆盖度），不是对称的相似度。
    """

    a = query_key(left)
    b = query_key(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    first = {a[index : index + 2] for index in range(len(a) - 1)}
    second = {b[index : index + 2] for index in range(len(b) - 1)}
    if not first or not second:
        return False
    return len(first & second) / min(len(first), len(second)) >= 0.72


def _one_space(text: str) -> str:
    """把一行里的连续空白压成一个空格。"""

    return " ".join(str(text or "").split())


def _url_key(url: str) -> str:
    text = str(url or "").strip().lower()
    if not text:
        return ""
    # 去掉 query（临时 token 每次都变）和结尾斜杠
    base = text.split("?", 1)[0].split("#", 1)[0]
    return base.rstrip("/")


def _title_key(title: str) -> str:
    return "".join(ch for ch in str(title or "").lower() if ch.isalnum())


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"
