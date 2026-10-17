"""受限 bash 工具：白名单只读命令直 exec（design.md §7.4 / D17）。

不引入 shell 解释器：校验后的参数以 shell=False 直接执行，
管道、重定向、命令组合、任意脚本在没有 shell 的情况下天然不可能发生。
命令白名单 {pwd, ls, head}，路径参数经过与文件工具相同的目录边界检查
（realpath 后必须仍在工作目录内），命令二进制取自 Git for Windows 的
usr/bin（可用环境变量 TOOL_BIN_DIR 覆盖）。真实执行、真实输出。
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from app import config
from app.agent.tools.base import Tool, err, ok
from app.agent.tools.fs import resolve_inside_workdir

# 命令白名单与各自允许的选项（极小集合，不做通用选项解析）
COMMANDS: dict[str, list[str]] = {
    "pwd": [],
    "ls": ["-l", "-a", "-la", "-al", "-1"],
    "head": ["-n"],
}
# 任何包含这些字符的命令直接拒绝（shell 元字符 + 反斜杠）
FORBIDDEN_CHARS = ";|&$`><\\"

_BIN_DIR: Path | None = None
_BIN_DIR_RESOLVED = False


def _exe_path(bin_dir: Path, cmd: str) -> Path | None:
    for name in (cmd + ".exe", cmd):
        p = bin_dir / name
        if p.is_file():
            return p
    return None


def _detect_bin_dir() -> Path | None:
    override = (config.settings.tool_bin_dir or "").strip()
    if override:
        p = Path(override)
        return p if _exe_path(p, "ls") else None
    exe = shutil.which("ls")
    if exe:
        return Path(exe).parent
    git = shutil.which("git")
    if git:
        cand = Path(git).resolve().parents[1] / "usr" / "bin"
        if _exe_path(cand, "ls"):
            return cand
    for cand in (Path(r"C:\Program Files\Git\usr\bin"),
                 Path(r"C:\Program Files (x86)\Git\usr\bin"),
                 Path.home() / "AppData" / "Local" / "Programs" / "Git" / "usr" / "bin"):
        if _exe_path(cand, "ls"):
            return cand
    return None


def _find_bin_dir() -> Path | None:
    global _BIN_DIR, _BIN_DIR_RESOLVED
    if not _BIN_DIR_RESOLVED:
        _BIN_DIR = _detect_bin_dir()
        _BIN_DIR_RESOLVED = True
    return _BIN_DIR


def build_bash_tool(workdir: Path) -> Tool:
    workdir = workdir.resolve()

    def handler(command: str) -> dict:
        if not isinstance(command, str) or not command.strip():
            return err("invalid_args", "command 不能为空")
        bad = sorted({c for c in FORBIDDEN_CHARS if c in command})
        if bad:
            return err("invalid_args",
                       f"命令包含禁止字符：{' '.join(bad)}。本工具不经过 shell，"
                       "不支持管道/重定向/变量/转义；路径请用正斜杠相对路径")
        try:
            tokens = shlex.split(command)
        except ValueError as e:
            return err("invalid_args", f"命令无法解析：{e}")
        if not tokens:
            return err("invalid_args", "command 为空")
        cmd, args = tokens[0], tokens[1:]
        if cmd not in COMMANDS:
            return err("invalid_args",
                       f"不支持的命令：{cmd}。白名单（只读）：{', '.join(COMMANDS)}")
        if cmd == "pwd" and args:
            return err("invalid_args", "pwd 不接受参数")

        allowed = COMMANDS[cmd]
        checked: list[str] = []
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("-"):
                if tok not in allowed:
                    return err("invalid_args",
                               f"不允许的选项：{tok}。{cmd} 仅支持：{allowed or '（无）'}")
                checked.append(tok)
                if cmd == "head" and tok == "-n":
                    if i + 1 >= len(args) or not args[i + 1].isdigit():
                        return err("invalid_args",
                                   "head -n 需要一个正整数，如：head -n 5 news/xxx.jsonl")
                    checked.append(args[i + 1])
                    i += 1
            else:
                # 路径参数：过与文件工具相同的边界检查；
                # 通过后按原样传递（相对 token + cwd=工作目录，避免路径形态转换问题）
                resolved, msg = resolve_inside_workdir(workdir, tok)
                if resolved is None:
                    return err("permission_denied", msg)
                if not resolved.exists():
                    return err("not_found", f"路径不存在：{tok}")
                checked.append(tok)
            i += 1

        bin_dir = _find_bin_dir()
        if bin_dir is None:
            return err("execution_failed",
                       "未找到命令二进制目录：请安装 Git for Windows，或设置环境变量 "
                       "TOOL_BIN_DIR 指向含 ls.exe 的 usr/bin 目录")
        exe = _exe_path(bin_dir, cmd)
        if exe is None:
            return err("execution_failed", f"在 {bin_dir} 下找不到命令：{cmd}")
        try:
            proc = subprocess.run([str(exe), *checked], shell=False, cwd=str(workdir),
                                  capture_output=True, timeout=config.TOOL_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return err("limit_exceeded", f"命令执行超过 {config.TOOL_TIMEOUT_S} 秒被终止")
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            detail = (stderr.strip() or stdout.strip()
                      or f"退出码 {proc.returncode}")[:500]
            return err("execution_failed",
                       f"命令失败（退出码 {proc.returncode}）：{detail}")
        truncated = len(stdout) > config.TOOL_OUTPUT_LIMIT
        text = stdout[:config.TOOL_OUTPUT_LIMIT] + ("…[输出过长已截断]" if truncated else "")
        if stderr.strip():
            text += "\n[stderr] " + stderr.strip()[:200]
        return ok({"command": command, "output": text, "returncode": proc.returncode})

    return Tool(
        name="bash",
        description=("执行受限的白名单只读命令：pwd / ls / head。不经过 shell："
                     "不支持管道、重定向、变量与任何其他命令。路径参数使用相对"
                     "工作目录的相对路径（正斜杠），如 news/xxx.jsonl。"),
        parameters={"type": "object",
                    "properties": {"command": {"type": "string",
                                               "description": "要执行的命令，如 ls 或 head -n 5 news/xxx.jsonl"}},
                    "required": ["command"]},
        handler=handler,
    )
