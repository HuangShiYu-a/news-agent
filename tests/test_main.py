"""应用启动路径测试：建表、清扫、/health。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn, init_db, now_iso
from app.main import app


def test_health_and_tables():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
    with get_conn() as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"settings", "runs", "tool_calls"} <= names


def test_startup_sweep_marks_interrupted():
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (trigger_type, status, brief_date, is_read, created_at) "
            "VALUES ('manual', 'running', '2026-10-17', 1, ?)",
            (now_iso(),))
        run_id = cur.lastrowid
    try:
        init_db()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT status, fail_reason FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["fail_reason"] == "interrupted"
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
