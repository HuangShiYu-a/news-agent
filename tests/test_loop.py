"""消息循环测试（mock 模型）：消息序列正确性 / 错误回传 / 超限（核心测试，不可砍）。"""
from __future__ import annotations

import json
import time

from app import config
from app.agent.loop import AgentLoop, LLMError
from app.agent.tools.base import Tool, ToolRegistry
from app.agent.tools.fs import build_fs_tools


class MockClient:
    """按脚本依次返回响应；记录每次调用收到的消息，供断言消息序列。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append((list(messages), tools))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def resp_with_calls(*calls, content=""):
    return {"content": content, "tool_calls": list(calls), "finish_reason": "tool_calls"}


def final_resp(content="完成"):
    return {"content": content, "tool_calls": [], "finish_reason": "stop"}


def tc(call_id, name, arguments):
    return {"id": call_id, "name": name, "arguments": arguments}


def echo_registry():
    return ToolRegistry([Tool(
        name="echo", description="回显文本",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
        handler=lambda text: {"echo": text})])


def base_messages():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


# —— 消息序列：assistant 回填 + 多调用逐个回传 ——


def test_assistant_backfill_and_multi_tool_round():
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "echo", '{"text": "a"}'),
                        tc("c2", "echo", '{"text": "b"}')),
        final_resp("done"),
    ])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "completed"
    assert result.content == "done" and result.turns_used == 2
    assert [m["role"] for m in messages] == \
        ["system", "user", "assistant", "tool", "tool", "assistant"]
    assert messages[2]["tool_calls"][0]["id"] == "c1"       # 回填的 assistant 消息
    assert messages[2]["tool_calls"][1]["id"] == "c2"
    assert messages[3]["tool_call_id"] == "c1"               # 逐个回传、顺序一致
    assert messages[4]["tool_call_id"] == "c2"
    payload = json.loads(messages[3]["content"])
    assert payload["ok"] is True and payload["result"]["echo"] == "a"


def test_tools_schema_passed_to_model():
    messages = base_messages()
    mock = MockClient([final_resp()])
    AgentLoop(mock, echo_registry()).run(messages)
    _, tools = mock.calls[0]
    assert tools[0]["function"]["name"] == "echo"


# —— 错误回传：不终止循环 ——


def test_tool_not_found_feeds_back(tmp_path):
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "read_file", '{"path": "missing.md"}')),
        final_resp(),
    ])
    registry = ToolRegistry(build_fs_tools(tmp_path))
    result = AgentLoop(mock, registry).run(messages)
    assert result.status == "completed"
    payload = json.loads(messages[3]["content"])
    assert payload["ok"] is False and payload["error"]["type"] == "not_found"


def test_unknown_tool_feeds_back():
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "no_such_tool", "{}")),
        final_resp(),
    ])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "completed"
    payload = json.loads(messages[3]["content"])
    assert payload["ok"] is False and payload["error"]["type"] == "invalid_args"
    assert "echo" in payload["error"]["message"]


def test_invalid_json_args_feeds_back():
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "echo", "not json")),
        final_resp(),
    ])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "completed"
    payload = json.loads(messages[3]["content"])
    assert payload["ok"] is False and payload["error"]["type"] == "invalid_args"


def test_wrong_arg_name_feeds_back():
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "echo", '{"wrong": 1}')),
        final_resp(),
    ])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "completed"
    payload = json.loads(messages[3]["content"])
    assert payload["ok"] is False and payload["error"]["type"] == "invalid_args"


# —— 终止状态 ——


def test_turn_limit():
    messages = base_messages()
    mock = MockClient([resp_with_calls(tc(f"c{i}", "echo", '{"text": "x"}'))
                       for i in range(config.MAX_TURNS + 5)])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "limit_exceeded"
    assert result.turns_used == config.MAX_TURNS
    assert len(mock.calls) == config.MAX_TURNS


def test_soft_deadline_expires_between_turns():
    messages = base_messages()
    mock = MockClient([])  # 不应发生任何模型调用
    result = AgentLoop(mock, echo_registry()).run(
        messages, deadline=time.monotonic() - 0.001)
    assert result.status == "limit_exceeded"
    assert mock.calls == []


def test_finish_reason_length():
    messages = base_messages()
    mock = MockClient([{"content": "xx", "tool_calls": [], "finish_reason": "length"}])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "model_output_truncated"


def test_empty_response():
    messages = base_messages()
    mock = MockClient([{"content": "", "tool_calls": [], "finish_reason": "stop"}])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "empty_response"


def test_infra_error_terminates():
    messages = base_messages()
    mock = MockClient([LLMError("auth_error", "boom")])
    result = AgentLoop(mock, echo_registry()).run(messages)
    assert result.status == "infra_error"
    assert "auth_error" in result.error_detail


# —— 修正续跑（D5）：同一段对话、共享轮数预算 ——


def test_correction_continuation_shares_budget():
    messages = base_messages()
    mock = MockClient([
        resp_with_calls(tc("c1", "echo", '{"text": "x"}')),
        final_resp("first"),
        resp_with_calls(tc("c2", "echo", '{"text": "y"}')),
        final_resp("fixed"),
    ])
    loop = AgentLoop(mock, echo_registry())
    first = loop.run(messages)
    assert first.status == "completed" and first.turns_used == 2

    messages.append({"role": "user", "content": "请修正 briefing.md"})
    corrected = loop.run(messages, turns_used=first.turns_used,
                         deadline=time.monotonic() + 60)
    assert corrected.status == "completed"
    assert corrected.turns_used == 4  # 2 + 2：预算累计而非重置

    # 修正轮的模型调用能看到完整历史（含 assistant 回填与修正消息）
    last_messages, _ = mock.calls[-1]
    assert any(m.get("role") == "user" and "修正" in m.get("content", "")
               for m in last_messages)


def test_correction_respects_shared_turn_budget():
    messages = base_messages()
    # 首轮用掉 MAX_TURNS-2 轮工具调用 + 1 轮最终回答 = MAX_TURNS-1 轮
    responses = [resp_with_calls(tc(f"c{i}", "echo", '{"text": "x"}'))
                 for i in range(config.MAX_TURNS - 2)]
    responses.append(final_resp("first"))
    mock = MockClient(responses + [resp_with_calls(tc("z", "echo", '{"text": "z"}'))])
    loop = AgentLoop(mock, echo_registry())
    first = loop.run(messages)
    assert first.status == "completed" and first.turns_used == config.MAX_TURNS - 1
    messages.append({"role": "user", "content": "请修正"})
    corrected = loop.run(messages, turns_used=first.turns_used,
                         deadline=time.monotonic() + 60)
    # 只剩 1 轮预算：该轮的工具调用执行完，循环因轮数耗尽终止，不再请求模型
    assert corrected.status == "limit_exceeded"
    assert corrected.turns_used == config.MAX_TURNS
    assert len(mock.calls) == config.MAX_TURNS
