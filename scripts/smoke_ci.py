#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 冒烟测试：在进程内起服务、打 API、断言、关掉。

为什么不用 shell 后台进程 + curl 轮询：那种写法在 CI 上容易假死——
后台进程没起来、端口被占、kill 不掉，都会变成一条说不清原因的失败。
这里用端口 0 让系统分配，跑在临时目录里，全程可复现。

断言从代码里推导，不写死常量。早先 CI 里把协议列表硬编码成 3 种，
加了 Gemini 之后断言必挂却没人发现——写死的东西一定会漂移。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tokenfurnace import __version__                      # noqa: E402
from tokenfurnace.providers import AUTH_STYLES, PROTOCOLS  # noqa: E402
from tokenfurnace.server import App, Handler, Server       # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tf-smoke-")
    app = App(pathlib.Path(tmp))
    Handler.app = app

    # 端口 0：由系统分配一个空闲端口，不会和别的任务撞车
    httpd = Server(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"服务已在 {base} 起来（临时目录 {tmp}）\n")

    try:
        state = json.loads(get(f"{base}/api/state"))
        cfg = state["config"]
        live = state["live"]

        print("接口形状：")
        check(state.get("version") == __version__, f"version == {__version__}")
        check(bool(cfg.get("profiles")), "config.profiles 非空")
        check(cfg.get("active_profile") in [p["id"] for p in cfg["profiles"]],
              "active_profile 指向一个存在的配置")

        print("\n协议与认证字段（从代码推导，不写死）：")
        check(set(state.get("protocols", {})) == set(PROTOCOLS),
              f"protocols == {sorted(PROTOCOLS)}")
        check(set(state.get("auth_styles", {})) == set(AUTH_STYLES),
              f"auth_styles == {sorted(AUTH_STYLES)}")
        for key, spec in PROTOCOLS.items():
            check(spec.get("auth") in AUTH_STYLES,
                  f"{key} 的默认认证字段 {spec.get('auth')} 存在")

        print("\n实时指标：")
        for field in ("rate_instant", "rate_peak", "spark", "series",
                      "sessions", "session_count", "workers_in_use",
                      "instant_window", "effective_window"):
            check(field in live, f"live.{field} 存在")
        check(live.get("session_count") == 0 and not live.get("running"),
              "刚起来时没有会话在跑")
        # 空闲时没有会话，聚合出的 spark 是空列表；有会话时才铺满 40 个桶
        check(isinstance(live.get("spark"), list)
              and len(live["spark"]) in (0, 40),
              f"spark 是列表且长度合理（当前 {len(live.get('spark') or [])}）")
        check("recent" not in live, "raw recent 不该出现在响应里")

        print("\n静态资源：")
        html = get(f"{base}/").decode("utf-8", "replace")
        check("<title>" in html and "app.js" in html, "/ 返回前端页面")
        check(len(get(f"{base}/static/app.js")) > 10000, "/static/app.js 有内容")
        check(len(get(f"{base}/static/style.css")) > 5000, "/static/style.css 有内容")

        print("\n配置文件落在临时目录里（没污染仓库）：")
        check(app.config.path.parent == pathlib.Path(tmp),
              f"config 路径在临时目录：{app.config.path}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        app.store.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
