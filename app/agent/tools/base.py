"""工具协议：定义、注册与统一执行（design.md §7.2）。

统一返回值：{"ok": true, "result": ...} 或
           {"ok": false, "error": {"type": ..., "message": ...}}
错误类型：not_found / permission_denied / invalid_args /
         execution_failed / limit_exceeded
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

ERROR_TYPES = ("not_found", "permission_denied", "invalid_args",
               "execution_failed", "limit_exceeded")


def ok(result: Any) -> dict:
    return {"ok": True, "result": result}


def err(error_type: str, message: str) -> dict:
    if error_type not in ERROR_TYPES:
        raise ValueError(f"未知错误类型：{error_type}")
    return {"ok": False, "error": {"type": error_type, "message": message}}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # OpenAI function parameters（JSON Schema）
    handler: Callable[..., Any]

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class ToolRegistry:
    tools: list[Tool] = field(default_factory=list)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def execute(self, name: str, args: dict) -> dict:
        """执行工具，永不抛出：一切失败都转为统一错误结构回传模型。"""
        tool = next((t for t in self.tools if t.name == name), None)
        if tool is None:
            available = ", ".join(t.name for t in self.tools)
            return err("invalid_args", f"工具不存在：{name}。可用工具：{available}")
        try:
            out = tool.handler(**args)
        except TypeError as e:
            return err("invalid_args", f"参数不合法：{e}")
        except Exception as e:  # 工具内部异常统一兜底，不终止循环
            return err("execution_failed", f"{type(e).__name__}: {e}")
        return out if isinstance(out, dict) and "ok" in out else ok(out)
