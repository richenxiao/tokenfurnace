"""消费引擎：多会话调度、自适应限流、额度刹车、滚动窗口等待。

结构
----
* `Session` —— **一次消费**：一个配置 + 一份参数 + 一组 worker。
* `Engine`  —— **管理器**：同时跑多个 Session，汇总统计，共享限流窗口。

为什么可以多会话并发
--------------------
积分是**逐请求落库**的（见 store.py 的 points 列），滚动窗口本来就是全表求和，
所以多个会话同时跑不会互相污染记账——早先「只能跑一个」的限制其实站不住脚。
真正需要防的是线程数爆炸，因此有一个全局 worker 上限。

设计要点
--------
* **预填充优先**：实测同一模型下「大输入 + 极短输出」比「小输入 + 长输出」
  快约 80 倍（9,000~18,000 tok/s vs ~110 tok/s），所以默认模式是 prefill。
* **缓存击穿**：每次请求都重新生成随机文本，否则会走 prefix cache 被折扣计费。
* **自适应退避**：撞限流或超时时，让**该会话的所有线程**一起暂停再逐步恢复。
* **额度刹车**：积分 / token / 请求数 / 时长四个维度，任一触顶即停。
* **滚动窗口**：5 小时与 7 天两个滑动窗口，本地记账推算，撞墙就等到窗口滑出。
  窗口是**全局**的——任一会话撞墙，所有会话一起等，因为平台额度本来就是共享的。
"""

from __future__ import annotations

import fnmatch
import random
import string
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from .providers import Adapter, CallResult

WINDOW_5H = 5 * 3600
WINDOW_WEEK = 7 * 86400

DECODE_PROMPT = (
    "请从 1 开始连续输出整数，每个数字之间用一个空格分隔，一直输出到你无法继续为止。"
    "不要省略、不要总结、不要解释、不要换行、不要加任何标点。"
)

MODE_LABELS = {
    "prefill": "预填充（大输入 + 极短输出）· 最快",
    "decode": "生成（小输入 + 长输出）· 最慢",
    "mixed": "混合（预填充为主，穿插生成）",
}

# 即时速率的默认滑动窗口（秒），可在界面上实时切换
INSTANT_WIN = 20.0
# recent 明细保留时长，必须 >= 最大可选窗口
RECENT_KEEP = 300.0
# 速率迷你图固定画多少个桶
SPARK_BUCKETS = 40
# 所有会话加起来最多开多少 worker，防止线程爆炸
MAX_TOTAL_WORKERS = 32


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NULL_LOCK = _NullLock()


# ====================================================================== #
# 参数
# ====================================================================== #
@dataclass
class RunSpec:
    profile_id: str = ""
    models: list = field(default_factory=list)      # [{"id","weight","points_per_1k"}]
    mode: str = "prefill"
    concurrency: int = 4
    input_chars: int = 380000
    max_tokens: int = 8
    reasoning_effort: str = "none"
    cache_bust: bool = True
    timeout: float = 180
    retry: int = 5
    instant_window: float = INSTANT_WIN

    max_points: float = 0          # 0 = 不限
    max_total_tokens: float = 0
    max_requests: int = 0
    duration_min: float = 0

    enforce_windows: bool = False
    limit_5h: float = 60000
    limit_week: float = 600000
    points_per_1k: float = 0
    safety_ratio: float = 0.97
    loop: bool = False
    # 积分池。空列表时退化成上面那对 limit_5h / limit_week 的单池行为。
    # 每项：{"name": str, "models": [glob...], "limit_5h": float, "limit_week": float}
    pools: list = field(default_factory=list)

    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "RunSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# ====================================================================== #
# 实时统计
# ====================================================================== #
@dataclass
class Live:
    status: str = "idle"           # idle|running|waiting|stopping|done|error
    run_id: str = ""
    started: float = 0.0
    ended: float = 0.0
    requests: int = 0
    ok: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    points: float = 0.0
    errors: dict = field(default_factory=dict)
    by_model: dict = field(default_factory=dict)
    wait_until: float = 0.0
    wait_reason: str = ""
    stop_reason: str = ""
    last_error: str = ""
    per_minute: list = field(default_factory=list)   # [{"t","tokens"}]
    recent: list = field(default_factory=list)       # [[ts, tokens]]
    rate_peak: float = 0.0


# ====================================================================== #
# 速率计算（纯函数，方便单测）
# ====================================================================== #
def bump_minute(L: Live, now: float, tokens: int) -> None:
    b = int(now // 60) * 60
    if L.per_minute and L.per_minute[-1]["t"] == b:
        L.per_minute[-1]["tokens"] += tokens
    else:
        L.per_minute.append({"t": b, "tokens": tokens})
    if len(L.per_minute) > 600:
        del L.per_minute[:-600]


def bump_recent(L: Live, now: float, tokens: int, win: float = INSTANT_WIN) -> None:
    """记录最近若干秒的 token 明细，用来算即时速率和迷你图。"""
    L.recent.append([now, tokens])
    if len(L.recent) > 600:
        del L.recent[:-600]
    cut = now - RECENT_KEEP
    while L.recent and L.recent[0][0] < cut:
        L.recent.pop(0)
    inst = instant_rate(L, now, effective_window(L.recent, now, win))
    if inst > L.rate_peak:
        L.rate_peak = inst


def instant_rate(L: Live, now: float, win: float = INSTANT_WIN) -> float:
    """最近 win 秒的平均速率。没有新完成就自然衰减到 0。"""
    s = sum(t for ts, t in L.recent if now - ts <= win)
    return s / win


def effective_window(recent: list, now: float, base: float) -> float:
    """把用户选的窗口自动放宽到至少覆盖最近两次完成。

    为什么需要：预填充模式下单个请求要 20~40 秒才完成，窗口设成 5 秒时
    绝大多数帧里「一次完成都没有」，主数字就长期显示 0.0——
    工具明明在满载跑、读数却是 0。这个取舍不该让用户自己去算。

    窗口不够就放宽到「距倒数第二次完成的时间 × 1.2」，上限 RECENT_KEEP。

    前提：`recent` 按时间**正序**追加（真实运行时如此，从队尾裁、往队尾加）。
    """
    ts = [t for t, _ in recent if now - t <= RECENT_KEEP]
    if len(ts) < 2:
        return base
    return min(max(base, (now - ts[-2]) * 1.2), RECENT_KEEP)


def spark_series(L: Live, now: float, win: float = INSTANT_WIN,
                 buckets: int = SPARK_BUCKETS) -> list[float]:
    """折成固定长度的每秒速率序列，供迷你图使用。

    桶宽跟着窗口走（≈ 窗口/4），所以调短窗口时迷你图也一起变灵敏，
    整条图始终覆盖约 10 个窗口的长度。
    """
    bw = max(1, int(round(win / 4)))
    acc: dict[int, int] = {}
    for ts, t in L.recent:
        b = int(ts // bw) * bw
        acc[b] = acc.get(b, 0) + t
    now_b = int(now // bw) * bw
    return [round(acc.get(now_b - i * bw, 0) / bw, 1)
            for i in range(buckets - 1, -1, -1)]


def series_10s(recent: list, now: float, buckets: int = 30,
               bw: int = 10) -> list[dict]:
    """把 recent 折成 10 秒粒度的序列，比 per_minute 灵敏得多。"""
    acc: dict[int, int] = {}
    for ts, t in recent:
        b = int(ts // bw) * bw
        acc[b] = acc.get(b, 0) + t
    now_b = int(now // bw) * bw
    return [{"t": now_b - i * bw, "tokens": acc.get(now_b - i * bw, 0)}
            for i in range(buckets - 1, -1, -1)]


# ====================================================================== #
# 一次消费会话
# ====================================================================== #
def _match_any(model: str, patterns: list) -> bool:
    """模型是否命中某个池的任一 glob 模式。"""
    return any(fnmatch.fnmatch(model, p or "*") for p in patterns)


def pool_of(model: str, pools: list) -> int | None:
    """模型属于第几个池。**首个匹配生效**，所以池的顺序就是优先级。

    平台规则是「专属积分不足后再扣通用积分」，把专属池写在前面、
    兜底的通用池（`*`）写在后面，语义就对齐了。
    """
    for i, p in enumerate(pools):
        if _match_any(model, p.get("models") or ["*"]):
            return i
    return None


def pool_usage(by_5h: dict, by_week: dict, pools: list,
               safety_ratio: float) -> list:
    """按池汇总窗口占用。返回的每项可直接给界面用。

    模型先按 pool_of 归属到唯一一个池再累加，避免同时匹配多个模式时被重复计入。

    `rebate` 是可选字段：这个池每消耗 1 积分能返赠多少别的积分。
    平台常见的活动规则是「消耗 1 专属积分返 1 通用积分」，
    填 1 就能在界面上直接看到这一窗口挣了多少——用来核对平台到底给没给。
    """
    if not pools:
        return []
    assign = {}
    for m in set(by_5h) | set(by_week):
        i = pool_of(m, pools)
        if i is not None:
            assign[m] = i
    u5 = [0.0] * len(pools)
    uw = [0.0] * len(pools)
    for m, v in by_5h.items():
        if m in assign:
            u5[assign[m]] += v
    for m, v in by_week.items():
        if m in assign:
            uw[assign[m]] += v

    out = []
    for i, p in enumerate(pools):
        l5 = float(p.get("limit_5h") or 0)
        lw = float(p.get("limit_week") or 0)
        rate = float(p.get("rebate") or 0)
        blocked_5h = l5 > 0 and u5[i] >= l5 * safety_ratio
        blocked_week = lw > 0 and uw[i] >= lw * safety_ratio
        out.append({
            "name": p.get("name") or f"池 {i + 1}",
            "models": list(p.get("models") or ["*"]),
            "points_5h": round(u5[i], 1), "limit_5h": l5,
            "points_week": round(uw[i], 1), "limit_week": lw,
            "rebate": rate,
            "rebate_5h": round(u5[i] * rate, 1),
            "rebate_week": round(uw[i] * rate, 1),
            "blocked_5h": blocked_5h, "blocked_week": blocked_week,
            "blocked": blocked_5h or blocked_week,
        })
    return out


class Session:
    """一个配置 + 一份参数 = 一条独立的消费流水线。"""

    def __init__(self, sid: str, spec: RunSpec, profile: dict | None = None,
                 api_key: str = "", store=None, engine=None):
        self.sid = sid
        self.spec = spec
        self.profile = profile or {}
        self.api_key = api_key
        self.store = store
        self.engine = engine
        self.live = Live(run_id=sid)
        self.name = self.profile.get("name") or "未命名配置"
        self.protocol = self.profile.get("protocol", "openai-chat")
        self._stop = threading.Event()
        self._pause_until = 0.0
        self._strikes = 0
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._tls_store = threading.local()
        # 各积分池的窗口占用，由 _refresh_pools() 刷新
        self._pool_state: list = []
        self._pool_at = 0.0
        # 最近若干次成功请求的耗时，用来自适应调整超时
        self._lat = deque(maxlen=20)
        self._adapter = None

    # ------------------------------------------------------------ 日志
    def log(self, msg: str, level: str = "info") -> None:
        if self.engine:
            self.engine.log(msg, level, sid=self.sid, name=self.name)

    @property
    def _lock(self):
        return self.engine.lock if self.engine else _NULL_LOCK

    @property
    def _win(self) -> float:
        return self.engine.instant_window if self.engine else INSTANT_WIN

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._stop.clear()
        self._pause_until = 0.0
        self._strikes = 0
        self.live.status = "running"
        self.live.started = time.time()
        if self.store:
            self.store.create_run(
                self.profile.get("id", ""), self.profile.get("name", ""),
                [m["id"] for m in self.spec.models], self.spec.mode,
                self.spec.concurrency, run_id=self.sid)
        self._thread = threading.Thread(target=self._run, name=f"tf-{self.sid}",
                                        daemon=True)
        self._thread.start()

    def stop(self, reason: str = "手动停止") -> None:
        if not self.running:
            return
        self.live.stop_reason = reason
        # 在途请求杀不掉（urllib 阻塞在 recv 上，Python 也没法强杀线程），
        # 所以状态要如实显示「正在停止」，而不是假装已经停了
        self.live.status = "stopping"
        self._stop.set()
        self.log(f"收到停止指令：{reason}，等待在途请求结束", "warn")

    def wait(self, timeout: float = 300) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        spec = self.spec
        auth = self.profile.get("auth") or {}
        adapter = Adapter(
            self.profile.get("protocol", "openai-chat"),
            self.profile.get("base_url", ""),
            self.api_key,
            self.profile.get("headers"),
            spec.timeout,
            auth.get("style"),
        )
        self._pool = ThreadPoolExecutor(max_workers=spec.concurrency,
                                        thread_name_prefix=f"tf-{self.sid}")
        self._adapter = adapter
        status = "done"
        try:
            while not self._stop.is_set():
                stop = self._check_budgets()
                if stop:
                    self.live.stop_reason = stop
                    break
                if spec.enforce_windows and not self._gate():
                    if not spec.loop:
                        self.live.stop_reason = "滚动额度已用尽"
                        break
                    continue
                futures = [self._pool.submit(self._one, adapter, i)
                           for i in range(spec.concurrency)]
                for f in futures:
                    try:
                        f.result()
                    except Exception as e:      # 兜底，别让 worker 异常掀翻主循环
                        self.log(f"worker 异常：{type(e).__name__}: {e}", "error")
        except Exception as e:
            status = "error"
            self.live.stop_reason = f"引擎异常：{type(e).__name__}: {e}"
            self.log(self.live.stop_reason, "error")
        finally:
            if self._pool:
                self._pool.shutdown(wait=False)
            self.live.ended = time.time()
            self.live.status = "done" if status == "done" else "error"
            if self.store:
                self.store.finish_run(self.sid, self.live.status,
                                      self.live.stop_reason)
            s = self.live
            self.log(f"会话结束：{s.stop_reason or '正常结束'} · "
                     f"{s.requests} 请求 / {s.prompt_tokens + s.completion_tokens:,} tokens")

    # ------------------------------------------------------------ 单次请求
    def _tls(self) -> dict:
        """每线程私有状态：随机源 + 本线程的退避截止时间。"""
        d = getattr(self._tls_store, "d", None)
        if d is None:
            d = {"rnd": random.Random(time.time_ns() ^ threading.get_ident()),
                 "pause": 0.0}
            self._tls_store.d = d
        return d

    def _rnd_for_thread(self) -> random.Random:
        return self._tls()["rnd"]

    def _noise(self, n_chars: int) -> str:
        if not self.spec.cache_bust:
            return " ".join(["lorem"] * (n_chars // 6))
        rnd = self._rnd_for_thread()
        parts, total = [], 0
        while total < n_chars:
            w = "".join(rnd.choices(string.ascii_lowercase, k=rnd.randint(3, 9)))
            parts.append(w)
            total += len(w) + 1
        return " ".join(parts)

    def _pick_model(self) -> dict | None:
        """按权重抽一个模型，跳过所在积分池已经用满的那些。

        这一步是防「通用积分被吃掉」的关键。平台在专属积分不足时会**静默**
        改从通用池扣费，请求照样返回 200，工具察觉不到——唯一能防的办法
        就是在专属池用满之后不再向它发请求。
        """
        usable = self._usable_models()
        if not usable:
            return None
        total = sum(max(0, float(m.get("weight") or 1)) for m in usable) or 1.0
        x = self._rnd_for_thread().random() * total
        acc = 0.0
        for m in usable:
            acc += max(0, float(m.get("weight") or 1))
            if x <= acc:
                return m
        return usable[-1]

    def _usable_models(self) -> list:
        """勾选的模型里，所在池还没被限住的那部分。

        没配池、或池状态还没刷新过时，原样返回——不能因为拿不到池信息就一个都不发。
        """
        models = self.spec.models or []
        if not self._pool_state:
            return models
        blocked_pools = {i for i, p in enumerate(self._pool_state) if p["blocked"]}
        if not blocked_pools:
            return models
        pools = self.spec.pools or []
        keep = [m for m in models if pool_of(m.get("id") or "", pools) not in blocked_pools]
        return keep

    def _coef(self, model: dict) -> float:
        v = float(model.get("points_per_1k") or 0)
        return v if v > 0 else float(self.spec.points_per_1k or 0)

    def _one(self, adapter: Adapter, idx: int) -> None:
        spec = self.spec
        if self._stop.is_set():
            return
        self._wait_pause()
        if self._stop.is_set():
            return

        model = self._pick_model()
        if model is None:
            # 勾选的模型全在已用满的积分池里。主循环的闸门会去等窗口滑出，
            # 这里直接返回——硬发出去就是扣下一个池的积分。
            return
        mid = model["id"]
        # 混合模式：每 5 个请求穿插一次生成，让 token 构成更自然
        mode = spec.mode
        if mode == "mixed":
            mode = "decode" if (idx + int(time.time())) % 5 == 0 else "prefill"

        if mode == "decode":
            system, user = "", DECODE_PROMPT
            max_tokens = spec.max_tokens if spec.max_tokens > 64 else 4096
        else:
            system = self._noise(spec.input_chars)
            user = "回复两个字：收到"
            max_tokens = spec.max_tokens

        last: CallResult | None = None
        for attempt in range(max(1, spec.retry)):
            if self._stop.is_set():
                return
            res = adapter.chat(mid, system, user, max_tokens=max_tokens,
                               reasoning_effort=spec.reasoning_effort or None,
                               extra=spec.extra_body or None)
            last = res
            if res.ok:
                self._relax()
                self._record_ok(res, model)
                return
            if res.error_kind in ("rps", "server", "network", "timeout"):
                self._backoff(res.error_kind, attempt)
                continue
            break
        if last is not None:
            self._record_err(last, mid)

    def _tune_timeout(self, latency: float) -> None:
        """按实测延迟自适应收紧超时。

        固定 180 秒意味着一条挂住的连接要占住一个 worker 整整三分钟；
        实测正常耗时只有 25 秒左右时，这是纯浪费。
        取最近延迟的中位数 ×3，夹在 [30, 用户设定值] 之间。
        """
        self._lat.append(latency)
        if self._adapter is None or len(self._lat) < 3:
            return
        med = sorted(self._lat)[len(self._lat) // 2]
        self._adapter.timeout = max(30.0, min(float(self.spec.timeout), med * 3))

    # ------------------------------------------------------------ 记账
    def _record_ok(self, res: CallResult, model: dict) -> None:
        self._tune_timeout(res.latency)
        pt, ct = res.prompt_tokens, res.completion_tokens
        coef = self._coef(model)
        pts = (pt + ct) / 1000.0 * coef if coef > 0 else 0.0
        now = time.time()
        if self.store:
            self.store.add_request(self.sid, res.model or model["id"],
                                   pt, ct, res.cached_tokens, res.latency, now,
                                   points=pts)
        with self._lock:
            L = self.live
            L.requests += 1
            L.ok += 1
            L.prompt_tokens += pt
            L.completion_tokens += ct
            L.cached_tokens += res.cached_tokens
            L.points += pts
            b = L.by_model.setdefault(res.model or model["id"],
                                      {"requests": 0, "tokens": 0, "points": 0.0})
            b["requests"] += 1
            b["tokens"] += pt + ct
            b["points"] += pts
            bump_minute(L, now, pt + ct)
            bump_recent(L, now, pt + ct, self._win)

    def _record_err(self, res: CallResult, mid: str) -> None:
        if self.store:
            self.store.add_error(self.sid, mid, res.error_kind or "unknown",
                                 res.error_msg, res.latency)
        with self._lock:
            L = self.live
            L.requests += 1
            L.failed += 1
            L.errors[res.error_kind or "unknown"] = \
                L.errors.get(res.error_kind or "unknown", 0) + 1
            L.last_error = f"[{res.error_kind}] {res.error_msg}"[:300]
        if res.error_kind == "auth":
            self.live.stop_reason = "鉴权失败，请检查 API Key"
            self.log(f"鉴权失败：{res.error_msg}", "error")
            self._stop.set()
        elif res.error_kind == "quota":
            self._on_quota(res.error_msg)

    def _on_quota(self, msg: str) -> None:
        """额度耗尽：这是反推换算系数的黄金时机。"""
        used = self._window_tokens(WINDOW_5H)
        self.live.wait_reason = f"平台返回额度耗尽：{msg}"
        self.log(f"平台返回额度耗尽：{msg}", "error")
        if used > 0 and self.spec.limit_5h > 0:
            est = self.spec.limit_5h * 1000.0 / used
            self.log(f"校准建议：本 5 小时窗口消耗 {used:,} tokens，"
                     f"按上限 {self.spec.limit_5h:,.0f} 反推 points_per_1k ≈ {est:.4f}",
                     "warn")
        self.live.stop_reason = "额度耗尽"
        self._stop.set()

    # ------------------------------------------------------------ 限流
    def _wait_pause(self) -> None:
        """同时等全局暂停和本线程暂停。"""
        while not self._stop.is_set():
            left = max(self._pause_until, self._tls()["pause"]) - time.time()
            if left <= 0:
                return
            if self._stop.wait(min(left, 5.0)):
                return

    # rps（限流）和 server（服务端拥塞）是**全局**状况，本会话所有线程一起停；
    # timeout / network 通常只是某条连接卡住，别的连接还是好的——
    # 全局停会白白浪费并发额度，所以只让当前线程等。
    GLOBAL_BACKOFF = {"rps", "server"}
    BACKOFF_BASE = {"rps": 2.0, "server": 5.0, "timeout": 8.0, "network": 5.0}

    def _backoff(self, kind: str, attempt: int) -> None:
        base = self.BACKOFF_BASE.get(kind, 3.0)
        if kind in self.GLOBAL_BACKOFF:
            with self._lock:
                self._strikes += 1
                wait = min(base * self._strikes, 30.0) + attempt * 2.0
                self._pause_until = max(self._pause_until, time.time() + wait)
            self.log(f"{kind} 触发全局退避 {wait:.0f}s", "warn")
        else:
            wait = min(base * (attempt + 1), 30.0)
            self._tls()["pause"] = time.time() + wait
            self.log(f"{kind} 本线程退避 {wait:.0f}s（其余线程继续）", "warn")

    def _relax(self) -> None:
        with self._lock:
            if self._strikes:
                self._strikes -= 1

    # ------------------------------------------------------------ 滚动窗口
    def _window_tokens(self, seconds: float) -> int:
        return self.store.window_tokens(seconds) if self.store else 0

    def _window_points(self, seconds: float) -> float:
        """窗口积分直接取落库值，与当前模型选择和系数无关。"""
        return self.store.window_points(seconds) if self.store else 0.0

    def _refresh_pools(self, max_age: float = 2.0) -> None:
        """刷新各积分池的窗口占用。

        每个批次刷一次而不是每个请求刷一次：池状态本来就变得慢，
        并发 8 时每请求查一次库是纯浪费。max_age 让快照读取也不至于反复查库。
        """
        pools = self.spec.pools or []
        if not pools or not self.store:
            self._pool_state = []
            return
        now = time.time()
        if self._pool_state and now - self._pool_at < max_age:
            return
        by5 = self.store.window_points_by_model(WINDOW_5H, now)
        byw = self.store.window_points_by_model(WINDOW_WEEK, now)
        self._pool_state = pool_usage(by5, byw, pools, self.spec.safety_ratio)
        self._pool_at = now

    def _gate(self) -> bool:
        """滚动窗口闸门。返回 True 表示放行。

        窗口是全局的：任一会话把额度用满，所有会话一起等——平台额度本来就共享。

        配了积分池时按池判定。某个池用满**不等于**整个会话要停：只要还有别的池
        能用，就只避开被限的那个（_pick_model 负责跳过）。这很关键——平台在专属
        积分不足时会**静默**改从通用池扣费，请求照样 200，继续发就是白烧通用积分。
        """
        spec = self.spec
        self._refresh_pools()

        if self._pool_state:
            blocked = [p for p in self._pool_state if p["blocked"]]
            if blocked and not self._usable_models():
                names = "、".join(p["name"] for p in blocked)
                oldest = self.store.window_oldest_ts(WINDOW_5H) if self.store else None
                wait = max(30.0, (oldest or time.time()) + WINDOW_5H - time.time() + 5)
                self._wait_window(wait, f"积分池已用满：{names}")
                return not self._stop.is_set()
            return True

        if spec.limit_week <= 0 and spec.limit_5h <= 0:
            return True
        w = self.store.windows() if self.store else {}
        if spec.limit_week > 0:
            used = w.get("points_week", 0.0)
            if used >= spec.limit_week * spec.safety_ratio:
                self._wait_window(300.0,
                                  f"周额度已用满（{used:,.0f}/{spec.limit_week:,.0f}）")
                return not self._stop.is_set()
        if spec.limit_5h > 0:
            used = w.get("points_5h", 0.0)
            if used >= spec.limit_5h * spec.safety_ratio:
                oldest = self.store.window_oldest_ts(WINDOW_5H) if self.store else None
                wait = max(30.0, (oldest or time.time()) + WINDOW_5H - time.time() + 5)
                self._wait_window(wait,
                                  f"5 小时额度已用满（{used:,.0f}/{spec.limit_5h:,.0f}）")
                return not self._stop.is_set()
        return True

    def _wait_window(self, seconds: float, reason: str) -> None:
        self.live.status = "waiting"
        self.live.wait_reason = reason
        self.live.wait_until = time.time() + seconds
        self.log(f"{reason}，等待 {seconds / 60:.1f} 分钟后继续", "warn")
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            self._stop.wait(min(5.0, max(0.1, end - time.time())))
        self.live.status = "running"
        self.live.wait_reason = ""
        self.live.wait_until = 0.0

    # ------------------------------------------------------------ 刹车
    def _check_budgets(self) -> str:
        spec, L = self.spec, self.live
        if spec.duration_min and time.time() - L.started >= spec.duration_min * 60:
            return f"到达设定时长 {spec.duration_min:g} 分钟"
        total_tokens = L.prompt_tokens + L.completion_tokens
        if spec.max_requests and L.ok >= spec.max_requests:
            return f"到达请求数上限 {spec.max_requests}"
        if spec.max_total_tokens and total_tokens >= spec.max_total_tokens:
            return f"到达 token 上限 {spec.max_total_tokens:,.0f}"
        if spec.max_points and L.points >= spec.max_points:
            return f"到达积分上限 {spec.max_points:,.0f}"
        return ""

    # ------------------------------------------------------------ 快照
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            L = asdict(self.live)
            recent = list(self.live.recent)
        L.pop("recent", None)
        L["elapsed"] = (L["ended"] or now) - L["started"] if L["started"] else 0
        L["total_tokens"] = L["prompt_tokens"] + L["completion_tokens"]
        L["rate"] = L["total_tokens"] / L["elapsed"] if L["elapsed"] > 0 else 0
        L["rps"] = L["requests"] / L["elapsed"] if L["elapsed"] > 0 else 0
        L["running"] = self.running
        L["sid"] = self.sid
        L["name"] = self.name
        L["profile_id"] = self.profile.get("id", "")
        L["protocol"] = self.protocol
        L["models"] = [m.get("id", "") for m in self.spec.models]
        L["mode"] = self.spec.mode
        L["concurrency"] = self.spec.concurrency
        # 单请求实测耗时：和「本地配置」无关，直接反映服务端快慢。
        # 平均速率掉了但延迟没涨 → 是自己并发/额度的问题；
        # 延迟涨了 → 是服务端拥塞。
        L["avg_latency"] = round(sum(self._lat) / len(self._lat), 1) if self._lat else 0.0
        L["timeout_now"] = round(self._adapter.timeout, 1) if self._adapter else 0.0

        base = self._win
        eff = effective_window(recent, now, base)
        tmp = Live(recent=recent)
        L["rate_instant"] = round(instant_rate(tmp, now, eff), 1)
        L["instant_window"] = base
        L["effective_window"] = round(eff, 1)
        # 窗口被自动放宽时前端要标出来，否则用户会以为自己的设置没生效
        L["window_auto"] = eff > base * 1.05
        L["spark"] = spark_series(tmp, now, eff)
        L["series"] = series_10s(recent, now)
        return L


# ====================================================================== #
# 管理器：同时跑多个会话
# ====================================================================== #
class Engine:
    def __init__(self, store, log_fn=None, max_total_workers: int = MAX_TOTAL_WORKERS):
        self.store = store
        self._log_fn = log_fn or (lambda m: None)
        self.lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._instant_win = INSTANT_WIN
        self._win_ctx: dict = {"limit_5h": 0.0, "limit_week": 0.0,
                               "enforce": False, "coef": 0.0,
                               "pools": [], "safety_ratio": 0.97}
        self._idle_pools_cache: tuple = ([], 0.0)
        self._logs: list[dict] = []
        self.max_total_workers = max_total_workers

    # ------------------------------------------------------------ 设置
    @property
    def instant_window(self) -> float:
        return self._instant_win

    def set_instant_window(self, seconds: float) -> float:
        """调整即时速率的统计窗口（秒），立即生效。"""
        self._instant_win = max(2.0, min(RECENT_KEEP, float(seconds or INSTANT_WIN)))
        return self._instant_win

    def set_window_context(self, limit_5h: float, limit_week: float,
                           enforce: bool, coef: float,
                           pools: list | None = None,
                           safety_ratio: float = 0.97) -> None:
        """由服务层喂入当前 profile 的额度设置，供未运行时的窗口展示使用。"""
        self._win_ctx = {"limit_5h": float(limit_5h or 0),
                         "limit_week": float(limit_week or 0),
                         "enforce": bool(enforce), "coef": float(coef or 0),
                         "pools": list(pools or []),
                         "safety_ratio": float(safety_ratio or 0.97)}

    # ------------------------------------------------------------ 日志
    def log(self, msg: str, level: str = "info", sid: str = "", name: str = "") -> None:
        entry = {"ts": time.time(), "level": level, "msg": msg,
                 "sid": sid, "name": name}
        with self.lock:
            self._logs.append(entry)
            if len(self._logs) > 400:
                del self._logs[:-400]
        self._log_fn(msg)

    def recent_logs(self, n: int = 120) -> list[dict]:
        with self.lock:
            return list(self._logs[-n:])

    # ------------------------------------------------------------ 会话
    @property
    def running(self) -> bool:
        return any(s.running for s in self.sessions())

    def sessions(self) -> list[Session]:
        with self.lock:
            return list(self._sessions.values())

    def workers_in_use(self) -> int:
        return sum(s.spec.concurrency for s in self.sessions() if s.running)

    def start(self, spec: RunSpec, profile: dict, api_key: str) -> dict:
        if not profile.get("base_url"):
            return {"ok": False, "error": "Base URL 为空。"}
        models = [m for m in spec.models if m.get("id")]
        if not models:
            return {"ok": False, "error": "没有选择任何模型。"}
        spec.concurrency = max(1, min(int(spec.concurrency or 1), 64))
        spec.input_chars = max(1000, int(spec.input_chars or 1000))

        with self.lock:
            self._reap()
            used = self.workers_in_use()
            if used + spec.concurrency > self.max_total_workers:
                return {"ok": False,
                        "error": f"并发额度不足：已用 {used}，本次要 {spec.concurrency}，"
                                 f"上限 {self.max_total_workers}。"
                                 f"先停掉一些会话，或把并发调低。"}
            sid = uuid.uuid4().hex[:12]
            sess = Session(sid, spec, profile, api_key, self.store, self)
            self._sessions[sid] = sess
        self.set_instant_window(spec.instant_window)
        sess.start()
        self.log(f"会话启动 · {sess.name} · {len(models)} 个模型 · "
                 f"并发 {spec.concurrency} · 模式 {MODE_LABELS.get(spec.mode, spec.mode)}",
                 sid=sid, name=sess.name)
        return {"ok": True, "run_id": sid, "sid": sid}

    def stop(self, sid: str | None = None, reason: str = "手动停止") -> None:
        targets = self.sessions() if not sid else [
            s for s in self.sessions() if s.sid == sid]
        for s in targets:
            s.stop(reason)

    def stop_all(self, reason: str = "服务退出", grace: float = 30.0) -> None:
        """停掉全部会话，并共享一个等待截止时间——不能每个会话各等 30 秒。"""
        sess = self.sessions()
        for s in sess:
            s.stop(reason)
        deadline = time.time() + grace
        for s in sess:
            s.wait(timeout=max(0.0, deadline - time.time()))

    def _reap(self) -> None:
        """丢掉已经结束一段时间的会话，避免越积越多。"""
        with self.lock:
            for sid, s in list(self._sessions.items()):
                if not s.running and s.live.ended and time.time() - s.live.ended > 120:
                    del self._sessions[sid]

    # ------------------------------------------------------------ 汇总
    def snapshot(self) -> dict:
        now = time.time()
        sess = self.sessions()
        parts = [s.snapshot(now) for s in sess]
        agg = self._aggregate(parts)
        agg["sessions"] = parts
        agg["session_count"] = len(parts)
        agg["workers_in_use"] = self.workers_in_use()
        agg["max_total_workers"] = self.max_total_workers

        l5 = self._win_ctx["limit_5h"]
        lw = self._win_ctx["limit_week"]
        enforce = self._win_ctx["enforce"]
        coef = self._win_ctx["coef"]
        run_sess = next((s for s in sess if s.running), None)
        if run_sess:
            l5, lw = run_sess.spec.limit_5h, run_sess.spec.limit_week
            enforce = run_sess.spec.enforce_windows
            coef = float(run_sess.spec.points_per_1k or 0) or coef
        coef_set = coef > 0 or any(
            float(m.get("points_per_1k") or 0) > 0
            for s in sess for m in s.spec.models)
        if l5 or lw or enforce or self._win_ctx["pools"]:
            w = self.store.windows(now) if self.store else {}
            pools = (run_sess._pool_state if (run_sess and run_sess._pool_state)
                     else self._idle_pools(now))
            agg["windows"] = {
                "tokens_5h": w.get("tokens_5h", 0),
                "tokens_week": w.get("tokens_week", 0),
                "points_5h": w.get("points_5h", 0.0),
                "points_week": w.get("points_week", 0.0),
                "limit_5h": l5, "limit_week": lw,
                "enforce": enforce, "coef_set": coef_set,
                "pools": pools,
            }
        return agg

    def _idle_pools(self, now: float) -> list:
        """没有会话在跑时，按当前配置的池现算一次占用，供界面显示。

        带 2 秒缓存：快照每秒被拉一次，不缓存的话就是每秒一次全表分组查询。
        """
        pools = self._win_ctx["pools"]
        if not pools or not self.store:
            return []
        cached, at = self._idle_pools_cache
        if cached and now - at < 2.0:
            return cached
        by5 = self.store.window_points_by_model(WINDOW_5H, now)
        byw = self.store.window_points_by_model(WINDOW_WEEK, now)
        out = pool_usage(by5, byw, pools, self._win_ctx["safety_ratio"])
        self._idle_pools_cache = (out, now)
        return out

    def _aggregate(self, parts: list[dict]) -> dict:
        """把所有会话的实时指标加总成一份，给看板顶部用。"""
        if not parts:
            return {"status": "idle", "running": False, "run_id": "",
                    "requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "cached_tokens": 0, "points": 0.0,
                    "errors": {}, "by_model": {}, "per_minute": [], "spark": [],
                    "series": [], "rate": 0, "rate_instant": 0, "rate_peak": 0,
                    "rps": 0, "elapsed": 0, "total_tokens": 0,
                    "wait_until": 0, "wait_reason": "", "stop_reason": "",
                    "last_error": "", "instant_window": self._instant_win,
                    "effective_window": self._instant_win, "window_auto": False}
        run = [p for p in parts if p["running"]]
        stopping = bool(run) and all(p["status"] == "stopping" for p in run)
        agg = {
            "status": "stopping" if stopping else ("running" if run else "done"),
            "running": bool(run),
            "run_id": run[0]["sid"] if run else parts[0]["sid"],
            "requests": sum(p["requests"] for p in parts),
            "ok": sum(p["ok"] for p in parts),
            "failed": sum(p["failed"] for p in parts),
            "prompt_tokens": sum(p["prompt_tokens"] for p in parts),
            "completion_tokens": sum(p["completion_tokens"] for p in parts),
            "cached_tokens": sum(p["cached_tokens"] for p in parts),
            "points": sum(p["points"] for p in parts),
            "rate_instant": round(sum(p["rate_instant"] for p in parts), 1),
            "rate_peak": round(sum(p["rate_peak"] for p in parts), 1),
            "elapsed": max((p["elapsed"] for p in parts), default=0),
            "errors": {}, "by_model": {},
            "wait_until": max((p["wait_until"] for p in parts), default=0),
            "wait_reason": next((p["wait_reason"] for p in parts if p["wait_reason"]), ""),
            "stop_reason": next((p["stop_reason"] for p in run), ""),
            "last_error": next((p["last_error"] for p in parts if p["last_error"]), ""),
            "instant_window": self._instant_win,
            "effective_window": max((p.get("effective_window", self._instant_win)
                                     for p in parts), default=self._instant_win),
            "window_auto": any(p.get("window_auto") for p in parts),
        }
        agg["total_tokens"] = agg["prompt_tokens"] + agg["completion_tokens"]
        agg["rate"] = agg["total_tokens"] / agg["elapsed"] if agg["elapsed"] > 0 else 0
        agg["rps"] = agg["requests"] / agg["elapsed"] if agg["elapsed"] > 0 else 0
        for p in parts:
            for k, v in p["errors"].items():
                agg["errors"][k] = agg["errors"].get(k, 0) + v
            for k, v in p["by_model"].items():
                b = agg["by_model"].setdefault(k, {"requests": 0, "tokens": 0, "points": 0.0})
                b["requests"] += v["requests"]
                b["tokens"] += v["tokens"]
                b["points"] += v["points"]
        # 迷你图与序列按位置逐点相加
        n = max((len(p["spark"]) for p in parts), default=0)
        if n:
            agg["spark"] = [round(sum((p["spark"][i] if i < len(p["spark"]) else 0)
                                      for p in parts), 1) for i in range(n)]
        s0 = max(parts, key=lambda p: len(p["series"]))
        agg["series"] = [{"t": pt["t"],
                          "tokens": sum(p["series"][i]["tokens"]
                                        for p in parts if i < len(p["series"]))}
                         for i, pt in enumerate(s0["series"])]
        mins: dict[int, int] = {}
        for p in parts:
            for m in p["per_minute"]:
                mins[m["t"]] = mins.get(m["t"], 0) + m["tokens"]
        agg["per_minute"] = [{"t": t, "tokens": v} for t, v in sorted(mins.items())]
        return agg


# ====================================================================== #
def estimate_points(tokens: int, coef: float) -> float:
    return tokens / 1000.0 * coef if coef > 0 else 0.0


def char_to_token_hint(chars: int) -> int:
    """随机英文词串的经验换算：约 2.07 字符 / token。"""
    return int(chars / 2.07)
