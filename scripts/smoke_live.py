#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对一个**已经跑起来**的 TokenFurnace 服务做端到端联调。

会真的发请求、真的消耗 token，所以请求数默认很小。

    python scripts/smoke_live.py
    python scripts/smoke_live.py --url http://127.0.0.1:8760 --requests 6
    python scripts/smoke_live.py --input-chars 20000 --concurrency 2

前提：控制台里已经配好一个带密钥的 profile（本脚本不碰密钥，直接用当前激活的那个）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(base: str, path: str, body=None, method: str = "GET", timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description="TokenFurnace 端到端联调")
    ap.add_argument("--url", default="http://127.0.0.1:8760")
    ap.add_argument("--requests", type=int, default=6, help="本次跑几个请求")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--input-chars", type=int, default=30000)
    ap.add_argument("--model", default="", help="留空则用 profile 里勾选的第一个模型")
    a = ap.parse_args()
    base = a.url.rstrip("/")

    st = call(base, "/api/state")
    pid = st["active_profile"]
    prof = next(p for p in st["config"]["profiles"] if p["id"] == pid)
    print(f"[1] 服务 v{st['version']} · 当前配置 {prof['name']} ({pid})")
    print(f"    协议 {prof['protocol']} · base_url {prof['base_url'] or '(空)'} · "
          f"密钥 {'已配置' if prof['auth'].get('has_key') else '缺失'}")
    if not prof["auth"].get("has_key"):
        print("    [!] 这个配置没有可用密钥，先在控制台里补上。")
        return 1

    r = call(base, "/api/test", {"profile_id": pid}, "POST")
    res = r["result"]
    print(f"[2] 连接自检 · 模型列表 {'OK' if res['models_ok'] else '失败'} · "
          f"对话 {'OK' if res['chat_ok'] else '失败'}")
    if res["models_error"]:
        print(f"    模型列表错误: {res['models_error']}")
    if res["chat_error"]:
        print(f"    对话错误: {res['chat_error']}")
    if not res["models_ok"]:
        return 1

    r = call(base, "/api/models", {"profile_id": pid}, "POST")
    ids = [m["id"] for m in r["models"]]
    print(f"[3] 拉到 {len(ids)} 个模型")

    model = a.model or (prof.get("selected") or [{}])[0].get("id") or (ids[0] if ids else "")
    if not model:
        print("    [!] 没有可用模型")
        return 1

    spec = {
        "models": [{"id": model, "weight": 1, "points_per_1k": prof.get("points_per_1k", 0)}],
        "mode": "prefill", "concurrency": a.concurrency,
        "input_chars": a.input_chars, "max_tokens": 8,
        "reasoning_effort": "none", "cache_bust": True, "timeout": 120, "retry": 3,
        "max_points": 0, "max_total_tokens": 0, "max_requests": a.requests,
        "duration_min": 0, "enforce_windows": False,
        "limit_5h": 0, "limit_week": 0,
        "points_per_1k": prof.get("points_per_1k", 0), "safety_ratio": 0.97, "loop": False,
    }
    r = call(base, "/api/run/start", {"spec": spec, "profile_id": pid}, "POST")
    if not r.get("ok"):
        print(f"[4] 启动失败: {r.get('error')}")
        return 1
    run_id = r["run_id"]
    print(f"[4] 会话 {run_id} 启动 · 模型 {model}")

    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        L = call(base, "/api/state")["live"]
        print(f"    状态={L['status']:<8} 成功={L['ok']:<4} 失败={L['failed']:<3} "
              f"tokens={L['total_tokens']:>12,}  即时={L['rate_instant']:>9,.0f} tok/s  "
              f"峰值={L['rate_peak']:>9,.0f}")
        if not L["running"]:
            break

    d = call(base, f"/api/run/detail?run={run_id}")
    run = d["run"]
    total = run["prompt_tokens"] + run["completion_tokens"]
    print(f"\n[5] 结果 · 状态={run['status']} · 请求={run['requests']} · 失败={run['failed']} "
          f"· tokens={total:,} · 备注={run['note']}")
    for m in d["models"]:
        print(f"    模型 {m['model']}: {m['p'] + m['c']:,} tokens / {m['n']} 次")
    print(f"    错误分布: {d['errors'] or '无'}")

    csv = urllib.request.urlopen(
        f"{base}/api/export/requests.csv?run={run_id}", timeout=30).read().decode("utf-8-sig")
    lines = csv.strip().splitlines()
    print(f"[6] CSV 导出 {len(lines) - 1} 行数据")

    if run["failed"] and not run["requests"]:
        return 1
    print("\n端到端联调通过。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as e:
        print(f"连不上服务：{e}\n先运行 python run.py 把控制台起起来。", file=sys.stderr)
        raise SystemExit(2)
