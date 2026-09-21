"""本地 HTTP 服务：REST API + 静态界面。

只用标准库 http.server，绑 127.0.0.1，默认不对外网暴露。
"""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import pkgutil
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import ConfigStore, WINDOW_5H, WINDOW_WEEK
from .engine import (MAX_TOTAL_WORKERS, MODE_LABELS, Engine, RunSpec,
                     char_to_token_hint)
from .providers import AUTH_STYLES, PROTOCOLS, Adapter, guess_protocol, test_connection
from .store import DEFAULT_RETENTION_DAYS, Store

# 前端资源一律通过 pkgutil 读，不拼文件系统路径。
# 拼路径在源码安装和 pip 安装下都没问题，但打进单文件 zipapp 之后
# `Path(__file__).parent / "web"` 指向的是压缩包内的虚拟路径，is_file() 恒为假。
MAX_BODY = 4 * 1024 * 1024


class App:
    """把 config / store / engine 组装在一起，供 handler 调用。"""

    def __init__(self, base_dir: Path, config_path: Path | None = None):
        self.base_dir = base_dir
        # 配置和账本都以 base_dir 为准，别一个走 base_dir、一个走 CWD
        self.config = ConfigStore(config_path or ConfigStore.default_path(base_dir))
        self.config.load()
        self.store = Store(base_dir / "data" / "tokenfurnace.db")
        if getattr(self.store, "_migrated", False):
            # 刚补上 points 列：历史记录没有积分，用配置里已知的系数回填一次（近似值）。
            # 取所有 profile 里最大的非零系数——数据可能是用别的 profile 跑出来的。
            coefs = [float(p.get("points_per_1k") or 0) for p in self.config.data["profiles"]]
            coef = max(coefs) if coefs else 0.0
            n = self.store.backfill_points(coef)
            if n:
                print(f"[迁移] 已为 {n} 条历史记录回填积分（系数 {coef}）")
        self.store.mark_stale_runs()
        # 明细按保留期清理，避免库无限膨胀拖慢窗口查询（周窗口只需 7 天）
        retention = int((self.config.data.get("ui") or {}).get("retention_days")
                        or DEFAULT_RETENTION_DAYS)
        pruned = self.store.prune(retention)
        if pruned:
            print(f"[维护] 已清理 {pruned:,} 条超过 {retention} 天的明细")
        self.store.optimize()
        self.engine = Engine(
            self.store,
            max_total_workers=int((self.config.data.get("engine_defaults") or {})
                                  .get("max_total_workers") or MAX_TOTAL_WORKERS))
        self._lock = threading.RLock()
        self.started_at = time.time()

    # ------------------------------------------------------------------ #
    def state(self) -> dict:
        with self._lock:
            cfg = self.config.public_view()
        active = self.config.get_profile()
        ed = self.config.data.get("engine_defaults") or {}
        # 空闲时也要按当前配置的额度口径展示窗口占用
        self.engine.set_window_context(
            (active.get("limits") or {}).get("window_5h") or 0,
            (active.get("limits") or {}).get("week") or 0,
            bool(ed.get("enforce_windows")),
            active.get("points_per_1k") or 0,
        )
        return {
            "version": __version__,
            "server_time": time.time(),
            "uptime": time.time() - self.started_at,
            "config": cfg,
            "active_profile": active.get("id", ""),
            "active_profile_name": active.get("name", ""),
            "active_has_key": bool(self.config.resolve_key(active)),
            "live": self.engine.snapshot(),
            "logs": self.engine.recent_logs(),
            "history": self.store.history(40),
            "totals": self.store.totals(),
            "presets": {},
            "protocols": {k: {"label": v["label"], "short": v["short"],
                              "auth": v["auth"], "hint": v["hint"]}
                          for k, v in PROTOCOLS.items()},
            "auth_styles": AUTH_STYLES,
            "modes": MODE_LABELS,
            "windows": {"h5": WINDOW_5H, "week": WINDOW_WEEK},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = f"TokenFurnace/{__version__}"
    # 默认是 HTTP/1.0，每次请求都要新建 TCP 连接。界面每秒轮询一次，
    # 开 keep-alive 后省掉每秒一次的握手开销。
    protocol_version = "HTTP/1.1"
    app: App = None          # 由 serve() 注入

    # ------------------------------------------------------------------ #
    def log_message(self, fmt, *args):        # 静音默认访问日志
        pass

    # ------------------------------------------------------------------ #
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False,
                                    default=str).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ #
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                return self._json(self.app.state())
            if path == "/api/export/requests.csv":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                run_id = (q.get("run") or [None])[0]
                csv = self.app.store.export_requests_csv(run_id)
                return self._send(200, csv.encode("utf-8-sig"),
                                  "text/csv; charset=utf-8",
                                  {"Content-Disposition":
                                   'attachment; filename="tokenfurnace-requests.csv"'})
            if path == "/api/export/runs.csv":
                csv = self.app.store.export_runs_csv()
                return self._send(200, csv.encode("utf-8-sig"),
                                  "text/csv; charset=utf-8",
                                  {"Content-Disposition":
                                   'attachment; filename="tokenfurnace-runs.csv"'})
            if path == "/api/export/config.json":
                data = json.dumps(self.app.config.data, ensure_ascii=False, indent=2)
                return self._send(200, data.encode("utf-8"), "application/json",
                                  {"Content-Disposition":
                                   'attachment; filename="tokenfurnace-config.json"'})
            if path == "/api/run/detail":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                run_id = (q.get("run") or [""])[0]
                return self._json({
                    "ok": True,
                    "run": self.app.store.run_stats(run_id),
                    "buckets": self.app.store.run_buckets(run_id),
                    "models": self.app.store.model_breakdown(run_id),
                    "errors": self.app.store.error_breakdown(run_id),
                })
            return self._err("未知路径", 404)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    # ------------------------------------------------------------------ #
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except Exception as e:
            return self._err(f"请求体解析失败：{e}")
        try:
            return self._route_post(path, body)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    def _route_post(self, path: str, body: dict):
        app = self.app
        cfg = app.config

        # ---------------- 配置 ---------------- #
        if path == "/api/profile/save":
            prof = body.get("profile") or {}
            pid = prof.get("id") or ""
            target = None
            if pid:
                target = next((p for p in cfg.data["profiles"] if p["id"] == pid), None)
            if target is None:
                target = cfg.add_profile(prof.get("name"))
            for k in cfg.ALLOWED_PROFILE_KEYS:
                if k in prof:
                    target[k] = prof[k]
            if isinstance(prof.get("auth"), dict):
                a = prof["auth"]
                cur = target.setdefault("auth", {})
                cur["source"] = a.get("source", cur.get("source", "env"))
                cur["style"] = a.get("style", cur.get("style", "bearer"))
                if cur["source"] in ("env", "file"):
                    cur["ref"] = a.get("ref", cur.get("ref", ""))
                else:
                    cur["ref"] = ""
                # 前端回传的是脱敏串时，不要用它覆盖真实密钥
                v = a.get("value", "")
                if cur["source"] == "inline" and v and "*" not in v:
                    cur["value"] = v
            cfg.data["active_profile"] = target["id"]
            cfg.save()
            return self._json({"ok": True, "id": target["id"]})

        if path == "/api/profile/activate":
            pid = body.get("id", "")
            if not any(p["id"] == pid for p in cfg.data["profiles"]):
                return self._err("Profile 不存在")
            cfg.data["active_profile"] = pid
            cfg.save()
            return self._json({"ok": True})

        if path == "/api/profile/duplicate":
            src = cfg.get_profile(body.get("id"))
            new = cfg.add_profile((src.get("name") or "配置") + " 副本")
            for k in cfg.ALLOWED_PROFILE_KEYS:
                if k in src:
                    new[k] = copy.deepcopy(src[k])
            if isinstance(src.get("auth"), dict):
                new["auth"] = copy.deepcopy(src["auth"])   # 连真实密钥一起复制
            cfg.data["active_profile"] = new["id"]
            cfg.save()
            return self._json({"ok": True, "id": new["id"]})

        if path == "/api/profile/delete":
            pid = body.get("id", "")
            if app.engine.running:
                return self._err("会话运行中，无法删除配置")
            ok = cfg.delete_profile(pid)
            cfg.save()
            return self._json({"ok": ok})

        if path == "/api/profile/import":
            try:
                n = cfg.import_profiles(body.get("data"))
            except Exception as e:
                return self._err(f"导入失败：{e}")
            cfg.save()
            return self._json({"ok": True, "imported": n})

        if path == "/api/config/engine":
            cfg.data["engine_defaults"].update(body.get("defaults") or {})
            cfg.save()
            return self._json({"ok": True})

        if path == "/api/config/window":
            # 即时速率窗口：立即生效，不用等下次启动会话
            sec = app.engine.set_instant_window(body.get("seconds"))
            cfg.data["engine_defaults"]["instant_window"] = sec
            cfg.save()
            return self._json({"ok": True, "seconds": sec})

        # ---------------- 连接与模型 ---------------- #
        if path == "/api/test":
            prof = self._profile_or_body(body)
            key = body.get("api_key") or cfg.resolve_key(prof)
            res = test_connection(
                prof.get("protocol", "openai-chat"), prof.get("base_url", ""), key,
                prof.get("headers"), (prof.get("auth") or {}).get("style"), timeout=30)
            return self._json({"ok": True, "result": res})

        if path == "/api/models":
            prof = self._profile_or_body(body)
            key = body.get("api_key") or cfg.resolve_key(prof)
            a = Adapter(prof.get("protocol", "openai-chat"), prof.get("base_url", ""),
                        key, prof.get("headers"), timeout=45,
                        auth_style=(prof.get("auth") or {}).get("style"))
            res = a.list_models()
            if res["ok"]:
                target = cfg.get_profile(prof.get("id"))
                target["models"] = [m["id"] for m in res["models"]]
                cfg.save()
            return self._json({"ok": res["ok"], "models": res["models"],
                               "error": res["error"], "latency": res["latency"]})

        if path == "/api/guess_protocol":
            return self._json({"ok": True,
                               "protocol": guess_protocol(body.get("base_url", ""))})

        # ---------------- 运行控制 ---------------- #
        if path == "/api/run/start":
            # 严格查找：绝不回退到别的配置，否则会烧错账号的额度
            prof = cfg.find_profile(body.get("profile_id"))
            if prof is None:
                return self._err(f"配置 {body.get('profile_id')} 不存在，"
                                 f"可能已被删除，请刷新页面")
            key = body.get("api_key") or cfg.resolve_key(prof)
            if not key:
                return self._err("该配置没有可用的 API Key，请在「连接配置」里补上。")
            spec = RunSpec.from_dict(body.get("spec") or {})
            spec.profile_id = prof.get("id", "")
            if not spec.models:
                spec.models = [{"id": m, "weight": 1, "points_per_1k": 0}
                               for m in (prof.get("selected") or [])]
            if not spec.models:
                return self._err("请先勾选至少一个模型。")
            if not spec.limit_5h:
                spec.limit_5h = float((prof.get("limits") or {}).get("window_5h") or 0)
            if not spec.limit_week:
                spec.limit_week = float((prof.get("limits") or {}).get("week") or 0)
            if not spec.points_per_1k:
                spec.points_per_1k = float(prof.get("points_per_1k") or 0)
            return self._json(app.engine.start(spec, prof, key))

        if path == "/api/run/start_batch":
            # 一次勾选多个配置，全部启动。每个会话用**各自**的密钥、模型和额度，
            # 策略参数（模式/并发/预算等）取当前表单这一份。
            ids = body.get("profile_ids") or []
            if not ids:
                return self._err("没有勾选任何配置")
            base = RunSpec.from_dict(body.get("spec") or {})
            started, failed = [], []
            for pid in ids:
                prof = cfg.find_profile(pid)
                if prof is None:
                    failed.append({"profile_id": pid, "name": str(pid),
                                   "error": "配置不存在"})
                    continue
                name = prof.get("name") or pid
                key = cfg.resolve_key(prof)
                if not key:
                    failed.append({"profile_id": pid, "name": name,
                                   "error": "没有可用的 API Key"})
                    continue
                spec = copy.deepcopy(base)
                spec.profile_id = prof.get("id", "")
                spec.models = [dict(m) for m in (prof.get("selected") or [])]
                if not spec.models:
                    failed.append({"profile_id": pid, "name": name,
                                   "error": "没有勾选任何模型"})
                    continue
                lim = prof.get("limits") or {}
                # 额度与换算系数一律取该配置自己的——表单里那份只属于当前配置，
                # 套到别人头上会把窗口限流算错
                spec.limit_5h = float(lim.get("window_5h") or 0)
                spec.limit_week = float(lim.get("week") or 0)
                spec.points_per_1k = float(prof.get("points_per_1k") or 0)
                r = app.engine.start(spec, prof, key)
                if r.get("ok"):
                    started.append({"sid": r["sid"], "name": name})
                else:
                    failed.append({"profile_id": pid, "name": name,
                                   "error": r.get("error")})
            return self._json({"ok": True, "started": started, "failed": failed})

        if path == "/api/run/stop":
            # 带 sid 停单个会话；不带则全部停掉
            app.engine.stop(body.get("sid") or None,
                            body.get("reason") or "手动停止")
            return self._json({"ok": True})

        # ---------------- 校准 ---------------- #
        if path == "/api/calibrate":
            tokens = float(body.get("tokens") or 0)
            points = float(body.get("points") or 0)
            if tokens <= 0:
                return self._err("请填入本窗口消耗的 token 数（可在历史里查看）")
            coef = points * 1000.0 / tokens if points > 0 else 0.0
            return self._json({"ok": True, "points_per_1k": coef})

        if path == "/api/calibrate/auto":
            # 用「5 小时窗口已消耗 tokens + 上限积分」反推
            prof = cfg.get_profile(body.get("profile_id"))
            limit = float(body.get("limit_5h") or
                          (prof.get("limits") or {}).get("window_5h") or 0)
            tokens = app.store.window_tokens(WINDOW_5H)
            if tokens <= 0 or limit <= 0:
                return self._err("窗口内还没有消耗记录，或上限为 0")
            return self._json({"ok": True, "tokens": tokens, "limit_5h": limit,
                               "points_per_1k": limit * 1000.0 / tokens})

        # ---------------- 其他 ---------------- #
        if path == "/api/estimate":
            spec = RunSpec.from_dict(body.get("spec") or {})
            per_req = char_to_token_hint(spec.input_chars) + spec.max_tokens
            models = spec.models or []
            coef = float(spec.points_per_1k or 0)
            if coef <= 0 and models:
                coef = max([float(m.get("points_per_1k") or 0) for m in models] or [0])
            rate = 12000.0
            out = {
                "tokens_per_request": per_req,
                "points_per_request": per_req / 1000.0 * coef if coef else 0,
                "assumed_rate": rate,
                "tokens_per_minute": rate * 60,
            }
            if spec.max_total_tokens:
                out["minutes_to_token_budget"] = spec.max_total_tokens / (rate * 60)
            if spec.max_points and coef > 0:
                need = spec.max_points * 1000.0 / coef
                out["tokens_to_point_budget"] = need
                out["minutes_to_point_budget"] = need / (rate * 60)
            return self._json({"ok": True, "estimate": out})

        return self._err("未知路径", 404)

    # ------------------------------------------------------------------ #
    def _profile_or_body(self, body: dict) -> dict:
        cfg = self.app.config
        pid = body.get("profile_id")
        if pid:
            prof = dict(cfg.get_profile(pid))
        else:
            prof = dict(cfg.get_profile())
        for k in ("base_url", "protocol", "headers"):
            if body.get(k):
                prof[k] = body[k]
        if body.get("auth_style"):
            prof["auth"] = {**(prof.get("auth") or {}), "style": body["auth_style"]}
        return prof

    def _static(self, rel: str):
        rel = rel.split("?")[0].lstrip("/")
        if not rel:
            rel = "index.html"
        # 路径穿越防护：pkgutil 会直接拼路径，不校验的话 ../ 能读到包外
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if ".." in parts or "\\" in rel or ":" in rel:
            return self._err("静态资源不存在", 404)
        try:
            data = pkgutil.get_data("tokenfurnace", "web/" + "/".join(parts))
        except Exception:
            data = None
        if data is None:
            return self._err("静态资源不存在", 404)
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)


class Server(ThreadingHTTPServer):
    """本地 HTTP 服务。

    `allow_reuse_address` 必须按平台分开设，否则会静默起出多个实例：

    * Windows 上 SO_REUSEADDR 允许**两个活着的进程绑同一个地址**，
      于是每次「重启」都只是又起了一个，几个进程一起抢连接、共用一个 SQLite 账本，
      而且谁接受连接是不确定的。关掉之后绑定已占用的端口会正常失败，
      serve() 里的端口递增逻辑才会真正生效。
    * POSIX 上 SO_REUSEADDR 只影响 TIME_WAIT 期间的重绑，是重启后能立刻再用
      同一端口的关键，所以要留着。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def serve(base_dir: Path, host: str = "127.0.0.1", port: int = 8760,
          open_browser: bool = True, config_path: Path | None = None) -> None:
    app = App(base_dir, config_path)
    Handler.app = app

    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = Server((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"{host}:{port}~{port + 19} 都被占用，请用 --port 换一个端口。")

    url = f"http://{host}:{port}/"
    print("=" * 64)
    print("  TokenFurnace · LLM Token 消费与额度管理")
    print("=" * 64)
    print(f"  控制台   : {url}")
    print(f"  配置文件 : {app.config.path}")
    print(f"  数据文件 : {app.store.path}")
    prof = app.config.get_profile()
    print(f"  当前配置 : {prof.get('name')} ({prof.get('base_url') or '未填写 Base URL'})")
    print("  按 Ctrl+C 退出")
    print("=" * 64)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        if app.engine.running:
            app.engine.stop_all("服务退出")
        httpd.server_close()
        app.store.close()
        print("已退出。")
