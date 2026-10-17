-- news-agent 建表脚本（design.md §6，单用户三张表，D19）
-- 幂等：应用启动时自动执行（D11）；不自动迁移，程序永不删库

CREATE TABLE IF NOT EXISTS settings (
    id          INTEGER PRIMARY KEY CHECK (id = 1),   -- 单用户：永远只有一行
    name        TEXT NOT NULL,
    persona     TEXT NOT NULL DEFAULT '',             -- 身份说明
    topics      TEXT NOT NULL DEFAULT '[]',           -- JSON 数组：关注话题
    keywords    TEXT NOT NULL DEFAULT '[]',           -- JSON 数组：关键词
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
    status       TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    brief_date   TEXT NOT NULL,                       -- 简报日期 YYYY-MM-DD（Asia/Shanghai）
    fail_reason  TEXT,                                -- limit_exceeded / validation_failed / fetch_failed /
                                                      -- infra_error / interrupted / model_output_truncated / empty_response
    fail_detail  TEXT,                                -- 失败明细（如校验错误清单）
    fetch_report TEXT,                                -- 抓取报告 JSON（部分源失败时记录，阶段二）
    briefing     TEXT,                                -- 简报正文 markdown（成功时随任务保存）
    is_read      INTEGER NOT NULL DEFAULT 0,          -- 站内未读通知标记（仅 scheduled 成功任务产生未读）
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    seq            INTEGER NOT NULL,                  -- run 内调用序号，从 1 开始
    model_call_id  TEXT,                              -- 模型返回的 tool_call id
    tool_name      TEXT NOT NULL,
    args_summary   TEXT,
    result_summary TEXT,
    status         TEXT NOT NULL,                     -- succeeded / failed（对应工具统一返回值的 ok 标志）
    duration_ms    INTEGER,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(brief_date, trigger_type, status);
