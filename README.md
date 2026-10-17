# AI 新闻简报 Agent

一个手写 function-calling 循环的每日 AI 新闻简报 Agent（面试题实现）：LLM 自主决定调用哪些工具、调用几次、何时停止；调度、路径与命令边界、产物校验、触发时机与防重由程序保证。技术栈：Python 3.10+ / FastAPI / SQLite / DeepSeek（OpenAI 兼容接口）。

- 设计基准文档（含全部决策记录 D1–D22）：[docs/design.md](docs/design.md)
- 当前进度：阶段一（核心链路）。完整 README（双模式启动说明）在交付阶段补齐。

## 开发期快速验证（阶段一）

```bash
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python scripts\run_demo.py
```

其中 `.env` 需填入 `LLM_API_KEY`（DeepSeek，https://platform.deepseek.com 申请）。

> 演示模式（`NEWS_SOURCE=demo`）使用 `fixtures/` 中的**虚构测试新闻样例**，不联网抓取、可复现；详见 [fixtures/README.md](fixtures/README.md)。
