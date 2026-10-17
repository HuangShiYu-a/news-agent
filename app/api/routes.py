"""API 路由（design.md §4）：设置 / 元信息 / 立即生成 / 状态与轨迹轮询 /
历史简报 / 站内未读通知（D16）。

处理函数为普通 def：FastAPI 自动放入线程池执行，不阻塞事件循环；
生成任务本身在 briefing.start_generate 的后台线程中运行（D1/D20）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.db import get_conn
from app.services import briefing

router = APIRouter(prefix="/api")


class SettingsIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    persona: str = Field(default="", max_length=200)
    topics: list[str] = Field(default_factory=list, max_length=10)
    keywords: list[str] = Field(default_factory=list, max_length=15)


@router.get("/settings")
def get_settings():
    return briefing.load_or_seed_settings()


@router.put("/settings")
def put_settings(payload: SettingsIn):
    topics = [t.strip()[:30] for t in payload.topics if t.strip()]
    keywords = [k.strip()[:40] for k in payload.keywords if k.strip()]
    try:
        return briefing.save_settings(payload.name, payload.persona,
                                      topics, keywords)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/meta")
def get_meta():
    return {"news_source": config.settings.news_source,
            "is_fictional": config.settings.news_source == "demo",
            "llm_model": config.settings.llm_model,
            "daily_run_at": config.settings.daily_run_at,
            "timezone": config.TIMEZONE}


@router.post("/generate")
def post_generate():
    try:
        run_id = briefing.start_generate("manual")
    except briefing.BusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"run_id": run_id}


@router.get("/runs")
def list_runs(limit: int = 30):
    limit = max(1, min(limit, 100))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, trigger_type, status, brief_date, fail_reason, is_read, "
            "created_at, ended_at, (briefing IS NOT NULL) AS has_briefing "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    out = dict(row)
    if out.get("fetch_report"):
        try:
            out["fetch_report"] = json.loads(out["fetch_report"])
        except (json.JSONDecodeError, TypeError):
            pass
    return out


@router.get("/runs/{run_id}/tool-calls")
def get_tool_calls(run_id: int):
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM runs WHERE id = ?",
                              (run_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        rows = conn.execute(
            "SELECT seq, model_call_id, tool_name, args_summary, result_summary, "
            "status, duration_ms, created_at "
            "FROM tool_calls WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/notifications/unread")
def unread_notifications():
    """站内未读通知：成功且未读的任务（仅 scheduled 会产生未读，§8）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, brief_date, created_at FROM runs "
            "WHERE status = 'succeeded' AND is_read = 0 ORDER BY id DESC"
        ).fetchall()
    return {"count": len(rows),
            "latest": dict(rows[0]) if rows else None}


@router.post("/runs/{run_id}/read")
def mark_read(run_id: int):
    with get_conn() as conn:
        cur = conn.execute("UPDATE runs SET is_read = 1 WHERE id = ?", (run_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}
