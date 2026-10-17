"""命令行端到端演示入口（阶段一验收路径）。

在项目根目录运行：
    .venv\\Scripts\\python scripts\\run_demo.py
前置：已按 README 安装依赖，.env 已配置 LLM_API_KEY。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台默认编码可能是 GBK，显式切到 UTF-8 避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _short(value, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    from app import config
    from app.db import init_db
    from app.services.briefing import generate

    mode_note = ("（fixtures 虚构测试样例，可复现）"
                 if config.settings.news_source == "demo" else "")
    print(f"新闻来源模式：{config.settings.news_source}{mode_note}")
    print("正在生成简报（真实模型调用，工具调用将实时打印）…\n")

    def echo(seq: int, record) -> None:
        print(f"  #{seq:02d} {record.tool_name}({_short(record.args)})"
              f" -> {record.status} ({record.duration_ms}ms)")

    init_db()
    summary = generate(trigger="manual", echo=echo)

    print(f"\n任务 #{summary['run_id']}：{summary['status']}"
          + (f"（{summary['fail_reason']}）" if summary.get("fail_reason") else ""))
    if summary.get("fail_detail"):
        print(f"  明细：{_short(summary['fail_detail'], 400)}")
    print(f"轮数：{summary['turns']}  工具调用：{summary['tool_calls']} 次  "
          f"耗时：{summary['elapsed_s']:.1f}s  工作目录：{summary['workdir']}")

    if summary["briefing"]:
        print("\n" + "=" * 28 + " briefing.md " + "=" * 28)
        print(summary["briefing"])
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
