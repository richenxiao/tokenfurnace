"""持久化记账层：SQLite 存储每次请求与每个运行会话。

用 SQLite（标准库自带）而不是日志文件，是为了：
  * 滚动窗口统计可以走索引，几十万条记录也不慢
  * 历史会话、导出、按模型/按小时聚合都是一句 SQL
  * 断电/强杀不会写坏（WAL + 逐条提交）
"""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
import uuid
from pathlib import Path

WINDOW_5H = 5 * 3600
WINDOW_WEEK = 7 * 86400
# 明细保留天数。周窗口只需要 7 天，留 30 天足够排查，也能让库不无限膨胀。
DEFAULT_RETENTION_DAYS = 30

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT    NOT NULL,
    ts                REAL    NOT NULL,
    model             TEXT    NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    latency           REAL    NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 1,
    error_kind        TEXT    DEFAULT '',
    error_msg         TEXT    DEFAULT '',
    points            REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_req_ts    ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_run   ON requests(run_id);
CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);

-- 覆盖索引：窗口统计只碰索引、不回表。20 万行下把周窗口查询从 40ms 降到 ~32ms。
CREATE INDEX IF NOT EXISTS idx_req_agg
    ON requests(ts, ok, prompt_tokens, completion_tokens, points);

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    started           REAL NOT NULL,
    ended             REAL,
    profile           TEXT,
    profile_name      TEXT,
    models            TEXT,
    mode              TEXT,
    concurrency       INTEGER,
    status            TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    requests          INTEGER DEFAULT 0,
    failed            INTEGER DEFAULT 0,
    note              TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
"""


class Store:
    """线程安全的记账库。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrated = self._migrate()
            self._conn.commit()
        # 写入计数器：totals 与窗口聚合都只在写发生后需要失效
        self._write_seq = 0
        self._totals_cache: tuple[int, dict] = (-1, {})
        # (写入序号, 计算时刻, 上次耗时ms, 结果)
        self._win_cache: tuple[int, float, float, dict] = (-1, 0.0, 0.0, {})

    def _migrate(self) -> bool:
        """老库补列。积分必须在请求发生时就算好并落库，
        否则事后改换算系数会把历史窗口的用量一起改掉。

        返回 True 表示这次确实补了列（调用方据此决定要不要回填历史数据）。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
        if "points" not in cols:
            self._conn.execute(
                "ALTER TABLE requests ADD COLUMN points REAL NOT NULL DEFAULT 0")
            return True
        return False

    def backfill_points(self, coef: float) -> int:
        """升级后的一次性回填：老记录没有 points，用当前系数补一个近似值。

        这是近似——历史请求当时用的是哪个系数已经不可考。只在补列那一次执行。
        """
        if coef <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET points=(prompt_tokens+completion_tokens)/1000.0*? "
                "WHERE points=0 AND ok=1 AND (prompt_tokens+completion_tokens)>0", (coef,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def create_run(self, profile: str, profile_name: str, models: list[str],
                   mode: str, concurrency: int, run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(id,started,profile,profile_name,models,mode,"
                "concurrency,status) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, time.time(), profile, profile_name,
                 ",".join(models), mode, concurrency, "running"))
            self._conn.commit()
        return run_id

    def add_request(self, run_id: str, model: str, pt: int, ct: int, cached: int,
                    latency: float, ts: float | None = None, points: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests(run_id,ts,model,prompt_tokens,completion_tokens,"
                "cached_tokens,latency,ok,points) VALUES(?,?,?,?,?,?,?,1,?)",
                (run_id, ts or time.time(), model, pt, ct, cached, latency, points))
            self._conn.execute(
                "UPDATE runs SET prompt_tokens=prompt_tokens+?,"
                "completion_tokens=completion_tokens+?,requests=requests+1 WHERE id=?",
                (pt, ct, run_id))
            self._conn.commit()
            self._write_seq += 1

    def add_error(self, run_id: str, model: str, kind: str, msg: str,
                  latency: float = 0.0, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests(run_id,ts,model,latency,ok,error_kind,error_msg)"
                " VALUES(?,?,?,?,0,?,?)",
                (run_id, ts or time.time(), model, latency, kind, msg[:500]))
            self._conn.execute(
                "UPDATE runs SET failed=failed+1,requests=requests+1 WHERE id=?", (run_id,))
            self._conn.commit()
            self._write_seq += 1

    def finish_run(self, run_id: str, status: str, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET ended=?,status=?,note=? WHERE id=?",
                (time.time(), status, note, run_id))
            self._conn.commit()

    def mark_stale_runs(self) -> None:
        """进程重启后，把上次没收尾的 running 会话标记为 interrupted。"""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status='interrupted', ended=? WHERE status='running'",
                (time.time(),))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def window_tokens(self, seconds: float, now: float | None = None,
                      model: str | None = None) -> int:
        now = now or time.time()
        sql = ("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t "
               "FROM requests WHERE ok=1 AND ts > ?")
        args: list = [now - seconds]
        if model:
            sql += " AND model = ?"
            args.append(model)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()["t"])

    def window_oldest_ts(self, seconds: float, now: float | None = None) -> float | None:
        """滚动窗口内最早一条记录的时间戳，用来算还要等多久。"""
        now = now or time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts) AS m FROM requests WHERE ok=1 AND ts > ?",
                (now - seconds,)).fetchone()
        return float(row["m"]) if row and row["m"] else None

    def window_points(self, seconds: float, now: float | None = None) -> float:
        """滚动窗口内的积分消耗。

        直接累加每条请求**发生时**落库的 points，因此：
          * 与当前勾选了哪些模型无关（换模型不会让历史用量消失）
          * 与当前换算系数无关（改系数不会篡改历史窗口）
        """
        now = now or time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(points),0) AS p FROM requests "
                "WHERE ok=1 AND ts > ?", (now - seconds,)).fetchone()
        return float(row["p"])

    def windows(self, now: float | None = None) -> dict:
        """一次查询取回 5 小时 + 周两个窗口的 tokens 与积分。

        两层优化：
          1. 原来拆成 4 条 SQL，每条自己扫一遍；合并成一条条件聚合只扫一遍。
          2. 配合 idx_req_agg 覆盖索引 + 结果缓存。

        缓存 TTL **随查询耗时自适应**：小库查询几乎不要钱，TTL 压到 1 秒（始终新鲜）；
        大库查询贵，TTL 放宽到最多 10 秒，用一点新鲜度换 CPU。
        另外只要有新写入就立刻失效，所以跑起来的时候窗口依然是实时的。
        """
        now = now or time.time()
        with self._lock:
            seq, at, cost, cached = self._win_cache
            if seq == self._write_seq and (now - at) < max(1.0, min(10.0, cost / 4.0)):
                return dict(cached)
            t0 = time.perf_counter()
            r = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN ts > ? THEN points ELSE 0 END),0) AS p5, "
                "COALESCE(SUM(points),0) AS pw, "
                "COALESCE(SUM(CASE WHEN ts > ? THEN prompt_tokens+completion_tokens "
                "                  ELSE 0 END),0) AS t5, "
                "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tw "
                "FROM requests WHERE ok=1 AND ts > ?",
                (now - WINDOW_5H, now - WINDOW_5H, now - WINDOW_WEEK)).fetchone()
            cost = (time.perf_counter() - t0) * 1000.0
            out = {"points_5h": float(r["p5"]), "points_week": float(r["pw"]),
                   "tokens_5h": int(r["t5"]), "tokens_week": int(r["tw"])}
            self._win_cache = (self._write_seq, now, cost, out)
            return dict(out)

    def prune(self, days: int = DEFAULT_RETENTION_DAYS) -> int:
        """删掉超过保留期的明细。周窗口只需要 7 天，不清理库会无限膨胀、
        窗口查询也会越来越慢。返回删除行数。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            n = cur.rowcount or 0
            if n:
                self._conn.commit()
                self._write_seq += 1
            return n

    def optimize(self) -> None:
        """让查询规划器用上最新的统计信息。"""
        with self._lock:
            self._conn.execute("ANALYZE")
            self._conn.commit()

    def run_stats(self, run_id: str) -> dict:
        with self._lock:
            r = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else {}

    def run_buckets(self, run_id: str, bucket: int = 60) -> list[dict]:
        """按时间桶聚合 token 量，用于画图。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT CAST(ts/? AS INTEGER)*? AS b, "
                "SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c "
                "FROM requests WHERE run_id=? AND ok=1 GROUP BY b ORDER BY b",
                (bucket, bucket, run_id)).fetchall()
        return [{"t": int(r["b"]), "prompt": int(r["p"] or 0),
                 "completion": int(r["c"] or 0)} for r in rows]

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def error_breakdown(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT error_kind AS kind, COUNT(*) AS n FROM requests "
                "WHERE run_id=? AND ok=0 GROUP BY error_kind ORDER BY n DESC",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def model_breakdown(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, COUNT(*) AS n, SUM(prompt_tokens) AS p, "
                "SUM(completion_tokens) AS c FROM requests "
                "WHERE run_id=? AND ok=1 GROUP BY model ORDER BY (SUM(prompt_tokens)+"
                "SUM(completion_tokens)) DESC", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def totals(self) -> dict:
        """累计量。它只增不减，所以只要没有新写入，缓存就一定有效——
        前端每秒轮询一次，缓存把这条全表聚合从「每秒一次」降到「有写入时一次」。"""
        with self._lock:
            seq, cached = self._totals_cache
            if seq == self._write_seq:
                return dict(cached)
            r = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t, "
                "COUNT(*) AS n FROM requests WHERE ok=1").fetchone()
            out = {"tokens": int(r["t"]), "requests": int(r["n"])}
            self._totals_cache = (self._write_seq, out)
            return dict(out)

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def export_requests_csv(self, run_id: str | None = None) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "会话", "模型", "输入tokens", "输出tokens",
                    "缓存tokens", "耗时秒", "成功", "错误类型"])
        sql = ("SELECT * FROM requests" +
               (" WHERE run_id=?" if run_id else "") + " ORDER BY ts")
        with self._lock:
            rows = self._conn.execute(sql, (run_id,) if run_id else ()).fetchall()
        for r in rows:
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                r["run_id"], r["model"], r["prompt_tokens"], r["completion_tokens"],
                r["cached_tokens"], f"{r['latency']:.2f}",
                "是" if r["ok"] else "否", r["error_kind"] or "",
            ])
        return buf.getvalue()

    def export_runs_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["会话", "开始", "结束", "配置", "模型", "模式", "并发", "状态",
                    "输入tokens", "输出tokens", "请求数", "失败数", "备注"])
        for r in reversed(self.history(10000)):
            w.writerow([
                r["id"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"])),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ended"]))
                if r["ended"] else "",
                r["profile_name"] or r["profile"] or "", r["models"] or "",
                r["mode"] or "", r["concurrency"] or "", r["status"] or "",
                r["prompt_tokens"], r["completion_tokens"],
                r["requests"], r["failed"], r["note"] or "",
            ])
        return buf.getvalue()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
