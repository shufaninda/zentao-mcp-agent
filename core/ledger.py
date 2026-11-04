"""记忆层账本 —— 跨运行的唯一真相（不可重建数据，bug_id 以此为准）。

三道幂等防线的载体（冻结）：
  ① write-ahead：claim_for_creation 抢占 state='creating'，调禅道成功才回写 created
  ② TTL 孤儿恢复：claim 条件含 attempt_at < now-10min，崩溃遗留的 creating 可被重新抢占
  ③ 禅道侧查重：finalize 在建单前再调 search_bugs（在 tools 层，不在这里）

一致性方向：State 可重建以文件为真相；本库存不可重建数据。
Week1 用 SQLite（本文件），Week2+ 换 MySQL 只改 connect()，schema 不变。

事务边界 = 单节点的记忆写入；事务里禁止调禅道 API（调用方负责）。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from core.config import settings

_SCHEMA_VERSION = 1
_local = threading.local()


def _connect() -> sqlite3.Connection:
    """每个线程一个连接。DSN 形如 sqlite:///path 或 mysql://...（MySQL week2 接）。"""
    dsn = settings.ledger_dsn
    if dsn.startswith("sqlite:///"):
        path = dsn[len("sqlite:///"):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")       # 并发读写友好
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    raise NotImplementedError(f"week1 仅支持 SQLite，收到: {dsn[:30]}...")


def db() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
        _init_schema(_local.conn)
    return _local.conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        run_id       TEXT PRIMARY KEY,
        task         TEXT NOT NULL,
        patches_hash TEXT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        total        INTEGER, passed INTEGER, failed INTEGER,
        report_path  TEXT
    );
    CREATE TABLE IF NOT EXISTS fingerprints (
        fp          TEXT PRIMARY KEY,          -- md5(module|method+path|exp!=act|err_sig)[:12]
        module      TEXT NOT NULL,
        signature   TEXT NOT NULL,             -- 可读错误特征（供 search_bugs 关键词）
        state       TEXT NOT NULL DEFAULT 'new',  -- new/creating/created/failed/recorded
        bug_id      INTEGER,
        count       INTEGER NOT NULL DEFAULT 1,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        last_run_id TEXT,
        attempt_id  TEXT,
        attempt_at  TEXT,
        note        TEXT
    );
    CREATE TABLE IF NOT EXISTS fingerprint_cases (
        fp       TEXT NOT NULL,
        case_id  TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        PRIMARY KEY (fp, case_id)
    );
    CREATE TABLE IF NOT EXISTS review_queue (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT NOT NULL,
        case_id    TEXT NOT NULL,
        category   TEXT NOT NULL,
        confidence REAL NOT NULL,
        reasoning  TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
        created_at TEXT NOT NULL
    );
    """)
    v = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if not v:
        conn.execute("INSERT INTO schema_version VALUES (?, ?)", (_SCHEMA_VERSION, _now()))
    conn.commit()


# ---------------------------------------------------------------- runs
def start_run(run_id: str, task: str, patches_hash: str = "") -> None:
    db().execute(
        "INSERT OR REPLACE INTO runs (run_id, task, patches_hash, started_at) VALUES (?,?,?,?)",
        (run_id, task, patches_hash, _now()),
    )
    db().commit()


def finish_run(run_id: str, total: int, passed: int, failed: int, report_path: str) -> None:
    db().execute(
        "UPDATE runs SET finished_at=?, total=?, passed=?, failed=?, report_path=? WHERE run_id=?",
        (_now(), total, passed, failed, report_path, run_id),
    )
    db().commit()


# ---------------------------------------------------------------- fingerprints
def upsert_fingerprint(fp: str, module: str, signature: str, case_id: str, run_id: str) -> dict:
    """登记/累加。同一 run 重放不重复 +1（用 last_run_id 判）。"""
    conn = db()
    row = conn.execute("SELECT * FROM fingerprints WHERE fp=?", (fp,)).fetchone()
    now = _now()
    if row is None:
        conn.execute(
            "INSERT INTO fingerprints (fp, module, signature, state, count, first_seen, last_seen, last_run_id)"
            " VALUES (?,?,?,'new',1,?,?,?)",
            (fp, module, signature, now, now, run_id),
        )
    elif row["last_run_id"] != run_id:
        conn.execute(
            "UPDATE fingerprints SET count=count+1, last_seen=?, last_run_id=? WHERE fp=?",
            (now, run_id, fp),
        )
    conn.execute(
        "INSERT OR IGNORE INTO fingerprint_cases (fp, case_id, first_seen) VALUES (?,?,?)",
        (fp, case_id, now),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM fingerprints WHERE fp=?", (fp,)).fetchone())


def get_fingerprint(fp: str) -> dict | None:
    row = db().execute("SELECT * FROM fingerprints WHERE fp=?", (fp,)).fetchone()
    return dict(row) if row else None


def open_fingerprints(module: str) -> list[dict]:
    """未关闭（created 之外）的同模块指纹 —— triage 证据项「无同类未关闭bug」用。"""
    rows = db().execute(
        "SELECT * FROM fingerprints WHERE module=? AND state NOT IN ('created','failed')",
        (module,),
    ).fetchall()
    return [dict(r) for r in rows]


def claim_for_creation(fp: str, attempt_id: str) -> bool:
    """write-ahead 抢占。三道幂等的第一道：乐观锁 + TTL 孤儿恢复。

    成功（rowcount=1）才允许调禅道；rowcount=0 说明：
      - 已 created → 调用方幂等返回
      - 别的进程 creating 且未超时 → 让出
    """
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    cur = db().execute(
        """UPDATE fingerprints SET state='creating', attempt_id=?, attempt_at=?
           WHERE fp=? AND (state IN ('new','failed','recorded')
                           OR (state='creating' AND attempt_at < ?))""",
        (attempt_id, _now(), fp, stale),
    )
    db().commit()
    return cur.rowcount == 1


def mark_created(fp: str, bug_id: int) -> None:
    db().execute(
        "UPDATE fingerprints SET state='created', bug_id=?, note=NULL WHERE fp=?",
        (bug_id, fp),
    )
    db().commit()


def mark_recorded(fp: str, note: str) -> None:
    """AUTO_CREATE_BUG=false：只记录不建单（功能开关路径）。"""
    db().execute("UPDATE fingerprints SET state='recorded', note=? WHERE fp=?", (note, fp))
    db().commit()


def mark_failed(fp: str, error: str) -> None:
    db().execute(
        "UPDATE fingerprints SET state='failed', note=? WHERE fp=?", (error[:500], fp)
    )
    db().commit()


# ---------------------------------------------------------------- review queue
def queue_review(run_id: str, case_id: str, category: str,
                 confidence: float, reasoning: str) -> int:
    """uncertain 进入人工复核队列。week2 只写不读（回流闭环是 TODO）。"""
    cur = db().execute(
        "INSERT INTO review_queue (run_id, case_id, category, confidence, reasoning, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, case_id, category, confidence, reasoning, _now()),
    )
    db().commit()
    return cur.lastrowid
