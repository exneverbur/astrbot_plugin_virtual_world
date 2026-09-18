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

_URL_RE = re.compile(r"https?://[^\s，。；、）)\]\"']+")


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
        body = self.passage or self.snippet
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
    """一行行文本的情况：带链接的行当一条证据，紧挨着的上一行当标题。"""

    items: list[Evidence] = []
    previous = ""
    for line in str(text).splitlines():
        stripped = " ".join(line.split())
        if not stripped:
            continue
        match = _URL_RE.search(stripped)
        if not match:
            previous = stripped
            continue
        url = match.group(0)
        body = stripped.replace(url, " ").strip(" -—·|:：")
        # 紧挨着的上一行通常就是标题（"百度热搜" + 链接 + 摘要这种排版）
        title = previous or body
        items.append(
            Evidence(
                title=_clip(title, 80),
                url=url,
                snippet=_clip(body or previous, SNIPPET_CHARS),
                source=source,
            )
        )
        previous = ""
    return items


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
