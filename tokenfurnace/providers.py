"""协议适配层：请求格式、认证字段、用量字段的差异封装。

四套协议互不兼容，选错了直接 400 / 404：

| | Anthropic Messages | OpenAI Chat | OpenAI Responses | Gemini Native |
|---|---|---|---|---|
| 路径 | `/v1/messages` | `/v1/chat/completions` | `/v1/responses` | `/v1beta/models/{m}:generateContent` |
| 默认认证 | `x-api-key` | `Authorization: Bearer` | 同左 | `x-goog-api-key` |
| 系统提示 | 顶层 `system` | messages 里 `role: system` | 顶层 `instructions` | 顶层 `systemInstruction` |
| 输入字段 | `messages` | `messages` | `input` | `contents[].parts[].text` |
| 输出上限 | `max_tokens`（必填） | `max_tokens` | `max_output_tokens` | `generationConfig.maxOutputTokens` |
| 思考开关 | `thinking.budget_tokens` | `reasoning_effort` | `reasoning.effort` | `thinkingConfig.thinkingBudget` |
| 用量字段 | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` | `usageMetadata.promptTokenCount` / `candidatesTokenCount` |

认证字段（凭证放哪个头）与协议是**两个独立维度**：同一协议在不同网关下可能要求不同的头，
所以分开配置。Anthropic 生态里的 `ANTHROPIC_AUTH_TOKEN` 走 `Authorization: Bearer`，
`ANTHROPIC_API_KEY` 走 `x-api-key`。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "TokenFurnace/0.1 (+local)"
ANTHROPIC_VERSION = "2023-06-01"

# ---------------------------------------------------------------- 认证字段
# 只保留三种协议各自的原生字段，一一对应，不需要用户去猜。
# 「不认证」不需要单独选项——密钥留空时本来就不发凭证头。
AUTH_STYLES = {
    "bearer": {
        "label": "Authorization: Bearer",
        "header": "Authorization", "format": "Bearer {key}",
        "hint": "OpenAI 系。Anthropic 生态里对应 ANTHROPIC_AUTH_TOKEN",
    },
    "x-api-key": {
        "label": "x-api-key",
        "header": "x-api-key", "format": "{key}",
        "hint": "Anthropic 系。Anthropic 生态里对应 ANTHROPIC_API_KEY",
    },
    "x-goog-api-key": {
        "label": "x-goog-api-key",
        "header": "x-goog-api-key", "format": "{key}",
        "hint": "Google Gemini 系",
    },
}

# ---------------------------------------------------------------- 协议
PROTOCOLS = {
    "anthropic": {
        "label": "Anthropic Messages",
        "short": "Anthropic",
        "path": "/messages",
        "api_version": "v1",
        "auth": "x-api-key",
        "hint": "端点暴露 /v1/messages",
    },
    "openai-chat": {
        "label": "OpenAI Chat Completions",
        "short": "Chat",
        "path": "/chat/completions",
        "api_version": "v1",
        "auth": "bearer",
        "hint": "端点暴露 /v1/chat/completions",
    },
    "openai-responses": {
        "label": "OpenAI Responses API",
        "short": "Responses",
        "path": "/responses",
        "api_version": "v1",
        "auth": "bearer",
        "hint": "端点暴露 /v1/responses",
    },
    "gemini": {
        "label": "Gemini Native generateContent",
        "short": "Gemini",
        "path": "/models/{model}:generateContent",
        "api_version": "v1beta",
        "auth": "x-goog-api-key",
        "hint": "端点暴露 /v1beta/models/{model}:generateContent",
    },
}

RETRYABLE = {"rps", "server", "network", "timeout"}
FATAL = {"auth", "quota", "bad_request"}


# ---------------------------------------------------------------- 工具
def classify(status: int, body: str) -> tuple[str, str]:
    """把失败响应归成 (kind, message)。

    坑：不少平台在触发「每秒请求数」限流时返回
      {"error":{"message":"rps exhausted","type":"quota_exceeded_error"}}
    type 写着 quota_exceeded_error，但 message 是 rps，属于可重试的限流。
    按 type 判断会把限流误判成额度耗尽直接停机，所以以 message 为准。
    """
    msg, etype = (body or "").strip(), ""
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            err = j.get("error") or j
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("detail") or err.get("status") or msg)
                etype = str(err.get("type") or err.get("status") or "")
            elif isinstance(err, str):
                msg = err
    except Exception:
        pass
    low = msg.lower()
    if status in (401, 403):
        return "auth", msg
    if any(k in low for k in ("rps", "qps", "tpm", "rpm", "rate limit",
                              "too many", "overloaded", "resource_exhausted")):
        return "rps", msg
    if etype == "quota_exceeded_error" or any(
            k in low for k in ("exhaust", "entitlement", "insufficient", "quota",
                               "balance", "credit", "billing", "积分", "余额", "额度")):
        return "quota", msg
    if status == 404:
        return "bad_request", f"{msg}（路径不存在，检查接口协议是否选对）"
    if status in (400, 422):
        return "bad_request", msg
    if status == 429:
        return "rps", msg
    if status >= 500:
        return "server", msg
    return "other", msg


def join_url(base: str, path: str, version: str = "v1") -> str:
    """拼接 base_url 与路径，容忍几种常见写法。

    支持 https://api.x.com / …/v1 / …/api/paas/v4 / …/v1beta，
    也会剥掉误粘贴的完整端点（含 Gemini 的 /models/{id}:generateContent）。
    """
    b = (base or "").strip().rstrip("/")
    b = re.sub(r":generateContent$", "", b)
    for suf in ("/chat/completions", "/messages", "/responses", "/completions", "/models"):
        if b.endswith(suf):
            b = b[: -len(suf)]
            break
    b = re.sub(r"/models/[^/]+$", "", b)      # Gemini 的 /models/{id}
    if not b:
        return path
    if re.search(r"/v\d+[a-z0-9.\-]*$", b):
        return b + path
    return f"{b}/{version}{path}"


@dataclass
class CallResult:
    ok: bool
    status: int = 0
    latency: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    model: str = ""
    error_kind: str = ""
    error_msg: str = ""
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 适配器
class Adapter:
    def __init__(self, protocol: str, base_url: str, api_key: str,
                 headers: dict | None = None, timeout: float = 180,
                 auth_style: str | None = None):
        self.protocol = protocol if protocol in PROTOCOLS else "openai-chat"
        self.spec = PROTOCOLS[self.protocol]
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.auth_style = auth_style if auth_style in AUTH_STYLES else self.spec["auth"]
        self.headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        self._apply_auth()
        for k, v in (headers or {}).items():
            if k and v:
                self.headers[str(k)] = str(v)
        if self.protocol == "anthropic" and "anthropic-version" not in {
                k.lower() for k in self.headers}:
            self.headers["anthropic-version"] = ANTHROPIC_VERSION

    def _apply_auth(self) -> None:
        style = AUTH_STYLES.get(self.auth_style) or AUTH_STYLES["bearer"]
        if not self.api_key or not style.get("header"):
            return
        self.headers[style["header"]] = style["format"].format(key=self.api_key)

    # ------------------------------------------------------------ HTTP
    def _request(self, path: str, payload: dict | None, method: str = "POST",
                 model_in_path: str = ""):
        path = path.replace("{model}", model_in_path)
        url = join_url(self.base_url, path, self.spec["api_version"])
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode(errors="replace"), time.time() - t0, None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), time.time() - t0, None
        except socket.timeout:
            return 0, "", time.time() - t0, ("timeout", "请求超时")
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            kind = "timeout" if "timed out" in reason.lower() else "network"
            return 0, "", time.time() - t0, (kind, reason)
        except Exception as e:
            return 0, "", time.time() - t0, ("network", f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ 模型名
    def _model_for_path(self, model: str) -> str:
        """Gemini 的路径模板已含 models/ 前缀，这里只取裸 id，避免拼成 models/models/x。"""
        if self.protocol != "gemini":
            return model
        return model[len("models/"):] if model.startswith("models/") else model

    @staticmethod
    def _model_from_api(name: str) -> str:
        return name[len("models/"):] if name.startswith("models/") else name

    # ------------------------------------------------------------ 模型列表
    def list_models(self) -> dict:
        status, body, dt, err = self._request("/models", None, "GET")
        if err:
            return {"ok": False, "models": [], "error": err[1], "kind": err[0], "latency": dt}
        if status != 200:
            kind, msg = classify(status, body)
            return {"ok": False, "models": [], "error": msg or f"HTTP {status}",
                    "kind": kind, "latency": dt}
        try:
            j = json.loads(body)
        except Exception:
            return {"ok": False, "models": [], "error": "响应不是合法 JSON",
                    "kind": "bad_request", "latency": dt}
        items = j.get("data") if isinstance(j, dict) else j
        if isinstance(j, dict) and "models" in j:          # Gemini
            items = j["models"]
        out = []
        for m in (items or []):
            if isinstance(m, dict):
                mid = m.get("id") or m.get("model") or m.get("name")
                if mid:
                    out.append({"id": self._model_from_api(str(mid)),
                                "owned_by": str(m.get("owned_by") or ""),
                                "display": str(m.get("displayName") or m.get("display_name") or "")})
            elif isinstance(m, str):
                out.append({"id": self._model_from_api(m), "owned_by": "", "display": ""})
        out.sort(key=lambda x: x["id"])
        return {"ok": True, "models": out, "error": "", "kind": "", "latency": dt}

    # ------------------------------------------------------------ 组装请求
    def build(self, model: str, system: str, user: str, max_tokens: int,
              reasoning_effort: str | None, extra: dict | None = None):
        eff = (reasoning_effort or "").lower()
        p = self.protocol

        if p == "anthropic":
            payload = {"model": model, "max_tokens": max(1, max_tokens),
                       "messages": [{"role": "user", "content": user}]}
            if system:
                payload["system"] = system
            if eff and eff != "none":
                budget = {"low": 2048, "medium": 8192, "high": 24576,
                          "max": 49152}.get(eff, 8192)
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
                payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)
            return self.spec["path"], payload

        if p == "openai-responses":
            payload = {"model": model, "input": user,
                       "max_output_tokens": max(1, max_tokens)}
            if system:
                payload["instructions"] = system
            if eff and eff != "none":
                payload["reasoning"] = {"effort": "high" if eff == "max" else eff}
            return self.spec["path"], payload

        if p == "gemini":
            payload = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                       "generationConfig": {"maxOutputTokens": max(1, max_tokens)}}
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            if eff and eff != "none":
                budget = {"low": 2048, "medium": 8192, "high": 24576,
                          "max": 49152}.get(eff, 8192)
                payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": budget}
            return self.spec["path"], payload

        # openai-chat
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload = {"model": model, "messages": messages,
                   "max_tokens": max(1, max_tokens), "stream": False}
        if eff:
            payload["reasoning_effort"] = eff
        return self.spec["path"], payload

    # ------------------------------------------------------------ 解析响应
    @staticmethod
    def parse(j: dict) -> dict:
        u = j.get("usage") or {}
        meta = j.get("usageMetadata") or {}
        ctd = u.get("completion_tokens_details") or {}
        ptd = u.get("prompt_tokens_details") or {}
        itd = u.get("input_tokens_details") or {}
        otd = u.get("output_tokens_details") or {}
        return {
            "model": j.get("model") or j.get("modelVersion") or "",
            "prompt_tokens": int(u.get("prompt_tokens") or u.get("input_tokens")
                                 or meta.get("promptTokenCount") or 0),
            "completion_tokens": int(u.get("completion_tokens") or u.get("output_tokens")
                                     or meta.get("candidatesTokenCount") or 0),
            "cached_tokens": int(ptd.get("cached_tokens") or itd.get("cached_tokens")
                                 or u.get("cache_read_input_tokens")
                                 or meta.get("cachedContentTokenCount") or 0),
            "reasoning_tokens": int(ctd.get("reasoning_tokens")
                                    or otd.get("reasoning_tokens")
                                    or meta.get("thoughtsTokenCount") or 0),
        }

    # ------------------------------------------------------------ 对话
    def chat(self, model: str, system: str, user: str, max_tokens: int = 8,
             reasoning_effort: str | None = None, extra: dict | None = None) -> CallResult:
        path, payload = self.build(model, system, user, max_tokens, reasoning_effort, extra)
        status, body, dt, err = self._request(path, payload, "POST",
                                              self._model_for_path(model))
        if err:
            return CallResult(ok=False, latency=dt, model=model,
                              error_kind=err[0], error_msg=err[1])
        if status != 200:
            kind, msg = classify(status, body)
            return CallResult(ok=False, status=status, latency=dt, model=model,
                              error_kind=kind, error_msg=msg or f"HTTP {status}")
        try:
            j = json.loads(body)
        except Exception:
            return CallResult(ok=False, status=status, latency=dt, model=model,
                              error_kind="bad_response", error_msg="响应不是合法 JSON")
        u = self.parse(j)
        return CallResult(ok=True, status=status, latency=dt,
                          model=u["model"] or model,
                          prompt_tokens=u["prompt_tokens"],
                          completion_tokens=u["completion_tokens"],
                          cached_tokens=u["cached_tokens"],
                          reasoning_tokens=u["reasoning_tokens"], raw=j)


Provider = Adapter


def guess_protocol(base_url: str) -> str:
    """按 base_url 猜协议，减少选错的概率。"""
    b = (base_url or "").lower()
    if "anthropic" in b or "claude" in b:
        return "anthropic"
    if "generativelanguage" in b or "gemini" in b:
        return "gemini"
    return "openai-chat"


def test_connection(protocol: str, base_url: str, api_key: str,
                    headers: dict | None = None, auth_style: str | None = None,
                    timeout: float = 30) -> dict:
    """连通性自检：拉模型列表 + 打一个最小请求。"""
    a = Adapter(protocol, base_url, api_key, headers, timeout, auth_style)
    res = a.list_models()
    out = {"protocol": protocol, "models_ok": res["ok"], "models": res["models"],
           "models_error": res["error"], "chat_ok": False, "chat_error": "",
           "latency": res["latency"]}
    if res["ok"] and res["models"]:
        m = res["models"][0]["id"]
        c = a.chat(m, "", "ping", max_tokens=16)
        out["chat_ok"] = c.ok
        out["chat_error"] = "" if c.ok else f"[{c.error_kind}] {c.error_msg}"
        out["chat_model"] = m
        out["chat_latency"] = c.latency
    return out
