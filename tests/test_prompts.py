"""提示词测试：简报日期唯一来源、纠错消息结构。"""
from __future__ import annotations

from app.agent.prompts import (SYSTEM_PROMPT, build_correction_message,
                                build_task_message)


def test_task_message_carries_brief_date():
    msg = build_task_message("2026-10-18")
    assert "2026-10-18" in msg
    assert "简报日期" in msg


def test_system_prompt_pins_date_source_to_task_message():
    """日期唯一来源为任务消息，禁止从材料推测。"""
    assert "任务消息" in SYSTEM_PROMPT
    assert "不得根据新闻材料推测" in SYSTEM_PROMPT


def test_correction_message_lists_errors():
    msg = build_correction_message(["引用了不属于本次材料的 news_id：N0099",
                                    "条目的来源链接缺失"])
    assert "N0099" in msg and "来源链接缺失" in msg
