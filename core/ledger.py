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


class _MySQLAdapter:
    """把 pymysql 包装成 sqlite3.Connection 的最小接口，让上层逻辑只写一份 SQL。

    差异处理：? → %s 占位符、executescript 拆句、DictCursor 返回 dict、
    启动断言 READ COMMITTED + UTC（设计冻结项）、断线自动重连。
    """

    def __init__(self, dsn: str):
        import pymysql
        u = urlparse(dsn)
        self.conn = pymysql.connect(
            host=u.hostname or "127.0.0.1", port=u.port or 3306,
            user=u.username or "", password=u.password or "",
            database=u.path.lstrip("/") or "zentao_memory",
            charset="utf8mb4", autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with self.conn.cursor() as c:
            c.execute("SET time_zone = '+00:00'")
            c.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        self.conn.commit()

    def execute(self, sql: str, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        self._last = cur
        return cur

    def executescript(self, script: str):
        # 去掉行首/行内 -- 注释，否则 MySQL 会把后半句吃掉
        lines = [l.split("--")[0] for l in script.splitlines()]
        for stmt in [x.strip() for x in "\n".join(lines).split(";") if x.strip()]:
            self.execute(stmt)
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def ping(self):
        self.conn.ping(reconnect=True)


def is_mysql() -> bool:
    return settings.ledger_dsn.startswith("mysql://")


def _connect():
    """每个线程一个连接。SQLite（默认）或 MySQL，schema 完全一致。"""
    dsn = settings.ledger_dsn
    if dsn.startswith("sqlite:///"):
        path = dsn[len("sqlite:///"):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")       # 并发读写友好
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    if is_mysql():
        return _MySQLAdapter(dsn)
    raise NotImplementedError(f"不支持的 LEDGER_DSN: {dsn[:30]}...")


def db():
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
        _init_schema(_local.conn)
    elif is_mysql():
        _local.conn.ping()   # 长连接断线自愈
    return _local.conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ddl() -> str:
    """兼容两种后端的 DDL。MySQL 需要定长主键 + AUTO_INCREMENT。"""
    auto = "AUTO_INCREMENT" if is_mysql() else "AUTOINCREMENT"
    return f"""
    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        run_id       VARCHAR(64) PRIMARY KEY,
        task         TEXT NOT NULL,
        patches_hash TEXT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        total        INTEGER, passed INTEGER, failed INTEGER,
        report_path  TEXT
    );
    CREATE TABLE IF NOT EXISTS fingerprints (
        fp          VARCHAR(32) PRIMARY KEY,          -- md5(module|method+path|exp!=act|err_sig)[:12]
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
        fp       VARCHAR(32) NOT NULL,
        case_id  VARCHAR(64) NOT NULL,
        first_seen VARCHAR(40) NOT NULL,
        PRIMARY KEY (fp, case_id)
    );
    CREATE TABLE IF NOT EXISTS review_queue (
        id         INTEGER PRIMARY KEY {auto},
        run_id     TEXT NOT NULL,
        case_id    TEXT NOT NULL,
        category   TEXT NOT NULL,
        confidence REAL NOT NULL,
        reasoning  TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
        created_at TEXT NOT NULL
    );
    """


def _init_schema(conn) -> None:
    conn.executescript(_ddl())
    v = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    v = next(iter(v.values())) if isinstance(v, dict) else v[0]
    if not v:
        conn.execute("INSERT INTO schema_version VALUES (?, ?)", (_SCHEMA_VERSION, _now()))
    conn.commit()


# ---------------------------------------------------------------- runs
def start_run(run_id: str, task: str, patches_hash: str = "") -> None:
    db().execute(
        ("REPLACE INTO" if is_mysql() else "INSERT OR REPLACE INTO")
        + " runs (run_id, task, patches_hash, started_at) VALUES (?,?,?,?)",
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
        ("INSERT IGNORE INTO" if is_mysql() else "INSERT OR IGNORE INTO")
        + " fingerprint_cases (fp, case_id, first_seen) VALUES (?,?,?)",
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


# ---------------------------------------------------------------- 人工复核回流
def list_review_pending(limit: int = 20) -> list[dict]:
    """列出待人工复核的 uncertain 条目。"""
    rows = db().execute(
        "SELECT * FROM review_queue WHERE status='pending' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_review(review_id: int, accept: bool, fp: str | None = None) -> dict:
    """人工判定回流（闭环的关键一步）。

    accept=True  + fp → 指纹置回 'new'，下一轮允许建单（人工确认了这是真缺陷）
    accept=False + fp → 指纹置 'muted'，后续永不再建单（抑制误报）
    判定结果本身会进入 RAG 语料（agent/retrieve.py::_review_chunks）。
    """
    status = "accepted" if accept else "rejected"
    db().execute("UPDATE review_queue SET status=? WHERE id=?", (status, review_id))
    if fp:
        new_state = "new" if accept else "muted"
        note = f"人工复核 #{review_id}: {status}"
        db().execute(
            "UPDATE fingerprints SET state=?, note=? WHERE fp=?",
            (new_state, note, fp),
        )
    db().commit()
    return {"review_id": review_id, "status": status, "fp": fp}


def review_decisions(limit: int = 50) -> list[dict]:
    """已被人工判定的样本 —— RAG 回灌语料（高置信度、带人的判断）。"""
    rows = db().execute(
        """SELECT id, case_id, category, confidence, reasoning, status
           FROM review_queue WHERE status IN ('accepted','rejected')
           ORDER BY id DESC LIMIT ?""", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
