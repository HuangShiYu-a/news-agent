"""手写 function-calling 循环与 LLM 客户端（design.md §7.1 / §4）。

纯同步实现（D1）。循环只做模型交互、工具执行与结果回传，不含业务逻辑；
业务校验失败后的修正续跑（D5）通过复用 run() 实现：传入原消息列表、
已用轮数与同一截止时间，即与首次运行共享轮数与软时限预算。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from openai import (APIConnectionError, APIStatusError, AuthenticationError,
                    OpenAI, RateLimitError)

from app import config
from app.agent.tools.base import ToolRegistry, err


class LLMError(Exception):
    """基础设施级错误（鉴权失败 / 网络与超时 / 服务不可用），直接终止任务。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


class LLMClient:
    """OpenAI 兼容客户端（默认 DeepSeek）。超时与重试交给 SDK（D12）。"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout_s: int, max_retries: int):
        if not api_key:
            raise RuntimeError(
                "LLM_API_KEY 未配置：请复制 .env.example 为 .env，"
                "填入 DeepSeek API Key（https://platform.deepseek.com）后重试")
        self._client = OpenAI(api_key=api_key, base_url=base_url,
                              timeout=timeout_s, max_retries=max_retries)
        self._model = model

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        try:
            resp = self._client.chat.completions.create(
                model=self._model, messages=messages,
                tools=tools, tool_choice="auto")
        except AuthenticationError as e:
            raise LLMError("auth_error",
                           f"模型接口鉴权失败（检查 LLM_API_KEY）：{e}") from e
        except RateLimitError as e:
            raise LLMError("rate_limited", f"模型接口限流：{e}") from e
        except APIConnectionError as e:
            raise LLMError("connection_or_timeout",
                           f"模型接口连接失败或超时：{e}") from e
        except APIStatusError as e:
            raise LLMError("api_error",
                           f"模型服务错误（HTTP {e.status_code}）：{e}") from e
        choice = resp.choices[0]
        tool_calls = [
            {"id": tc.id, "name": tc.function.name,
             "arguments": tc.function.arguments or "{}"}
            for tc in (choice.message.tool_calls or [])
        ]
        return {"content": choice.message.content or "",
                "tool_calls": tool_calls,
                "finish_reason": choice.finish_reason}


@dataclass
class ToolCallRecord:
    """一次工具执行的记录（经 on_tool_call 回调落库/回显）。"""
    model_call_id: str
    tool_name: str
    args: dict
    result: dict
    duration_ms: int
    status: str  # succeeded / failed（对应统一返回值的 ok 标志）


@dataclass
class LoopResult:
    status: str  # completed / limit_exceeded / model_output_truncated /
                 # empty_response / infra_error
    content: str = ""
    messages: list = field(default_factory=list)
    turns_used: int = 0
    elapsed_s: float = 0.0
    error_detail: str = ""


class AgentLoop:
    def __init__(self, client, registry: ToolRegistry,
                 on_tool_call: Callable[[ToolCallRecord], None] | None = None):
        self.client = client
        self.registry = registry
        self.on_tool_call = on_tool_call

    def run(self, messages: list[dict], *, turns_used: int = 0,
            deadline: float | None = None) -> LoopResult:
        start = time.monotonic()
        if deadline is None:
            deadline = start + config.SOFT_TOTAL_LIMIT_S
        turns = turns_used
        while turns < config.MAX_TURNS:
            # 软总时限：仅在轮次之间检查，不中断进行中的模型请求（D12）
            if time.monotonic() >= deadline:
                return self._fail(messages, turns, start, "limit_exceeded",
                                  "轮间软总时限已到（单任务尽力而为的软限制）")
            try:
                resp = self.client.chat(messages, self.registry.schemas())
            except LLMError as e:
                return self._fail(messages, turns, start, "infra_error",
                                  f"{e.reason}: {e}")
            turns += 1
            if resp["finish_reason"] == "length":
                return self._fail(messages, turns, start, "model_output_truncated",
                                  "模型单次输出被截断（finish_reason=length）")
            if not resp["tool_calls"]:
                if not resp["content"].strip():
                    return self._fail(messages, turns, start, "empty_response",
                                      "模型返回了空内容")
                # 最终回答也回填进对话：修正续跑时模型能看到自己此前的完成表态
                messages.append({"role": "assistant", "content": resp["content"]})
                return LoopResult("completed", resp["content"], messages,
                                  turns, time.monotonic() - start)
            # ★ 必须先回填含 tool_calls 的 assistant 消息，再逐个执行并回传
            messages.append({
                "role": "assistant",
                "content": resp["content"] or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in resp["tool_calls"]],
            })
            for call in resp["tool_calls"]:
                result, record = self._execute(call)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
                if self.on_tool_call:
                    self.on_tool_call(record)
        return self._fail(messages, turns, start, "limit_exceeded",
                          f"已达最大轮数（{config.MAX_TURNS}）")

    def _execute(self, call: dict) -> tuple[dict, ToolCallRecord]:
        started = time.monotonic()
        args: dict = {}
        parse_error = ""
        if call["arguments"]:
            try:
                parsed = json.loads(call["arguments"])
                if not isinstance(parsed, dict):
                    raise ValueError("必须是 JSON 对象")
                args = parsed
            except (json.JSONDecodeError, ValueError) as e:
                parse_error = f"工具参数不是合法的 JSON 对象：{e}"
        result = (err("invalid_args", parse_error) if parse_error
                  else self.registry.execute(call["name"], args))
        duration_ms = int((time.monotonic() - started) * 1000)
        record = ToolCallRecord(call["id"], call["name"], args, result,
                                duration_ms,
                                "succeeded" if result["ok"] else "failed")
        return result, record

    def _fail(self, messages, turns, start, status, detail) -> LoopResult:
        return LoopResult(status, "", messages, turns,
                          time.monotonic() - start, detail)
