"""配置中心 —— 唯一允许读环境变量的地方。

契约：其他模块一律 from core.config import settings，禁止散落 os.getenv。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # 被测系统
    zentao_base: str = os.getenv("ZENTAO_BASE", "http://127.0.0.1:8080").rstrip("/")
    account: str = os.getenv("ZENTAO_ACCOUNT", "admin")
    password: str = os.getenv("ZENTAO_PASSWORD", "123456")
    alt_account: str = os.getenv("ZENTAO_ALT_ACCOUNT", "test1")
    alt_password: str = os.getenv("ZENTAO_ALT_PASSWORD", "123456")
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "10"))

    # token 文件缓存（执行器子进程 / MCP 常驻进程共用）
    token_ttl_seconds: int = int(os.getenv("TOKEN_TTL_SECONDS", "1800"))
    token_cache: Path = ROOT / "report" / ".token_cache.json"

    # db_check：禅道业务库【只读】账号（物理隔离，仅 SELECT）
    zentao_db_dsn: str = os.getenv("ZENTAO_DB_DSN", "")  # mysql://user:pass@127.0.0.1:3306/zentao
    dbcheck_poll_interval: float = float(os.getenv("DBCHECK_POLL_INTERVAL", "0.2"))
    dbcheck_max_polls: int = int(os.getenv("DBCHECK_MAX_POLLS", "5"))

    # 记忆层：week1 用 SQLite（同 schema），week2 换 MySQL 只改此 DSN
    ledger_dsn: str = os.getenv("LEDGER_DSN", f"sqlite:///{(ROOT / 'report' / 'ledger.db').as_posix()}")

    # 功能开关（AI 审核方法论④：先只跑不建单，误报率可接受后再开）
    auto_create_bug: bool = _bool(os.getenv("AUTO_CREATE_BUG", "false"))

    # 执行保护
    setup_breaker_threshold: int = int(os.getenv("SETUP_BREAKER_THRESHOLD", "3"))
    max_cases_per_run: int = int(os.getenv("MAX_CASES_PER_RUN", "60"))

    # 产物目录
    report_dir: Path = ROOT / "report"
    data_dir: Path = ROOT / "data"


settings = Settings()
settings.report_dir.mkdir(parents=True, exist_ok=True)
