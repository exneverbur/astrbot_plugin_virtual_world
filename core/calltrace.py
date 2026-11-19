"""模型调用的观测账：**这一次到底问了谁、问了什么、哪几块没进去、为什么没进去**。

为什么单独一个模块（而不是继续往引擎里塞字段）：调试页要的是"三类请求分得清"，
写回复的、自己决定的、Agent 出去办事的是三种东西，混在一份快照里既看不清也没法解释
（复核方案 §10.11 / DBG01–DBG03）。

几条硬规矩，写在这里是为了以后别退化：

1. **只读、只在内存里**：最多留 ``KEEP`` 条，超了丢最旧的。不落库、不发消息、不动额度。
2. **脱敏**：像 ``api_key=…`` / ``Authorization: …`` 这种行整行抹掉——调试页是给用户看的，
   不能因为排查问题把凭据抄到屏幕上。
3. **截断要标出来**：``system_truncated`` / ``user_truncated`` 为真时页面必须显示，
   否则"看到的就是全部"会骗人。
4. **区分构造与实际**：``source="constructed"`` 是我们拼出来的请求；
   ``source="provider"`` 是 Provider 边界上真正发出去的那一份。两者不许互相冒充。
5. **缺席要给理由**：``excluded`` 里每一条都带中文原因（关着 / 没工具 / 预算不够 /
   没图片 / 没候选 / 权限受限），不允许只写"没有"。
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Iterable, Mapping

#: 内存里最多留多少条（调试页一次也就看最近几条）
KEEP = 24

#: 单条里 system / user 各自最多留多少字符
SYSTEM_CHARS = 8000
USER_CHARS = 4000

#: 调用类型：写回复 / 自己决定 / Agent 出去办事 / 续说 / 辅助补参数 / 内容生成
CALL_KINDS = (
    "vw_reply",
    "vw_autonomous",
    "agent_plan",
    "continuation",
    "helper",
    "creator",
)

CALL_KIND_LABELS = {
    "vw_reply": "接管回复",
    "vw_autonomous": "自主决策",
    "agent_plan": "Agent 规划",
    "continuation": "续说",
    "helper": "辅助模型（补参数 / 起标题）",
    "creator": "内容生成（编事件包 / 生成动作地点）",
}

#: 看得出来是凭据的行：整行抹掉（宁可少一条日志，也不把 Key 抄到屏幕上）
_SECRET_LINE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|authorization|bearer)\b",
    re.IGNORECASE,
)


def _clip(text: Any, limit: int) -> tuple[str, bool]:
    """截断一段文本，返回 ``(文本, 是否被截断)``。"""

    body = str(text or "")
    if len(body) <= limit:
        return body, False
    return body[:limit], True


def redact(text: Any) -> str:
    """把看起来像凭据的行抹掉（其余原样保留，便于对照提示词）。"""

    lines = []
    for line in str(text or "").splitlines():
        if _SECRET_LINE.search(line):
            lines.append("［已隐去一行：疑似凭据］")
        else:
            lines.append(line)
    return "\n".join(lines)


class CallTrace:
    """一次进程内的调用账（环形、有界、可脱敏）。"""

    def __init__(self, keep: int = KEEP) -> None:
        self._rows: deque[dict[str, Any]] = deque(maxlen=max(1, int(keep)))
        self._seq = 0

    # ---------------- 写 ----------------

    def record(
        self,
        *,
        kind: str,
        stage: str = "",
        session_id: str = "",
        persona_id: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        contexts: Iterable[Any] | None = None,
        image_urls: Iterable[str] | None = None,
        provider: str = "",
        model: str = "",
        provider_source: str = "",
        config_revision: int | str = 0,
        included: Iterable[str] | None = None,
        excluded: Iterable[tuple[str, str]] | None = None,
        source: str = "constructed",
        ok: bool | None = None,
        error: str = "",
        reply_chars: int = 0,
        at: float = 0.0,
    ) -> dict[str, Any]:
        """记一条。任何字段都可以缺省——缺了就是"不知道"，页面照实写，不许补猜。"""

        self._seq += 1
        system_text, system_clipped = _clip(redact(system_prompt), SYSTEM_CHARS)
        user_text, user_clipped = _clip(redact(user_prompt), USER_CHARS)
        row = {
            "request_id": f"call-{self._seq}",
            "kind": str(kind or "vw_reply"),
            "kind_label": CALL_KIND_LABELS.get(str(kind or ""), str(kind or "")),
            "stage": str(stage or ""),
            "session_id": str(session_id or ""),
            "persona_id": str(persona_id or ""),
            "at": float(at or 0.0),
            "config_revision": config_revision,
            "provider": str(provider or ""),
            # `main` = 跟主模型一样；`agent` = 用户给 Agent 单独配的；`fallback_main` = 没配、跟着主模型
            "provider_source": str(provider_source or ""),
            "model": str(model or ""),
            "source": str(source or "constructed"),
            "system_prompt": system_text,
            "user_prompt": user_text,
            "system_truncated": bool(system_clipped),
            "user_truncated": bool(user_clipped),
            "system_chars": len(str(system_prompt or "")),
            "user_chars": len(str(user_prompt or "")),
            "contexts": self._contexts(contexts),
            "images": self._images(image_urls),
            "included": [str(item) for item in (included or [])],
            "excluded": [
                {"block": str(block), "reason": str(reason)}
                for block, reason in (excluded or [])
            ],
            "ok": ok,
            "error": str(error or "")[:300],
            "reply_chars": int(reply_chars or 0),
        }
        self._rows.appendleft(row)
        return row

    @staticmethod
    def _contexts(contexts: Iterable[Any] | None) -> list[dict[str, Any]]:
        """contexts 只留"有没有、几条、什么类型"，正文按需要再看请求本身。"""

        rows: list[dict[str, Any]] = []
        for item in contexts or []:
            if isinstance(item, Mapping):
                rows.append(
                    {
                        "type": str(item.get("type") or item.get("role") or ""),
                        "chars": len(str(item.get("text") or item.get("content") or "")),
                    }
                )
            else:
                rows.append({"type": type(item).__name__, "chars": len(str(item))})
        return rows

    @staticmethod
    def _images(image_urls: Iterable[str] | None) -> dict[str, Any]:
        """图片只报数量与"有没有"，不把内容或本地路径抄进来。"""

        urls = [str(item) for item in (image_urls or []) if str(item or "").strip()]
        return {
            "count": len(urls),
            "kinds": sorted({("url" if "://" in item else "file") for item in urls}),
        }

    # ---------------- 读 ----------------

    def rows(self, kind: str = "", limit: int = KEEP) -> list[dict[str, Any]]:
        rows = list(self._rows)
        if kind:
            rows = [row for row in rows if row["kind"] == kind]
        return rows[: max(0, int(limit))]

    def summary(self) -> dict[str, Any]:
        """给调试页的汇总：每类各几条、最近一条是什么时候。"""

        counts: dict[str, int] = {name: 0 for name in CALL_KINDS}
        latest: dict[str, float] = {}
        for row in self._rows:
            name = row["kind"]
            counts[name] = counts.get(name, 0) + 1
            latest[name] = max(latest.get(name, 0.0), float(row.get("at") or 0.0))
        return {
            "total": len(self._rows),
            "counts": counts,
            "latest": latest,
            "labels": dict(CALL_KIND_LABELS),
        }

    def clear(self) -> None:
        self._rows.clear()
