"""周期任务的两张表 —— 定义（`cron_jobs`）+ 台账（`cron_runs`）。

本模块只管 SQL：
"下一次什么时候跑"在 :mod:`core.cron.expr`，跑与投递在 :mod:`core.cron.runner`。

## 三条不许回退的判据

### ① 统计四格是派生的，不冗余在 `cron_jobs` 上

`last_run` / `run_count` / `last_status` / `last_error` 一律从 `cron_runs` 算。
判据：能推导的别存。（唯一必须存的是 `next_run` —— 退避状态只能落在它上面，
且它让"该不该跑"变成一次索引查询。）

### ② 认领必须是一个事务

"选中"与"置 `running=1`"之间只要有缝隙，两次 tick 就会把同一条任务各跑一遍。
:meth:`CronStore.claim_due` 用 `BEGIN IMMEDIATE`（先拿写锁再看），
不是"先 SELECT 再 UPDATE"。

### ③ 手动（测试）运行不许污染自动那本账

`cron_runs.trigger` 区分 `schedule` / `manual`；
`fail_streak` 只数 `schedule` —— 否则手点两下失败就能把一条好任务自动停掉。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

#: 默认库位置。落在 `data/runtime/` 下（模块自带的运行数据不许进源码目录）。
DEFAULT_DB_PATH = "data/runtime/cron/cron.db"

#: 僵尸认领的回收线（秒）。
#:
#: 判据是「它必须长于最长的一轮」（一轮可以跑十几分钟）。
#: 回收线太短，活着的运行被当成僵尸，同一条任务被跑两遍，
#: 而症状看起来像"任务自己重复了"。一轮可以更长，所以取 30 分钟。
STALE_CLAIM_SECONDS = 1800.0

#: 进度行（`:meth:`CronStore.progress`）最多读几行 —— 读它是为了数开头连续几个 error，
#: 20 行足够判断"连续失败"，不必扫全表。
_STREAK_SCAN = 20

#: "这一格别动"的哨兵。
#:
#: `finish()` 的 `next_run` 若用 `None` 同时表示"别动它"，
#: "把 next_run 清成空"这件事根本做不到（一次性任务跑完/错过之后该清空，
#: 而那些格子永远留着旧值）。症状很隐蔽：`enabled` 同时被置 0，调度器不会再碰它，
#: 所以行为看着是对的，只有台账和界面上那一格在说谎。
#:加这个哨兵：`None` = 显式写成空，不给 = 不动。
_UNSET = object()

TRIGGERS = ("schedule", "manual")
STATUSES = ("ok", "error", "missed", "cancelled")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class CronStore:
    """`cron_jobs` + `cron_runs` 的读写。线程安全（一把模块内锁 + 每个事务自带写锁）。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ---- 连接 ----

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """开一条连接，用完关掉。

        `with sqlite3.connect(...) as conn` 只提交事务、不关连接 ——
        照那样写，每调一次接口就漏一个文件句柄（而症状要到很久以后才现形）。
        """
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False,
                               isolation_level=None)      # 显式事务，见 claim_due
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    id          TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    title       TEXT NOT NULL DEFAULT '',
                    prompt      TEXT NOT NULL DEFAULT '',
                    rule_json   TEXT NOT NULL,
                    channel     TEXT NOT NULL DEFAULT '',
                    deliver     TEXT NOT NULL DEFAULT 'here',
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    next_run    TEXT,
                    running     INTEGER NOT NULL DEFAULT 0,
                    claimed_at  TEXT,
                    end_at      TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cron_jobs_due
                ON cron_jobs(enabled, running, next_run)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cron_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT NOT NULL,
                    trigger     TEXT NOT NULL DEFAULT 'schedule',
                    started_at  TEXT NOT NULL,
                    finished_at TEXT,
                    status      TEXT NOT NULL,
                    reason      TEXT NOT NULL DEFAULT '',
                    error       TEXT NOT NULL DEFAULT '',
                    storage_id  TEXT NOT NULL DEFAULT '',
                    -- ★★ 台账：**这一次跑了哪些工具**。
                    -- 判据见 `core/cron/runner.py::_collect_actions` ——
                    -- "它做了什么"是"到点时没人在场"那个洞唯一的补法。
                    actions     TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cron_runs_job
                ON cron_runs(job_id, id DESC)
            """)
            # 迁移：`CREATE TABLE IF NOT EXISTS` 加不上列 —— 库是早先建的
            #   就永远缺这一列，而症状是"记了但读不到"
            #   （读不到 != 0）。显式查一次 `PRAGMA` 再补。
            have = {r[1] for r in conn.execute("PRAGMA table_info(cron_runs)")}
            if "actions" not in have:
                conn.execute("ALTER TABLE cron_runs ADD COLUMN actions TEXT NOT NULL DEFAULT ''")

    # ---- jobs：写 ----

    def add(self, *, title: str, prompt: str, rule_json: str, channel: str = "",
            deliver: str = "here", next_run: Optional[str] = None,
            end_at: Optional[str] = None) -> str:
        """建一条任务，返回 id。上限不在这里判 —— 那是调用方（工具层）的活，
        因为"拒"要说得出话。"""
        jid = "job_%s" % uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO cron_jobs
                   (id, created_at, updated_at, title, prompt, rule_json,
                    channel, deliver, enabled, next_run, running, claimed_at, end_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?,0,NULL,?)""",
                (jid, now, now, title, prompt, rule_json, channel, deliver,
                 next_run, end_at))
        return jid

    def update(self, job_id: str, **fields: Any) -> bool:
        """改若干列（`enabled` / `next_run` / `running` / `claimed_at` / `title` …）。

        列名白名单：这是唯一一处把外部字符串拼进 SQL 的地方，必须挡住。
        """
        allowed = {"title", "prompt", "rule_json", "channel", "deliver",
                   "enabled", "next_run", "running", "claimed_at", "end_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError("不允许改这些列：%s" % sorted(bad))
        if not fields:
            return False
        sets = ", ".join("%s = ?" % k for k in fields)
        vals = list(fields.values()) + [_now_iso(), job_id]
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE cron_jobs SET %s, updated_at = ? WHERE id = ?" % sets, vals)
            return cur.rowcount > 0

    def delete(self, job_id: str) -> bool:
        """删任务。台账一起删 —— 留着无主的 `cron_runs` 只会让派生统计对不上账。"""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,)).rowcount
                conn.execute("DELETE FROM cron_runs WHERE job_id = ?", (job_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return n > 0

    # ---- jobs：读 ----

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cron_jobs ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock, self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0])

    # ---- 调度：认领 / 回收 / 结算 ----

    def claim_due(self, now: Optional[datetime] = None) -> list[dict]:
        """原子地认领所有到点且没在跑的任务（`running` 0 -> 1）。

        判据（见模块 docstring ②）：`BEGIN IMMEDIATE` 先拿写锁，再 SELECT + UPDATE
        在同一个事务里。两次 tick 不会选中同一条。
        """
        now = now or datetime.now().astimezone()
        now_iso = now.isoformat()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """SELECT * FROM cron_jobs
                       WHERE enabled = 1 AND running = 0
                         AND next_run IS NOT NULL AND next_run <= ?
                       ORDER BY next_run ASC""", (now_iso,)).fetchall()
                claimed = [dict(r) for r in rows]
                for job in claimed:
                    conn.execute(
                        "UPDATE cron_jobs SET running = 1, claimed_at = ?, updated_at = ?"
                        " WHERE id = ?", (now_iso, now_iso, job["id"]))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        for job in claimed:
            job["running"] = 1
            job["claimed_at"] = now_iso
        return claimed

    def due_jobs(self, now: Optional[datetime] = None) -> list[dict]:
        """只读：现在到点了、且没在跑、且启用的那些（按 next_run 升序）。

        与 :meth:`claim_due` 分开是故意的：要**先看
        "逾期了多久"再决定认不认领（超过宽限的不跑**、只记一笔）。认领那一步
        仍然交给 :meth:`claim_one` —— 它是原子的，两次 tick 不会重复认领。
        中间那道缝（读到这里、还没认领）是安全的：另一个 tick 先认领了，
        `claim_one` 会给 `None`，我们跳过它就是了。
        """
        now = now or datetime.now().astimezone()
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM cron_jobs
                   WHERE enabled = 1 AND running = 0
                     AND next_run IS NOT NULL AND next_run <= ?
                   ORDER BY next_run ASC""", (now.isoformat(),)).fetchall()
        return [dict(r) for r in rows]

    def claim_one(self, job_id: str) -> Optional[dict]:
        """手动（测试运行）占用同一把 `running` 锁。已在跑，`None`（不排队、不抢）。

        同一任务同时只允许一次运行。
        """
        now_iso = _now_iso()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM cron_jobs WHERE id = ? AND running = 0",
                    (job_id,)).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE cron_jobs SET running = 1, claimed_at = ?, updated_at = ?"
                    " WHERE id = ?", (now_iso, now_iso, job_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        out = dict(row)
        out["running"] = 1
        out["claimed_at"] = now_iso
        return out

    def reap_stale(self, now: Optional[datetime] = None,
                   stale_after: float = STALE_CLAIM_SECONDS) -> int:
        """把陈旧的认领放回去（进程死在半路 / 一轮崩了没收尾）。

        回收线要长于最长的一轮，否则活着的运行会被当成僵尸、同一任务跑两遍
        —— 理由写在 :data:`STALE_CLAIM_SECONDS`。
        """
        now = now or datetime.now().astimezone()
        cutoff = (now - timedelta(seconds=stale_after)).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """UPDATE cron_jobs SET running = 0, claimed_at = NULL, updated_at = ?
                   WHERE running = 1 AND (claimed_at IS NULL OR claimed_at < ?)""",
                (now.isoformat(), cutoff))
            return int(cur.rowcount or 0)

    def finish(self, job_id: str, *, status: str, trigger: str = "schedule",
               next_run: object = _UNSET, enabled: Optional[int] = None,
               reason: str = "", error: str = "", storage_id: str = "",
               actions: str = "",
               started_at: Optional[str] = None,
               finished_at: Optional[str] = None) -> None:
        """一次事务里写台账 + 放锁 + 推 `next_run`。

        两件事必须同生共死：台账写了一行、锁没放，这条任务再也不跑了
        （而且不会有任何东西红）。
        """
        if status not in STATUSES:
            raise ValueError("未知 status：%r" % (status,))
        if trigger not in TRIGGERS:
            raise ValueError("未知 trigger：%r" % (trigger,))
        started = started_at or _now_iso()
        finished = finished_at or _now_iso()

        sets = ["running = 0", "claimed_at = NULL", "updated_at = ?"]
        vals: list = [finished]
        if next_run is not _UNSET:
            sets.append("next_run = ?")
            vals.append(next_run)               # 可以是 None（= 显式清空）
        if enabled is not None:
            sets.append("enabled = ?")
            vals.append(int(enabled))
        vals.append(job_id)

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO cron_runs
                       (job_id, trigger, started_at, finished_at, status,
                        reason, error, storage_id, actions)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (job_id, trigger, started, finished, status,
                     reason, error[:2000], storage_id, actions[:4000]))
                conn.execute("UPDATE cron_jobs SET %s WHERE id = ?" % ", ".join(sets), vals)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ---- 台账：派生统计（判据 ①） ----

    def stats(self, job_id: str) -> dict:
        """从 `cron_runs` 派生的四格 + `fail_streak`。

        `fail_streak` 只数 `trigger='schedule'`（判据 ③）：
        手动测试失败不该把一条好任务自动停掉。
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          MAX(started_at) AS last_run
                   FROM cron_runs WHERE job_id = ?""", (job_id,)).fetchone()
            latest = conn.execute(
                """SELECT status, error, trigger, started_at, finished_at,
                          reason, storage_id, actions
                   FROM cron_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1""",
                (job_id,)).fetchone()
            sched = conn.execute(
                """SELECT status FROM cron_runs
                   WHERE job_id = ? AND trigger = 'schedule'
                   ORDER BY id DESC LIMIT ?""", (job_id, _STREAK_SCAN)).fetchall()

        streak = 0
        for r in sched:
            if r["status"] == "error":
                streak += 1
            else:
                break
        return {
            "run_count": int(row["n"] or 0),
            "last_run": row["last_run"],
            "last_status": latest["status"] if latest else None,
            "last_error": (latest["error"] if latest else "") or "",
            "last_trigger": latest["trigger"] if latest else None,
            "last_reason": (latest["reason"] if latest else "") or "",
            "last_actions_raw": (latest["actions"] if latest else "") or "",
            "fail_streak": streak,
        }

    def recent_runs(self, job_id: str, limit: int = 20) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM cron_runs WHERE job_id = ?
                   ORDER BY id DESC LIMIT ?""", (job_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_with_stats(self) -> list[dict]:
        """列表页要的形状：每条任务 + 它的派生统计。"""
        out = []
        for job in self.list_all():
            merged = dict(job)
            merged["stats"] = self.stats(job["id"])
            out.append(merged)
        return out


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------

_store: Optional[CronStore] = None
_store_lock = threading.Lock()


def get_cron_store() -> CronStore:
    """进程内单例。

    必须显式传 `DEFAULT_DB_PATH`，不能用它的默认参数值 ——
    `def __init__(self, db_path=DEFAULT_DB_PATH)` 的默认值是函数定义时求值的，
    改模块常量对它无效（测试想把它挪去临时目录时必须能生效）。
    """
    global _store
    with _store_lock:
        if _store is None:
            _store = CronStore(db_path=DEFAULT_DB_PATH)
        return _store


def reset_cron_store() -> None:
    """给测试用（也把"重新读盘"这件事收成一处）。"""
    global _store
    with _store_lock:
        _store = None
