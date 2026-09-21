#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把真实前端（index.html + style.css + app.js）打包成一个自包含的静态预览页。

用途：
  * 不用起后端就能看界面（也方便截 README 截图）
  * 界面改动后重新生成：python scripts/build_preview.py

    python scripts/build_preview.py            # 输出到 preview.html
    python scripts/build_preview.py -o x.html

页面里的数据是演示数据，fetch 被拦截后返回假后端响应；界面代码本身是原封不动的真实文件，
所以看到的就是真界面的样子。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokenfurnace.engine import MODE_LABELS                      # noqa: E402
from tokenfurnace.providers import AUTH_STYLES, PROTOCOLS        # noqa: E402


def burst(n: int, seed: int, lo: float = 1200.0, hi: float = 19200.0) -> list[float]:
    """带爆发与退避的锯齿曲线：猛冲、骤降、偶尔归零，而不是平滑正弦。"""
    rnd, out, v = random.Random(seed), [], 8600.0
    for _ in range(n):
        r = rnd.random()
        if r < 0.10:
            v = rnd.uniform(15600, hi)                  # 爆发冲刺
        elif r < 0.17:
            v = rnd.uniform(0, 2000)                    # 退避 / 等窗口
        elif r < 0.34:
            v = min(hi, v * rnd.uniform(1.18, 1.60))    # 加速
        else:
            v = max(lo, min(hi, v * rnd.uniform(0.50, 1.02)))   # 回落
        out.append(round(v, 1))
    return out


def build_demo() -> dict:
    now = time.time()
    spark = burst(40, 7)
    series_v = burst(30, 21)
    minute_v = burst(24, 33, hi=18600.0)
    points_5h = 47452.0

    return {
        "version": "0.1.0", "server_time": now, "uptime": 3820,
        "active_profile": "demo", "active_profile_name": "主力端点",
        "active_has_key": True,
        "config": {
            "version": 2, "active_profile": "demo", "ui": {"poll_ms": 1000},
            "profiles": [
                {"id": "demo", "name": "主力端点", "protocol": "anthropic",
                 "base_url": "https://relay.example.com", "headers": {},
                 "auth": {"source": "env", "value": "", "ref": "MY_LLM_API_KEY",
                          "style": "x-api-key", "has_key": True,
                          "masked": "sk-ant********9f2c"},
                 "models": ["model-large", "model-standard", "model-fast"],
                 "selected": [{"id": "model-large", "weight": 3, "points_per_1k": 0},
                              {"id": "model-fast", "weight": 1, "points_per_1k": 0}],
                 "limits": {"window_5h": 60000, "week": 600000, "unit": "points"},
                 "points_per_1k": 1.4, "note": ""},
                {"id": "gw", "name": "自建网关", "protocol": "openai-chat",
                 "base_url": "https://gateway.internal/v1", "headers": {},
                 "auth": {"source": "inline", "value": "sk-gw1********7ab3", "ref": "",
                          "style": "bearer", "has_key": True,
                          "masked": "sk-gw1********7ab3"},
                 "models": ["gpt-4o", "gpt-4o-mini"], "selected": [],
                 "limits": {"window_5h": 0, "week": 0, "unit": "usd"},
                 "points_per_1k": 0, "note": ""},
                {"id": "gm", "name": "Gemini 直连", "protocol": "gemini",
                 "base_url": "https://generativelanguage.googleapis.com", "headers": {},
                 "auth": {"source": "env", "value": "", "ref": "GEMINI_API_KEY",
                          "style": "x-goog-api-key", "has_key": False, "masked": ""},
                 "models": [], "selected": [],
                 "limits": {"window_5h": 0, "week": 0, "unit": "tokens"},
                 "points_per_1k": 0, "note": ""},
            ],
            "engine_defaults": {
                "mode": "prefill", "concurrency": 4, "input_chars": 380000,
                "max_tokens": 8, "reasoning_effort": "none", "cache_bust": True,
                "timeout": 180, "retry": 5, "safety_ratio": 0.97,
                "instant_window": 20,
                "max_points": 60000, "max_total_tokens": 0, "max_requests": 0,
                "duration_min": 60, "enforce_windows": True, "points_per_1k": 1.4,
                "loop": True},
        },
        "presets": {},
        "protocols": {k: {"label": v["label"], "short": v["short"],
                          "auth": v["auth"], "hint": v["hint"]}
                      for k, v in PROTOCOLS.items()},
        "auth_styles": {k: {"label": v["label"], "hint": v["hint"]}
                        for k, v in AUTH_STYLES.items()},
        "modes": MODE_LABELS,
        "windows": {"h5": 18000, "week": 604800},
        "totals": {"tokens": 42840000, "requests": 240},
        "live": {
            "status": "running", "run_id": "demo0a1b2c3d",
            "started": now - 1847, "ended": 0,
            "requests": 240, "ok": 232, "failed": 8,
            "prompt_tokens": 42765400, "completion_tokens": 74600, "cached_tokens": 0,
            "points": 59872.4, "rate_instant": spark[-1], "rate": 23180.0,
            "rate_peak": max(spark),
            "errors": {"rps": 6, "timeout": 2},
            "by_model": {"model-large": {"requests": 174, "tokens": 31020000,
                                         "points": 43428.0},
                         "model-fast": {"requests": 66, "tokens": 11720000,
                                        "points": 16440.4}},
            "wait_until": 0, "wait_reason": "", "stop_reason": "", "last_error": "",
            "per_minute": [{"t": int(now // 60 * 60 - (23 - i) * 60),
                            "tokens": int(minute_v[i] * 60)} for i in range(24)],
            "recent": [], "spark": spark,
            "series": [{"t": int(now // 10 * 10 - (29 - i) * 10),
                        "tokens": int(series_v[i] * 10)} for i in range(30)],
            "elapsed": 1847, "total_tokens": 42840000, "rps": 0.13, "running": True,
            "session_count": 2, "workers_in_use": 8, "max_total_workers": 32,
            "sessions": [
                {"sid": "demo0a1b2c3d", "name": "主力端点", "status": "running",
                 "running": True, "protocol": "anthropic",
                 "models": ["model-large", "model-fast"], "mode": "prefill",
                 "concurrency": 4, "rate_instant": 8420.0, "total_tokens": 28400000,
                 "ok": 158, "requests": 163, "elapsed": 1847, "wait_reason": ""},
                {"sid": "9c2f7e1a44b8", "name": "自建网关", "status": "waiting",
                 "running": True, "protocol": "openai-chat",
                 "models": ["gpt-4o"], "mode": "prefill",
                 "concurrency": 4, "rate_instant": 4060.0, "total_tokens": 14440000,
                 "ok": 74, "requests": 77, "elapsed": 1204,
                 "wait_reason": "5 小时额度已用满（58,210 / 60,000）"},
            ],
            "windows": {"tokens_5h": 33893979, "tokens_week": 33981036,
                        "points_5h": points_5h, "points_week": 47573.5,
                        "limit_5h": 60000, "limit_week": 600000,
                        "enforce": True, "coef_set": True},
        },
        "logs": [
            {"ts": now - 1847, "level": "info", "msg": "会话 demo0a1b2c3d 启动 · 2 个模型 · 并发 4 · 模式 预填充（大输入 + 极短输出）· 最快"},
            {"ts": now - 1840, "level": "info", "msg": "连接自检通过：Anthropic Messages · 发现 3 个模型 · 首包 0.42s"},
            {"ts": now - 1620, "level": "warn", "msg": "rps 触发退避 2s（全局）"},
            {"ts": now - 1618, "level": "info", "msg": "退避结束，恢复正常调度"},
            {"ts": now - 1180, "level": "warn", "msg": "timeout 触发退避 35s（全局）"},
            {"ts": now - 1145, "level": "info", "msg": "退避结束，恢复正常调度"},
            {"ts": now - 620, "level": "info", "msg": "已消耗 30,000,000 tokens · 约 42,000 积分 · 5h 窗口占用 70.1%"},
            {"ts": now - 300, "level": "warn", "msg": "5 小时额度已用满（58,210 / 60,000），等待 42.3 分钟后继续"},
            {"ts": now - 60, "level": "info", "msg": "缓存命中 0 tokens，击穿有效"},
            {"ts": now - 5, "level": "info", "msg": "已消耗 42,840,000 tokens · 约 59,872 积分 · 5h 窗口占用 71.3%"},
        ],
        "history": [
            {"id": "demo0a1b2c3d", "started": now - 1847, "ended": None,
             "profile": "demo", "profile_name": "主力端点",
             "models": "model-large,model-fast", "mode": "prefill", "concurrency": 4,
             "status": "running", "prompt_tokens": 42765400,
             "completion_tokens": 74600, "requests": 240, "failed": 8, "note": ""},
            {"id": "8f31c0a94e21", "started": now - 9600, "ended": now - 4200,
             "profile": "demo", "profile_name": "主力端点", "models": "model-large",
             "mode": "prefill", "concurrency": 4, "status": "done",
             "prompt_tokens": 61200000, "completion_tokens": 41000, "requests": 331,
             "failed": 3, "note": "到达积分上限 60,000"},
            {"id": "2b7e5d1180aa", "started": now - 50400, "ended": now - 46800,
             "profile": "gw", "profile_name": "自建网关", "models": "gpt-4o",
             "mode": "mixed", "concurrency": 6, "status": "done",
             "prompt_tokens": 18400000, "completion_tokens": 2400000, "requests": 96,
             "failed": 0, "note": "到达设定时长 60 分钟"},
            {"id": "c40a9f2b7e13", "started": now - 172800, "ended": now - 171900,
             "profile": "demo", "profile_name": "主力端点", "models": "model-fast",
             "mode": "prefill", "concurrency": 4, "status": "interrupted",
             "prompt_tokens": 4200000, "completion_tokens": 900, "requests": 23,
             "failed": 1, "note": "服务退出"},
        ],
    }


MOCK = """
<script>
/* 静态预览：拦截 fetch 返回演示数据。界面代码本身是真实文件，未做任何改动。 */
(function () {
  const DEMO = __DEMO__;
  let tick = 0;
  const reply = (o) => ({ ok: true, status: 200, json: async () => o });
  const jitter = (b, s) => Math.max(200, b * (1 + Math.sin(s * 2.7) * 0.45 + Math.sin(s * 11.3) * 0.28));
  window.fetch = async function (url) {
    const path = String(url).split('?')[0];
    if (path === '/api/state') {
      tick++;
      const d = JSON.parse(JSON.stringify(DEMO));
      const grow = tick * 5800;
      d.live.total_tokens += grow;
      d.live.prompt_tokens += grow;
      d.live.requests += Math.floor(tick / 2);
      d.live.ok += Math.floor(tick / 2);
      d.live.points = d.live.total_tokens / 1000 * 1.4;
      d.live.elapsed = DEMO.live.elapsed + tick;
      d.live.rate = d.live.total_tokens / d.live.elapsed;
      d.live.rate_instant = jitter(DEMO.live.rate_instant, tick);
      d.live.rate_peak = Math.max(d.live.rate_peak, d.live.rate_instant);
      d.live.spark = d.live.spark.slice(1).concat([Math.round(d.live.rate_instant)]);
      const lastT = d.live.series[d.live.series.length - 1].t;
      d.live.series = d.live.series.slice(1).concat([
        { t: lastT + 10, tokens: Math.round(d.live.rate_instant * 10) }]);
      d.live.windows.points_5h = d.live.total_tokens / 1000 * 1.4 * 0.79;
      return reply(d);
    }
    if (path === '/api/run/detail') {
      return reply({ ok: true, run: DEMO.history[0], buckets: DEMO.live.per_minute,
        models: [{ model: 'model-large', n: 174, p: 31020000, c: 41000 },
                 { model: 'model-fast', n: 66, p: 11720000, c: 33600 }],
        errors: [{ kind: 'rps', n: 6 }, { kind: 'timeout', n: 2 }] });
    }
    if (path === '/api/guess_protocol') return reply({ ok: true, protocol: 'anthropic' });
    return reply({ ok: true, result: {}, models: [] });
  };
  addEventListener('DOMContentLoaded', () => {
    const bar = document.createElement('div');
    bar.textContent = '静态预览 · 界面为真实文件渲染，数据为演示数据，不可交互';
    Object.assign(bar.style, {
      position: 'fixed', left: '50%', transform: 'translateX(-50%)', bottom: '16px',
      zIndex: 200, padding: '7px 16px', borderRadius: '999px',
      background: '#1f6feb22', border: '1px solid #1f6feb66', color: '#58a6ff',
      fontSize: '12px', backdropFilter: 'blur(6px)', pointerEvents: 'none' });
    document.body.appendChild(bar);
  });
})();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="构建自包含的界面静态预览")
    ap.add_argument("-o", "--out", default="docs/preview.html")
    a = ap.parse_args()

    web = ROOT / "tokenfurnace" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "style.css").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")

    demo = build_demo()
    html = html.replace('<link rel="stylesheet" href="/static/style.css">',
                        f"<style>\n{css}\n</style>")
    html = html.replace('<script src="/static/app.js"></script>',
                        MOCK.replace("__DEMO__", json.dumps(demo, ensure_ascii=False))
                        + f"<script>\n{js}\n</script>")

    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    sp = demo["live"]["spark"]
    print(f"已生成 {out}  ({out.stat().st_size / 1024:.0f} KB，自包含)")
    print(f"演示速率区间 {min(sp):,.0f} ~ {max(sp):,.0f} tok/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
