"""SQLite 访问：短连接 + WAL + busy_timeout；启动幂等建表与遗留任务清扫（D3/D11）。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app import config

_SCHEMA_PATH = config.BASE_DIR / "schema.sql"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def get_conn():
    """每次操作短连接（D3）。使用方在 with 块内操作，正常退出即提交。"""
    conn = sqlite3.connect(config.settings.db_path,
                            timeout=config.BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """幂等建表 + 清扫上次进程中断遗留的执行中任务。

    只应在进程启动时调用（main.py lifespan / 命令行入口），
    不得在每次生成任务时调用——否则会把正在执行的其他任务误标为中断。
    """
    config.settings.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "UPDATE runs SET status = 'failed', fail_reason = 'interrupted', "
            "ended_at = ? WHERE status IN ('pending', 'running')",
            (now_iso(),),
        )
