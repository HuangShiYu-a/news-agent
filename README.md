# AI 新闻简报 Agent

一个**手写 function-calling 循环**的每日 AI 新闻简报 Agent：LLM 自主决定调用哪些工具、调用几次、何时停止；调度、路径与命令边界、产物校验、触发时机与防重由程序保证。

- 设计基准文档（含全部决策记录 D1–D25）：[docs/design.md](docs/design.md)
- 技术栈：Python 3.10+ / FastAPI / SQLite / APScheduler / DeepSeek（OpenAI 兼容接口）

## 功能

1. 设置页保存身份与订阅偏好（话题、关键词）；
2. 手动"立即生成"：返回任务 ID，页面轮询展示每次工具调用（工具名、参数、结果、状态、耗时）；
3. Agent 在独立工作目录中用**五个工具**（`list_dir` / `read_file` / `search_content` / `write_file` / `bash`）自主筛选新闻并写出简报——不预设调用顺序，不强制调用全部工具；
4. 每条新闻含 `news_id` / 标题 / 来源链接 / 发布时间 / 摘要，按与偏好的相关度排序；产物经 **news_id 可追溯性校验**（校验失败会在同一段对话中带错误反馈修正一次）；
5. 每日定时自动生成（默认 15:00，Asia/Shanghai），完成后产生**站内未读通知**；同一日期定时任务只执行一次；
6. 历史简报页面可查；同一时刻至多一个生成任务。

## 5 分钟快速开始（演示模式）

环境要求：Windows 10/11、Python 3.10+（`py` 启动器）、Git for Windows。

```bat
git clone https://github.com/HuangShiYu-a/news-agent.git
cd news-agent
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env
```

> 国内网络建议走上面的清华 PyPI 镜像（实测约 1 分钟；直连 PyPI 可能需 5 分钟以上）。若本机系统代理已失效导致 pip 报 `ProxyError`，请临时关闭系统代理或设置 `NO_PROXY=*`。

编辑 `.env`，填入 `LLM_API_KEY`（DeepSeek，https://platform.deepseek.com 申请；**唯一必填项**，密钥只走环境变量不进版本库），然后启动：

```bat
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --workers 1
```

打开 http://127.0.0.1:8000 ：填写订阅偏好 → 点"立即生成" → 观察工具调用轨迹与简报。

命令行验证核心链路（可选）：

```bat
.venv\Scripts\python scripts\run_demo.py
```

## 两种新闻来源模式（`.env` 的 `NEWS_SOURCE`）

| 模式 | 说明 |
|---|---|
| `demo`（默认） | 使用 `fixtures/` 中的**虚构测试新闻样例**（14 条，内容为编造，仅用于演示），不联网抓取、完全可复现 |
| `rss` | 实时抓取 `RSS_SOURCES` 配置的源（默认 Hacker News / TechCrunch / 机器之心，均已实测直连可达）；采集层做确定性清洗：发布时间统一为 Asia/Shanghai、默认只保留最近 48 小时发布的新闻（`RSS_FRESHNESS_HOURS`，0=关闭窗口）、按保守规则归一化 URL 去重（缺发布时间的条目不进入材料）；部分源失败会继续并记录到任务，全部失败或窗口内无有效新闻任务记 `fetch_failed`，**绝不静默回退到样例数据** |

## 运行测试

```bat
.venv\Scripts\python -m pytest tests -q
```

不需要 API Key：模型客户端在测试中全部 mock；真实模型端到端已另行验证。

## 诚实声明（能力边界）

- **通知为站内实现**：定时简报生成后，打开的页面出现未读提醒（"今日简报已生成"），查看后清除；不提供页面关闭后的外部推送（无邮件 / IM）。推送位置为自定实现；演进方向是在生成成功后挂渠道适配器，生成与发送的边界已经存在。
- **bash 工具是受限执行**：白名单只读命令 `pwd` / `ls` / `head`，极小选项集，**不经过 shell 解释**（`shell=False` 直接执行 Git for Windows 自带的命令二进制），路径参数须在本次任务的工作目录内——管道、重定向、命令组合、任意脚本在没有 shell 的情况下天然不可能。真实进程、真实输出，非预设结果。
- **产物校验是引用可追溯性检查**（news_id 归属、条目内链接与材料逐字一致、无重复、空匹配声明、非空），不是对摘要内容的事实核查；**新闻相关性由 LLM 判断，校验器不做相关性裁决**（"本期暂无匹配新闻"只做格式检查）。
- **单用户、单进程单 worker**：按单个本地用户实现；无账号体系；并发防护用进程内锁，请保持 `--workers 1` 运行。
- **Agent 没有任何发送或联网工具**：五个工具都只能访问本次任务的独立工作目录（`data/runs/{任务ID}/`）。
- 演示模式样例为**虚构测试新闻**，页面与本文件均标注；样例日期固定不平移。

## 定时任务

`.env` 的 `DAILY_RUN_AT`（默认 `15:00`，Asia/Shanghai）。应用需保持运行，到点触发一次生成（成功后站内未读通知）；同一日期只执行一次（防重：当日已有成功的定时任务则跳过）；到点时如恰有其他任务在执行则跳过、不补跑；应用未运行时错过的时间点不补跑。

## 项目结构

```
app/
├── main.py               # FastAPI 入口（建表、清扫、调度、路由、静态页）
├── config.py             # 环境变量与限制常量集中管理
├── db.py                 # SQLite：短连接 + WAL + 幂等建表 + 启动清扫
├── agent/
│   ├── loop.py           # 手写 function-calling 循环 + LLM 客户端（核心）
│   ├── prompts.py        # system prompt（产物契约、路径约定、注入边界）
│   └── tools/            # base 协议 / fs 四工具 / bash 白名单直 exec
├── fetcher/rss.py        # RSS 抓取与规范化落盘
├── services/briefing.py  # 业务协调：建任务→备材料→跑 Agent→校验→落库
├── scheduler.py          # 每日定时调度
└── api/routes.py         # REST 接口
web/index.html            # 零依赖单页前端
scripts/run_demo.py       # 命令行端到端演示
schema.sql                # 建表脚本（settings / runs / tool_calls）
fixtures/                 # 虚构测试新闻样例（演示模式材料）
tests/                    # 循环正确性、安全边界、校验、路由、调度
```

## 核心设计取舍

1. 哪些步骤由模型决定（读什么、搜什么、写什么、何时停）；
2. 哪些边界由程序保证（调度、路径与命令边界、轮数上限、产物校验、触发时机与防重）；
3. 为什么这些取舍是合理的、哪些被刻意留作演进方向；
4. 推送为何选站内通知。

详见 [docs/design.md](docs/design.md) §13 与 §14 决策记录；实施过程与三阶段验收实测记录见 [docs/process.md](docs/process.md)；录屏演示脚本见 [docs/demo-script.md](docs/demo-script.md)。
