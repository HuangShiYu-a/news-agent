"""集中管理环境变量与运行限制常量（design.md §2 / D21）。

环境变量只保留必要的一组；限制数值集中为本模块常量，
不做全量可配置框架。
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# —— 运行限制常量（集中于此）——
MAX_TURNS = 15                # Agent 最大轮数
LLM_TIMEOUT_S = 60            # 单次模型请求超时
LLM_MAX_RETRIES = 1           # SDK 内建重试次数（不自写重试，D12）
TOOL_TIMEOUT_S = 10           # 单个工具执行超时
TOOL_OUTPUT_LIMIT = 10_000    # 单次工具输出/读取截断上限（字符）
WRITE_LIMIT = 100_000         # write_file 单次写入上限（字符）
SOFT_TOTAL_LIMIT_S = 300      # 轮间软总时限：尽力而为，不承诺硬截止（D12）
TIMEZONE = "Asia/Shanghai"    # 定时与简报日期判定时区
BUSY_TIMEOUT_MS = 5_000       # SQLite busy_timeout


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    # 模型接口（OpenAI 兼容，默认 DeepSeek）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # 新闻来源：demo=fixtures 固定虚构样例；rss=实时抓取（阶段二实现）
    news_source: str = "demo"
    rss_sources: str = ""

    # 每日定时生成时间（HH:MM，Asia/Shanghai）
    daily_run_at: str = "08:00"

    # 可选：覆盖 bash 工具命令二进制目录（默认自动探测 Git for Windows usr/bin）
    tool_bin_dir: str = ""

    @field_validator("news_source")
    @classmethod
    def _check_news_source(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("demo", "rss"):
            raise ValueError("NEWS_SOURCE 只能是 demo 或 rss")
        return v

    @field_validator("daily_run_at")
    @classmethod
    def _check_daily_run_at(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"\d{2}:\d{2}", v):
            raise ValueError("DAILY_RUN_AT 格式须为 HH:MM，如 08:00")
        hh, mm = int(v[:2]), int(v[3:])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("DAILY_RUN_AT 时间超出范围")
        return v

    @property
    def rss_source_list(self) -> list[str]:
        return [s.strip() for s in self.rss_sources.split(",") if s.strip()]

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "news_agent.db"

    @property
    def fixtures_dir(self) -> Path:
        return BASE_DIR / "fixtures"


settings = Settings()
