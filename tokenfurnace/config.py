"""配置层：多配置源、多 Profile、密钥来源抽象。

设计原则
--------
**不绑定任何服务商。** 这里只认识三样东西：
  1. `protocol` —— 接口协议（openai-chat / openai-responses / anthropic）
  2. `base_url` —— 端点根地址
  3. `auth`     —— 密钥从哪来、用哪种认证头

任何兼容上述协议的端点都能接，不需要为每家写适配代码。

配置来源（优先级由低到高）
  1. 内置默认值
  2. 配置文件 config.json（路径可用环境变量 TOKENFURNACE_CONFIG 覆盖）
  3. 环境变量注入（TOKENFURNACE_BASE_URL / TOKENFURNACE_API_KEY / TOKENFURNACE_MODEL /
     TOKENFURNACE_PROTOCOL）
  4. Web 界面里的运行时修改（落回 config.json）

API Key 三种来源：
  * inline —— 直接写在配置里
  * env    —— 只存环境变量名，运行时读取（推荐，不落盘）
  * file   —— 只存文件路径，运行时读取
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

WINDOW_5H = 5 * 3600
WINDOW_WEEK = 7 * 86400
CONFIG_VERSION = 2

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "active_profile": "default",
    "profiles": [
        {
            "id": "default",
            "name": "默认配置",
            "protocol": "openai-chat",
            "base_url": "",
            "auth": {"source": "env", "value": "", "ref": "TOKENFURNACE_API_KEY",
                     "style": "bearer"},
            "headers": {},
            "models": [],
            "selected": [],
            "limits": {"window_5h": 0, "week": 0, "unit": "tokens"},
            "points_per_1k": 0,
            # 积分池。留空 = 单池，行为等同上面的 limits。
            # 平台把积分拆成多个独立计额的池时（比如专属积分池 + 通用积分池），
            # 在这里逐池声明，工具才能在某个池用满时停手，
            # 而不是继续发请求让平台悄悄改从下一个池扣费。
            "pools": [],
            "note": "",
        }
    ],
    "ui": {"poll_ms": 1000, "confirm_big_budget": True},
    "engine_defaults": {
        "mode": "prefill",
        "concurrency": 4,
        "input_chars": 380000,
        "max_tokens": 8,
        "reasoning_effort": "none",
        "cache_bust": True,
        "timeout": 180,
        "retry": 5,
        "instant_window": 20,
        "safety_ratio": 0.97,
        "max_points": 0,
        "max_total_tokens": 0,
        "max_requests": 0,
        "duration_min": 0,
        "enforce_windows": False,
        "points_per_1k": 0,
        "loop": False,
    },
}


def new_profile(name: str = "新配置") -> dict:
    p = copy.deepcopy(DEFAULT_CONFIG["profiles"][0])
    p["id"] = f"p{int(time.time() * 1000) % 10 ** 9}"
    p["name"] = name
    return p


class ConfigStore:
    """配置的加载 / 保存 / 校验 / 迁移。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = copy.deepcopy(DEFAULT_CONFIG)

    # ------------------------------------------------------------------ #
    @staticmethod
    @staticmethod
    def default_path(base: Path | None = None) -> Path:
        """默认配置文件路径。优先级：环境变量 > 传入的 base > 当前工作目录。

        这里刻意不用 `Path(__file__).parent.parent`——那个写法在源码克隆里没问题，
        但 pip 安装后包在 site-packages 里，配置就会试图写进 site-packages，
        要么写不进去，要么污染环境。用 CWD 对两种装法都成立，也是 CLI 工具的常规行为。
        """
        env = os.environ.get("TOKENFURNACE_CONFIG")
        if env:
            return Path(env).expanduser()
        return (base or Path.cwd()) / "config.json"

    def load(self) -> dict:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), raw)
            except Exception as e:
                raise RuntimeError(f"配置文件解析失败 {self.path}: {e}") from e
        self._migrate()
        self._apply_env()
        if not self.data["profiles"]:
            self.data["profiles"] = copy.deepcopy(DEFAULT_CONFIG["profiles"])
        if not self.data.get("active_profile") or not any(
                p["id"] == self.data["active_profile"] for p in self.data["profiles"]):
            self.data["active_profile"] = self.data["profiles"][0]["id"]
        # pools 是后加的字段，老配置里没有。放在这里统一补齐而不是塞进 _migrate：
        # _migrate 只在版本号落后时跑一次，而这里每次加载都会过一遍，幂等且不会漏。
        for p in self.data["profiles"]:
            p.setdefault("pools", [])
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        """把 v1 配置升级到 v2（v1 没有 protocol / auth.style 字段）。"""
        ver = int(self.data.get("version") or 1)
        if ver >= CONFIG_VERSION:
            return
        for p in self.data.get("profiles", []):
            p.setdefault("protocol", "openai-chat")
            auth = p.setdefault("auth", {})
            auth.setdefault("style", "bearer")
            p.setdefault("points_per_1k", 0)
        self.data["version"] = CONFIG_VERSION

    def _apply_env(self) -> None:
        """环境变量可以整体注入一个临时 profile，方便 CI / 容器 / 快速试用。"""
        base = os.environ.get("TOKENFURNACE_BASE_URL")
        key = os.environ.get("TOKENFURNACE_API_KEY")
        model = os.environ.get("TOKENFURNACE_MODEL")
        proto = os.environ.get("TOKENFURNACE_PROTOCOL")
        if not (base or key or model or proto):
            return
        prof = next((p for p in self.data["profiles"] if p["id"] == "__env__"), None)
        if prof is None:
            prof = new_profile("环境变量注入")
            prof["id"] = "__env__"
            prof["auth"] = {"source": "env", "value": "", "ref": "TOKENFURNACE_API_KEY",
                            "style": "bearer"}
            self.data["profiles"].insert(0, prof)
        if base:
            prof["base_url"] = base
        if proto:
            prof["protocol"] = proto
        if model:
            prof["models"] = [m.strip() for m in model.split(",") if m.strip()]
        if key:
            prof["auth"] = {"source": "inline", "value": key, "ref": "",
                            "style": prof.get("auth", {}).get("style", "bearer")}
        self.data["active_profile"] = "__env__"

    # ------------------------------------------------------------------ #
    # Profile 操作
    # ------------------------------------------------------------------ #
    def get_profile(self, pid: str | None = None) -> dict:
        """宽松查找：找不到就回退到第一个。用于展示，不用于启动会话。"""
        pid = pid or self.data.get("active_profile")
        for p in self.data["profiles"]:
            if p["id"] == pid:
                return p
        if self.data["profiles"]:
            return self.data["profiles"][0]
        prof = new_profile()
        self.data["profiles"].append(prof)
        self.data["active_profile"] = prof["id"]
        return prof

    def find_profile(self, pid: str | None) -> dict | None:
        """严格查找：找不到返回 None。

        启动会话必须用这个——宽松查找会静默回退到第一个配置，
        结果就是你以为在跑配置 X，实际烧的是配置 A 的额度。
        """
        if not pid:
            return self.get_profile()
        for p in self.data["profiles"]:
            if p["id"] == pid:
                return p
        return None

    def add_profile(self, name: str | None = None) -> dict:
        prof = new_profile(name or f"配置 {len(self.data['profiles']) + 1}")
        existing = {p["id"] for p in self.data["profiles"]}
        while prof["id"] in existing:
            prof["id"] += "x"
        self.data["profiles"].append(prof)
        return prof

    def delete_profile(self, pid: str) -> bool:
        before = len(self.data["profiles"])
        self.data["profiles"] = [p for p in self.data["profiles"] if p["id"] != pid]
        if len(self.data["profiles"]) == before:
            return False
        if self.data.get("active_profile") == pid:
            self.data["active_profile"] = (self.data["profiles"][0]["id"]
                                           if self.data["profiles"] else "")
        return True

    ALLOWED_PROFILE_KEYS = ("name", "protocol", "base_url", "headers", "models",
                            "selected", "limits", "note", "points_per_1k")

    def import_profiles(self, payload) -> int:
        """导入 profile：支持单个对象、列表，或含 profiles 的整份配置。"""
        items = payload
        if isinstance(payload, dict):
            items = payload.get("profiles", [payload])
        if not isinstance(items, list):
            raise ValueError("导入内容必须是 profile 或 profile 列表")
        n = 0
        existing = {p["id"] for p in self.data["profiles"]}
        for it in items:
            if not isinstance(it, dict) or not (it.get("base_url") or it.get("name")):
                continue
            prof = new_profile(it.get("name"))
            for k in self.ALLOWED_PROFILE_KEYS:
                if k in it:
                    prof[k] = it[k]
            if isinstance(it.get("auth"), dict):
                prof["auth"] = it["auth"]
            base_id = str(it.get("id") or prof["id"])
            pid, n2 = base_id, 1
            while pid in existing:
                n2 += 1
                pid = f"{base_id}-{n2}"
            prof["id"] = pid
            existing.add(pid)
            self.data["profiles"].append(prof)
            n += 1
        return n

    # ------------------------------------------------------------------ #
    # 密钥
    # ------------------------------------------------------------------ #
    @staticmethod
    def resolve_key(profile: dict) -> str:
        auth = profile.get("auth") or {}
        src = auth.get("source", "inline")
        if src == "env":
            ref = (auth.get("ref") or "").strip()
            return os.environ.get(ref, "") if ref else ""
        if src == "file":
            ref = (auth.get("ref") or "").strip()
            if not ref:
                return ""
            p = Path(ref).expanduser()
            if not p.exists() or not p.is_file():
                return ""
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                return ""
        return (auth.get("value") or "").strip()

    @staticmethod
    def mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 10:
            return key[:2] + "*" * max(0, len(key) - 2)
        return f"{key[:6]}{'*' * 8}{key[-4:]}"

    def public_view(self) -> dict:
        """给前端的配置视图：密钥一律脱敏，绝不外泄明文。"""
        d = copy.deepcopy(self.data)
        for p in d["profiles"]:
            auth = p.setdefault("auth", {})
            resolved = self.resolve_key(p)
            auth["has_key"] = bool(resolved)
            auth["masked"] = self.mask_key(resolved)
            if auth.get("source") == "inline":
                auth["value"] = self.mask_key(auth.get("value", ""))
        return d


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
