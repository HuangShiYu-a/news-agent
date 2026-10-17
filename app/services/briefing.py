"""业务协调层（design.md §4 / §8 / §9）：建任务 → 准备材料 → 跑 Agent →
校验产物（失败同对话修正一次）→ 落库。

三个入口共用同一条执行路径（design.md §4 关键原则）：
- generate()      命令行（scripts/run_demo.py）：同步创建并执行
- start_generate() API（POST /api/generate）：创建后在后台线程执行，立即返回任务 ID
- scheduler       定时触发（阶段三接入）
并发防护（D20）：进程内 threading.Lock 保护"检查无执行中任务 + 建任务"，
"先查再建"不是原子操作，多线程下会竞争；单进程单 worker 约束下这把锁足够。
注意：本模块不调用 init_db()（清扫会误杀执行中任务，只能进程启动时做）。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app import config
from app.agent.loop import AgentLoop, LLMClient, ToolCallRecord
from app.agent.prompts import (SYSTEM_PROMPT, build_correction_message,
                                build_task_message)
from app.agent.tools.base import ToolRegistry
from app.agent.tools.bash import build_bash_tool
from app.agent.tools.fs import build_fs_tools
from app.db import get_conn, now_iso
from app.fetcher.rss import FetchError, assign_news_ids, fetch_all

# 演示默认偏好（settings 表为空时种子写入；页面保存后覆盖）
DEFAULT_PREFERENCES = {
    "name": "演示用户",
    "persona": "关注 AI 应用开发的工程师",
    "topics": ["AI Agent", "开源模型"],
    "keywords": ["function calling", "MCP", "多模态"],
}

_BRIEFING_FILENAME = "briefing.md"
_NEWS_ID_RE = re.compile(r"\[(N\d+)\]")
_NO_MATCH_PHRASE = "本期暂无匹配新闻"
_RUN_UPDATABLE = {"status", "fail_reason", "fail_detail", "fetch_report",
                  "briefing", "started_at", "ended_at"}

_create_lock = threading.Lock()  # D20：保护"查重 + 建任务"的原子性


class BusyError(RuntimeError):
    """已有生成任务在执行中。"""


def load_or_seed_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (id, name, persona, topics, keywords, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (DEFAULT_PREFERENCES["name"], DEFAULT_PREFERENCES["persona"],
                 json.dumps(DEFAULT_PREFERENCES["topics"], ensure_ascii=False),
                 json.dumps(DEFAULT_PREFERENCES["keywords"], ensure_ascii=False),
                 now_iso()))
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return {"name": row["name"], "persona": row["persona"],
            "topics": json.loads(row["topics"]),
            "keywords": json.loads(row["keywords"])}


def save_settings(name: str, persona: str, topics: list[str],
                  keywords: list[str]) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("姓名不能为空")
    topics = [t.strip() for t in topics if t and t.strip()][:10]
    keywords = [k.strip() for k in keywords if k and k.strip()][:15]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (id, name, persona, topics, keywords, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "persona=excluded.persona, topics=excluded.topics, "
            "keywords=excluded.keywords, updated_at=excluded.updated_at",
            (name, persona.strip()[:200],
             json.dumps(topics, ensure_ascii=False),
             json.dumps(keywords, ensure_ascii=False), now_iso()))
    return load_or_seed_settings()


def _brief_date() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def _render_preferences(s: dict) -> str:
    return ("# 订阅偏好\n\n"
            f"- 姓名：{s['name']}\n"
            f"- 身份：{s['persona'] or '（未填写）'}\n"
            f"- 关注话题：{'、'.join(s['topics']) or '（未设置）'}\n"
            f"- 关键词：{'、'.join(s['keywords']) or '（未设置）'}\n")


def _prepare_workspace(workdir: Path, settings: dict) -> list[dict] | None:
    """准备工作目录（固定步骤，非 Agent 决策）。

    返回抓取报告（真实模式；部分源失败时含失败明细），演示模式返回 None。
    全部源失败抛 FetchError（D13）。
    """
    (workdir / "news").mkdir(parents=True, exist_ok=True)
    if config.settings.news_source == "demo":
        fixtures = sorted(config.settings.fixtures_dir.glob("*.jsonl"))
        if not fixtures:
            raise FetchError("fixtures 目录为空：演示模式依赖虚构新闻样例")
        for f in fixtures:
            shutil.copy(f, workdir / "news" / f.name)
        report = None
    else:
        urls = config.settings.rss_source_list
        if not urls:
            raise FetchError("真实模式需要在 .env 配置 RSS_SOURCES（逗号分隔的源列表）")
        by_source, report = fetch_all(urls)
        for source, items in assign_news_ids(by_source).items():
            lines = [json.dumps(item, ensure_ascii=False) for item in items]
            (workdir / "news" / f"{source}.jsonl").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
    (workdir / "preferences.md").write_text(_render_preferences(settings),
                                            encoding="utf-8")
    return report


def load_material_news(workdir: Path) -> dict[str, dict]:
    """读取工作目录 news/ 下全部材料，返回 news_id → 条目。"""
    news: dict[str, dict] = {}
    for f in sorted((workdir / "news").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            news[item["news_id"]] = item
    return news


def validate_briefing(workdir: Path) -> tuple[bool, list[str]]:
    """产物校验（design.md §9）：引用可追溯性检查，非事实核查。"""
    path = workdir / _BRIEFING_FILENAME
    if not path.exists():
        return False, ["briefing.md 不存在：请把简报写入工作目录根部的 briefing.md"]
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return False, ["briefing.md 是空文件"]
    news = load_material_news(workdir)
    ids = _NEWS_ID_RE.findall(text)
    if not ids:
        if _NO_MATCH_PHRASE in text:
            return True, []
        return False, ['简报中没有任何 [news_id] 引用，也未声明"本期暂无匹配新闻"']
    errors: list[str] = []
    seen: set[str] = set()
    dup: list[str] = []
    unknown: list[str] = []
    for i in ids:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
        if i not in news and i not in unknown:
            unknown.append(i)
    if unknown:
        errors.append(f"引用了不属于本次材料的 news_id：{', '.join(unknown)}")
    if dup:
        errors.append(f"同一条新闻被重复引用：{', '.join(dup)}")
    missing_url = [i for i in dict.fromkeys(ids)
                   if i in news and news[i]["url"] not in text]
    if missing_url:
        errors.append("以下新闻的来源链接与材料不一致（链接必须与材料逐字一致）："
                      + ", ".join(missing_url))
    return (not errors), errors


def _update_run(run_id: int, **fields) -> None:
    if not set(fields) <= _RUN_UPDATABLE:
        raise ValueError(f"不允许更新的字段：{set(fields) - _RUN_UPDATABLE}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id = ?",
                     (*fields.values(), run_id))


def _summary_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)[:500]


def _create_run(trigger: str) -> int:
    """锁内完成"无执行中任务检查 + 建任务"（D20）；忙时抛 BusyError。"""
    with _create_lock:
        with get_conn() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('pending', 'running')"
            ).fetchone()[0]
            if active:
                raise BusyError("已有生成任务在执行中，请等待完成后再试")
            cur = conn.execute(
                "INSERT INTO runs (trigger_type, status, brief_date, is_read, created_at) "
                "VALUES (?, 'pending', ?, ?, ?)",
                (trigger, _brief_date(), 1 if trigger == "manual" else 0, now_iso()))
            return cur.lastrowid


def start_generate(trigger: str = "manual") -> int:
    """API 入口：建任务（锁内查重）后立即返回任务 ID，执行放后台线程。"""
    if trigger not in ("manual", "scheduled"):
        raise ValueError("trigger 只能是 manual 或 scheduled")
    run_id = _create_run(trigger)
    threading.Thread(target=_execute_run, args=(run_id, trigger),
                     daemon=True, name=f"briefing-run-{run_id}").start()
    return run_id


def generate(trigger: str = "manual", client=None,
             echo: Callable[[int, ToolCallRecord], None] | None = None) -> dict:
    """命令行入口：同步创建并执行（供 scripts/run_demo.py）。"""
    if trigger not in ("manual", "scheduled"):
        raise ValueError("trigger 只能是 manual 或 scheduled")
    run_id = _create_run(trigger)
    return _execute_run(run_id, trigger, client=client, echo=echo)


def _execute_run(run_id: int, trigger: str, client=None,
                 echo: Callable[[int, ToolCallRecord], None] | None = None) -> dict:
    """执行一次生成任务的全过程（三条入口共用的唯一执行路径）。"""
    t0 = time.monotonic()
    settings = load_or_seed_settings()
    workdir = config.settings.data_dir / "runs" / str(run_id)
    _update_run(run_id, status="running", started_at=now_iso())

    # —— 材料准备（固定步骤）——
    try:
        report = _prepare_workspace(workdir, settings)
        if report is not None and any(not r["ok"] for r in report):
            _update_run(run_id, fetch_report=json.dumps(report, ensure_ascii=False))
    except Exception as e:
        _update_run(run_id, status="failed", fail_reason="fetch_failed",
                    fail_detail=str(e), ended_at=now_iso())
        return _summary(run_id, trigger, "failed", workdir, t0,
                        fail_reason="fetch_failed", fail_detail=str(e))

    registry = ToolRegistry(build_fs_tools(workdir) + [build_bash_tool(workdir)])
    seq = 0

    def on_tool_call(record: ToolCallRecord) -> None:
        nonlocal seq
        seq += 1
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO tool_calls (run_id, seq, model_call_id, tool_name, "
                "args_summary, result_summary, status, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, record.model_call_id, record.tool_name,
                 _summary_json(record.args), _summary_json(record.result),
                 record.status, record.duration_ms, now_iso()))
        if echo:
            echo(seq, record)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_task_message()},
    ]
    deadline = time.monotonic() + config.SOFT_TOTAL_LIMIT_S
    final_status, fail_reason, fail_detail, turns = "failed", None, None, 0

    try:
        if client is None:
            client = LLMClient(config.settings.llm_api_key,
                               config.settings.llm_base_url,
                               config.settings.llm_model,
                               config.LLM_TIMEOUT_S, config.LLM_MAX_RETRIES)
        loop = AgentLoop(client, registry, on_tool_call=on_tool_call)
        result = loop.run(messages, deadline=deadline)
        turns = result.turns_used
        if result.status != "completed":
            fail_reason, fail_detail = result.status, result.error_detail
        else:
            ok, errors = validate_briefing(workdir)
            if ok:
                final_status = "succeeded"
            else:
                # 一次修正（D5）：错误清单回传同一段对话，共用轮数与软时限预算
                messages.append({"role": "user",
                                 "content": build_correction_message(errors)})
                corrected = loop.run(messages, turns_used=result.turns_used,
                                     deadline=deadline)
                turns = corrected.turns_used
                if corrected.status != "completed":
                    fail_reason = corrected.status
                    fail_detail = corrected.error_detail
                else:
                    ok2, errors2 = validate_briefing(workdir)
                    if ok2:
                        final_status = "succeeded"
                    else:
                        fail_reason = "validation_failed"
                        fail_detail = "；".join(errors2)
    except Exception as e:  # 基础设施级异常（密钥缺失、网络中断等）统一落库
        fail_reason = "infra_error"
        fail_detail = f"{type(e).__name__}: {e}"

    briefing_text = ""
    briefing_file = workdir / _BRIEFING_FILENAME
    if briefing_file.exists():
        briefing_text = briefing_file.read_text(encoding="utf-8", errors="replace")
    _update_run(run_id, status=final_status, ended_at=now_iso(),
                fail_reason=fail_reason, fail_detail=fail_detail,
                briefing=briefing_text or None)
    return _summary(run_id, trigger, final_status, workdir, t0, turns=turns,
                    fail_reason=fail_reason, fail_detail=fail_detail,
                    briefing=briefing_text, tool_calls=seq)


def _summary(run_id: int, trigger: str, status: str, workdir: Path, t0: float,
             turns: int = 0, fail_reason: str | None = None,
             fail_detail: str | None = None, briefing: str = "",
             tool_calls: int = 0) -> dict:
    return {"run_id": run_id, "status": status, "trigger": trigger,
            "turns": turns, "tool_calls": tool_calls,
            "fail_reason": fail_reason, "fail_detail": fail_detail,
            "briefing": briefing, "workdir": str(workdir),
            "elapsed_s": time.monotonic() - t0}
