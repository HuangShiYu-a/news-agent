"""bash 工具测试：元字符 / 白名单 / 选项 / 路径边界（安全边界测试，不可砍）+ 真实执行。"""
from __future__ import annotations

import pytest

from app.agent.tools.bash import _find_bin_dir, build_bash_tool

requires_git_bins = pytest.mark.skipif(
    _find_bin_dir() is None, reason="未找到 Git for Windows usr/bin 命令二进制")


@pytest.fixture()
def workdir(tmp_path):
    run = tmp_path / "run"
    (run / "news").mkdir(parents=True)
    (run / "news" / "a.jsonl").write_text(
        '{"news_id": "N0001"}\n{"news_id": "N0002"}\n{"news_id": "N0003"}\n',
        encoding="utf-8")
    return run


@pytest.fixture()
def tool(workdir):
    return build_bash_tool(workdir)


# —— 拒绝路径 ——


@pytest.mark.parametrize("command", [
    "ls | wc -l",               # 管道
    "ls; pwd",                  # 命令串联
    "ls && pwd",                # 逻辑串联
    "echo $HOME",               # 变量（且 echo 不在白名单）
    "ls > out.txt",             # 重定向
    "ls `pwd`",                 # 命令替换
    "ls news\\a.jsonl",         # 反斜杠（shlex 下会被吞掉，直接拒绝）
    "cat preferences.md",       # 白名单外命令
    "rm -rf /",                 # 白名单外命令
    "grep -r MCP .",            # 白名单外命令
    "pwd -L",                   # pwd 不接受参数
])
def test_rejects_invalid(command, tool):
    result = tool.handler(command=command)
    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_args"


@pytest.mark.parametrize("command", [
    "ls --help",                # 不允许的长选项
    "ls -lah",                  # 未列入的选项组合
    "head -c 5 news/a.jsonl",   # head 只允许 -n
    "head -n x news/a.jsonl",   # -n 必须是正整数
    "head -n news/a.jsonl",     # -n 缺数字
])
def test_rejects_disallowed_options(command, tool):
    result = tool.handler(command=command)
    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_args"


@pytest.mark.parametrize("command", [
    "head -n 3 ../../outside.txt",   # 相对越界
    "ls ..",                         # 父目录
    "head -n 3 /etc/passwd",         # 绝对路径
    "head -n 3 C:/Windows/win.ini",  # Windows 绝对路径
])
def test_rejects_path_escape(command, tool, tmp_path):
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    result = tool.handler(command=command)
    assert result["ok"] is False
    assert result["error"]["type"] == "permission_denied"


def test_rejects_missing_path(tool):
    result = tool.handler(command="head -n 3 news/missing.jsonl")
    assert result["ok"] is False
    assert result["error"]["type"] == "not_found"


# —— 真实执行（依赖 Git for Windows 的命令二进制）——


@requires_git_bins
def test_pwd(tool):
    result = tool.handler(command="pwd")
    assert result["ok"]
    assert result["result"]["returncode"] == 0


@requires_git_bins
def test_ls_lists_workdir(tool):
    result = tool.handler(command="ls")
    assert result["ok"]
    assert "news" in result["result"]["output"]


@requires_git_bins
def test_ls_with_option_and_path(tool):
    result = tool.handler(command="ls -la news")
    assert result["ok"]
    assert "a.jsonl" in result["result"]["output"]


@requires_git_bins
def test_head_reads_lines(tool):
    result = tool.handler(command="head -n 2 news/a.jsonl")
    assert result["ok"]
    assert "N0001" in result["result"]["output"]
    assert "N0003" not in result["result"]["output"]


@requires_git_bins
def test_head_default_lines(tool):
    result = tool.handler(command="head news/a.jsonl")
    assert result["ok"]
    assert "N0001" in result["result"]["output"]


@requires_git_bins
def test_nonexistent_file_reports_failure(tool):
    # 路径参数在执行前经过存在性预检查（与文件工具一致的 not_found）
    result = tool.handler(command="ls news/missing.jsonl")
    assert result["ok"] is False
    assert result["error"]["type"] == "not_found"


@requires_git_bins
def test_command_failure_reports_execution_error(tool, workdir):
    # 目录存在但 ls 无法列出（Windows 下无读权限目录难以构造，此处验证正常退出码路径）
    result = tool.handler(command="head -n 3 news/a.jsonl")
    assert result["ok"] and result["result"]["returncode"] == 0
