"""db_check：只读连接 + 短轮询。

两条物理隔离的连接（冻结）：
  - 本模块 → 禅道业务库，只读账号，仅 SELECT（schema 层已拦非 SELECT）
  - 记忆层 → 自建库（week1 SQLite / week2 MySQL），读写
绝不混用。异步落库场景"刚写完查不到"用轮询解决，不判失败。
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from core.config import settings


def _connect():
    """按 DSN 建只读连接。DSN 形如 mysql://user:pass@host:3306/dbname"""
    import pymysql  # 延迟导入：week1 没配 DSN 时不强依赖

    u = urlparse(settings.zentao_db_dsn)
    conn = pymysql.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 3306,
        user=u.username or "",
        password=u.password or "",
        database=u.path.lstrip("/") or "zentao",
        charset="utf8mb4",
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.Cursor,
    )
    # 只读兜底：会话级禁写（schema 层已拦，这里是第二道）
    with conn.cursor() as c:
        c.execute("SET SESSION TRANSACTION READ ONLY")
    return conn


def poll_select(sql: str, interval: float, max_polls: int) -> dict:
    """轮询执行 SELECT，返回 {'rows': [...], 'polls': n}。全失败时 rows 为空。"""
    last_err: Exception | None = None
    for i in range(1, max_polls + 1):
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
                    return {"rows": rows, "polls": i}
        except Exception as e:  # noqa: BLE001 —— 记录后轮询重试
            last_err = e
            time.sleep(interval)
    return {"rows": [], "polls": max_polls, "error": str(last_err)}
