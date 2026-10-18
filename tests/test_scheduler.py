"""定时调度测试：trigger 未读标记、忙时拒绝、防重语义（§8）、调度器装配。"""
from __future__ import annotations

import pytest

from app import scheduler
from app.db import get_conn, now_iso
from app.services import briefing


def _insert_run(trigger, status, brief_date, is_read):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs (trigger_type, status, brief_date, is_read, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (trigger, status, brief_date, is_read, now_iso()))
        return cur.lastrowid


def _cleanup(run_ids):
    with get_conn() as conn:
        for rid in run_ids:
            conn.execute("DELETE FROM tool_calls WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))


def test_create_run_is_read_follows_trigger():
    """手动任务不产生未读；定时任务 is_read=0（站内通知语义，§8/D16）。"""
    manual = briefing._create_run("manual")
    # 建完的任务处于 pending，会阻塞下一次创建：先置终态
    briefing._update_run(manual, status="failed", ended_at=now_iso())
    scheduled = briefing._create_run("scheduled")
    try:
        with get_conn() as conn:
            m = conn.execute("SELECT is_read FROM runs WHERE id = ?",
                             (manual,)).fetchone()
            s = conn.execute("SELECT is_read FROM runs WHERE id = ?",
                             (scheduled,)).fetchone()
        assert m["is_read"] == 1 and s["is_read"] == 0
    finally:
        _cleanup([manual, scheduled])


def test_create_run_busy_raises():
    rid = briefing._create_run("manual")
    try:
        with pytest.raises(briefing.BusyError):
            briefing._create_run("manual")
    finally:
        _cleanup([rid])


def test_has_scheduled_success_today():
    baseline = scheduler.has_scheduled_success_today()
    rid = _insert_run("scheduled", "succeeded", briefing._brief_date(), 0)
    try:
        assert scheduler.has_scheduled_success_today() is True
    finally:
        _cleanup([rid])
    assert scheduler.has_scheduled_success_today() == baseline


def test_dedup_ignores_manual_failed_other_dates():
    """手动 / 失败 / 非今日 的记录都不构成"今日已有成功的定时简报"。"""
    baseline = scheduler.has_scheduled_success_today()
    today = briefing._brief_date()
    ids = [
        _insert_run("manual", "succeeded", today, 1),
        _insert_run("scheduled", "failed", today, 0),
        _insert_run("scheduled", "succeeded", "2000-01-01", 0),
    ]
    try:
        assert scheduler.has_scheduled_success_today() == baseline
    finally:
        _cleanup(ids)


def test_run_scheduled_skips_when_done_today(monkeypatch):
    monkeypatch.setattr(scheduler, "has_scheduled_success_today", lambda: True)

    def boom(trigger):
        raise AssertionError("今日已完成，不应再创建任务")
    monkeypatch.setattr(scheduler.briefing, "start_generate", boom)
    scheduler.run_scheduled_briefing()  # 直接跳过，不创建


def test_run_scheduled_creates_when_not_done(monkeypatch):
    monkeypatch.setattr(scheduler, "has_scheduled_success_today", lambda: False)
    calls = []
    monkeypatch.setattr(scheduler.briefing, "start_generate",
                        lambda trigger: calls.append(trigger) or 123)
    scheduler.run_scheduled_briefing()
    assert calls == ["scheduled"]


def test_run_scheduled_busy_is_swallowed(monkeypatch):
    monkeypatch.setattr(scheduler, "has_scheduled_success_today", lambda: False)

    def busy(trigger):
        raise briefing.BusyError("已有任务在执行中")
    monkeypatch.setattr(scheduler.briefing, "start_generate", busy)
    scheduler.run_scheduled_briefing()  # 忙时静默跳过，不抛异常


def test_scheduler_starts_and_stops():
    sched = scheduler.start_scheduler()
    try:
        jobs = sched.get_jobs()
        assert any(job.id == "daily-briefing" for job in jobs)
    finally:
        scheduler.stop_scheduler()
