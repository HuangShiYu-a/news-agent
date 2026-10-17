"""API 路由测试：设置、生成（含 409 并发拒绝）、轮询、历史、未读通知流。

注意：测试不进入 lifespan（避免启动清扫把插入的 running 行误标中断），
表结构沿用开发库（已由此前启动建好）。
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.db import get_conn, now_iso
from app.main import app
from app.services import briefing

client = TestClient(app)


def _insert_run(trigger="manual", status="succeeded", is_read=1,
                briefing="# t"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (trigger_type, status, brief_date, is_read, "
            "briefing, created_at, ended_at) VALUES (?, ?, '2026-10-17', ?, ?, ?, ?)",
            (trigger, status, is_read, briefing, now_iso(), now_iso()))
        return cur.lastrowid


def _cleanup(run_ids):
    with get_conn() as conn:
        for rid in run_ids:
            conn.execute("DELETE FROM tool_calls WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))


def test_settings_roundtrip():
    before = client.get("/api/settings").json()
    resp = client.put("/api/settings", json={
        "name": "路由测试", "persona": "测试身份",
        "topics": ["AI Agent", "  "], "keywords": ["MCP"]})
    assert resp.status_code == 200
    saved = client.get("/api/settings").json()
    assert saved["name"] == "路由测试"
    assert saved["topics"] == ["AI Agent"]  # 空白项被清理
    assert saved["keywords"] == ["MCP"]
    # 还原，避免污染开发数据
    client.put("/api/settings", json={
        "name": before["name"], "persona": before["persona"],
        "topics": before["topics"], "keywords": before["keywords"]})


def test_settings_rejects_empty_name():
    resp = client.put("/api/settings", json={
        "name": "  ", "persona": "", "topics": [], "keywords": []})
    assert resp.status_code == 422


def test_meta_endpoint():
    data = client.get("/api/meta").json()
    assert data["news_source"] in ("demo", "rss")
    assert data["timezone"] == "Asia/Shanghai"
    assert data["daily_run_at"]


def test_generate_busy_returns_409():
    active = _insert_run(status="running", is_read=1, briefing=None)
    try:
        resp = client.post("/api/generate")
        assert resp.status_code == 409
        assert "执行中" in resp.json()["detail"]
    finally:
        _cleanup([active])


def test_generate_starts_background_execution(monkeypatch):
    def fake_execute(run_id, trigger, client=None, echo=None):
        briefing._update_run(run_id, status="succeeded", ended_at=now_iso(),
                             briefing="# fake briefing")
    monkeypatch.setattr(briefing, "_execute_run", fake_execute)

    resp = client.post("/api/generate")
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    try:
        status = None
        for _ in range(50):  # 等待后台线程完成
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            status = row["status"] if row else None
            if status == "succeeded":
                break
            time.sleep(0.1)
        assert status == "succeeded"
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["briefing"] == "# fake briefing"
    finally:
        _cleanup([run_id])


def test_run_list_tool_calls_and_404():
    rid = _insert_run()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tool_calls (run_id, seq, model_call_id, tool_name, "
            "args_summary, result_summary, status, duration_ms, created_at) "
            "VALUES (?, 1, 'c1', 'read_file', '{}', '{}', 'succeeded', 3, ?)",
            (rid, now_iso()))
    try:
        rows = client.get("/api/runs").json()
        assert any(r["id"] == rid for r in rows)
        calls = client.get(f"/api/runs/{rid}/tool-calls").json()
        assert calls[0]["tool_name"] == "read_file" and calls[0]["seq"] == 1
        assert client.get("/api/runs/999999").status_code == 404
        assert client.get("/api/runs/999999/tool-calls").status_code == 404
    finally:
        _cleanup([rid])


def test_unread_notification_flow():
    before = client.get("/api/notifications/unread").json()["count"]
    rid = _insert_run(trigger="scheduled", status="succeeded", is_read=0)
    try:
        data = client.get("/api/notifications/unread").json()
        assert data["count"] == before + 1
        assert data["latest"]["id"] == rid
        resp = client.post(f"/api/runs/{rid}/read")
        assert resp.status_code == 200
        data = client.get("/api/notifications/unread").json()
        assert data["count"] == before
    finally:
        _cleanup([rid])
