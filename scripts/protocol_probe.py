#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协议差异实证：起一个本地 mock 端点，把四种协议**真实发出的 HTTP 请求**抓下来。

不依赖任何外部服务，纯离线。用来回答「协议是不是只改了个名字」。

    python scripts/protocol_probe.py

会打印每种协议的：请求路径、认证头、请求体顶层字段、系统提示的位置，
以及从响应里解析出的 token 用量——全部是适配层实际发出/收到的内容。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokenfurnace.providers import PROTOCOLS, Adapter  # noqa: E402

CAPTURED: list[dict] = []

# 各协议的响应形态完全不同，mock 要按协议返回对应结构，
# 这样才能顺带验证适配层的用量解析
RESPONSES = {
    "openai-chat": {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "demo-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 5,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    },
    "openai-responses": {
        "id": "resp_1", "object": "response", "model": "demo-model",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 2345, "output_tokens": 7,
                  "input_tokens_details": {"cached_tokens": 0}},
    },
    "anthropic": {
        "id": "msg_1", "type": "message", "model": "demo-model", "role": "assistant",
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 3456, "output_tokens": 9,
                  "cache_read_input_tokens": 0},
    },
    "gemini": {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP"}],
        "modelVersion": "demo-model",
        "usageMetadata": {"promptTokenCount": 4567, "candidatesTokenCount": 11,
                          "cachedContentTokenCount": 0},
    },
}

MODELS = {
    "openai-chat": {"data": [{"id": "demo-model", "owned_by": "mock"}]},
    "openai-responses": {"data": [{"id": "demo-model", "owned_by": "mock"}]},
    "anthropic": {"data": [{"id": "demo-model", "owned_by": "mock"}]},
    "gemini": {"models": [{"name": "models/demo-model", "displayName": "Demo"}]},
}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        CAPTURED.append({"method": "GET", "path": self.path,
                         "headers": dict(self.headers), "body": None})
        for proto, payload in MODELS.items():
            if proto == "gemini" and "models" in self.path:
                return self._reply(payload)
            if proto != "gemini" and self.path.endswith("/models"):
                return self._reply(payload)
        self._reply({"data": []})

    def do_POST(self):
        body = self._read()
        CAPTURED.append({"method": "POST", "path": self.path,
                         "headers": dict(self.headers), "body": body})
        for proto, resp in RESPONSES.items():
            path = PROTOCOLS[proto]["path"].replace("{model}", "demo-model")
            if self.path.endswith(path):
                return self._reply(resp)
        self._reply({"error": {"message": "no mock for this path"}}, 404)


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"mock 端点已启动：{base}\n")

    rows = []
    for proto in PROTOCOLS:
        CAPTURED.clear()
        a = Adapter(proto, base, "SECRET-KEY", None, 15)
        res = a.chat("demo-model", "SYSTEM-PROMPT", "USER-MESSAGE", max_tokens=8,
                     reasoning_effort="none")
        cap = CAPTURED[-1] if CAPTURED else {}
        hdrs = {k: v for k, v in (cap.get("headers") or {}).items()
                if k.lower() in ("authorization", "x-api-key", "x-goog-api-key",
                                 "anthropic-version")}
        body = cap.get("body") or {}
        rows.append({
            "协议": PROTOCOLS[proto]["label"],
            "路径": cap.get("path", ""),
            "认证头": ", ".join(f"{k}: {v}" for k, v in hdrs.items()) or "(无)",
            "请求体顶层字段": ", ".join(sorted(body)),
            "系统提示位置": ("顶层 system" if "system" in body else
                             "顶层 systemInstruction" if "systemInstruction" in body else
                             "顶层 instructions" if "instructions" in body else
                             "messages[0].role=system"),
            "解析出的用量": f"in={res.prompt_tokens} out={res.completion_tokens}",
        })

    for r in rows:
        print("=" * 76)
        for k, v in r.items():
            print(f"  {k:<14} {v}")

    print("=" * 76)
    paths = {r["路径"] for r in rows}
    auths = {r["认证头"] for r in rows}
    fields = {r["请求体顶层字段"] for r in rows}
    usage = {r["解析出的用量"] for r in rows}
    print(f"\n四种协议产生了 {len(paths)} 个不同路径、{len(auths)} 种不同认证头、"
          f"{len(fields)} 种不同请求体、{len(usage)} 种不同用量解析。")
    # 认证头只有 3 种是正常的：Chat 与 Responses 同属 OpenAI 系，共用 Bearer
    ok = len(paths) == 4 and len(auths) == 3 and len(fields) == 4 and len(usage) == 4
    print("结论：" + ("四种协议确实各不相同，不是换个名字。" if ok else "存在重复，检查适配层。"))
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
