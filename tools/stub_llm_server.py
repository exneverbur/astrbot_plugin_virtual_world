"""本地联调用的假 LLM 服务（OpenAI 兼容）。

用途：在没有真实模型账号的情况下，让 AstrBot 跑通完整消息管线——
既能观察插件注入到 system_prompt 的内容，也能让"接管模式"拿到合法的 JSON 动作。
所有请求都会原样追加写入日志文件，便于核对提示词。

用法：
    python tools/stub_llm_server.py --port 9909 --log stub_llm.jsonl

然后在 AstrBot 里加一个 Provider：
    type: openai_chat_completion
    api_base: http://127.0.0.1:9909/v1
    key: sk-stub
    model: stub-model

判定规则：
- system_prompt 里出现「第 5 层：输出格式」→ 认为这是插件的自主行为请求，返回 JSON 动作；
- 否则返回一句普通的角色回复。
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORLD_MARKER = "虚拟世界状态"
JSON_MARKER = "第 5 层：输出格式"

DEFAULT_REPLY = "嗯，我在书房看书呢，你叫我干嘛呀？"
# 接管模式/自主行为用的 JSON：注意 reasoning 写在 actions 前面
AUTONOMOUS_REPLY = json.dumps(
    {
        "reasoning": {
            "env": "书房，桌上摊着书",
            "state": "刚醒不久，精力一般",
            "mood": "平静",
            "who": "群友在跟我说话",
            "intent": "随口应一句，然后继续待着",
        },
        # 顺手给的一句话总结：会被存成「互动记忆」，下次在同一个地方能想起来
        "memory": "群友来找我说话，我随口应了一句",
        "actions": [{"type": "say", "messages": ["窗外的云好像散了，我去看看。"]}],
    },
    ensure_ascii=False,
)

LOG_PATH = "stub_llm.jsonl"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs) -> None:  # 静音默认日志
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": "stub-model", "object": "model", "owned_by": "local"}],
                }
            )
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"_raw": raw.decode("utf-8", "replace")}

        messages = payload.get("messages") or []
        system_prompt = "\n".join(
            str(item.get("content") or "")
            for item in messages
            if isinstance(item, dict) and item.get("role") == "system"
        )
        record = {
            "at": time.time(),
            "path": self.path,
            "model": payload.get("model"),
            "tools": payload.get("tools"),
            "tool_choice": payload.get("tool_choice"),
            "messages": messages,
            "has_world_block": WORLD_MARKER in system_prompt,
            "is_autonomous": JSON_MARKER in system_prompt,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send_json({"error": "not found"}, status=404)
            return

        content = AUTONOMOUS_REPLY if record["is_autonomous"] else DEFAULT_REPLY
        self._send_json(
            {
                "id": f"chatcmpl-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


def main() -> None:
    global LOG_PATH
    parser = argparse.ArgumentParser(description="假的 OpenAI 兼容服务（联调用）")
    parser.add_argument("--port", type=int, default=9909)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default="stub_llm.jsonl")
    args = parser.parse_args()
    LOG_PATH = args.log
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"stub LLM listening on http://{args.host}:{args.port}/v1  log={LOG_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
