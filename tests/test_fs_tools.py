"""文件工具测试：路径边界（D7）+ 各工具行为。"""
from __future__ import annotations

import pytest

from app.agent.tools.fs import build_fs_tools


@pytest.fixture()
def workdir(tmp_path):
    run = tmp_path / "run"
    (run / "news").mkdir(parents=True)
    (run / "preferences.md").write_text("# 订阅偏好\n\n- 姓名：测试\n", encoding="utf-8")
    (run / "news" / "a.jsonl").write_text(
        '{"news_id": "N0001", "title": "MCP toolkit", "url": "https://x/N0001"}\n',
        encoding="utf-8")
    return run


@pytest.fixture()
def tools(workdir):
    return {t.name: t for t in build_fs_tools(workdir)}


# —— 正常路径 ——


def test_list_dir_root(tools):
    result = tools["list_dir"].handler(path=".")
    assert result["ok"]
    names = {e["name"] for e in result["result"]["entries"]}
    assert names == {"news", "preferences.md"}


def test_read_file_roundtrip(tools):
    result = tools["read_file"].handler(path="preferences.md")
    assert result["ok"] and "订阅偏好" in result["result"]["content"]


def test_read_file_truncates(workdir, tools):
    (workdir / "big.txt").write_text("x" * 20_000, encoding="utf-8")
    result = tools["read_file"].handler(path="big.txt")
    assert result["ok"] and result["result"]["truncated"] is True


def test_write_file_creates(workdir, tools):
    result = tools["write_file"].handler(path="briefing.md", content="# 简报\n内容")
    assert result["ok"]
    assert (workdir / "briefing.md").read_text(encoding="utf-8") == "# 简报\n内容"


def test_write_file_creates_parent_dirs(workdir, tools):
    result = tools["write_file"].handler(path="out/sub/f.md", content="hi")
    assert result["ok"] and (workdir / "out" / "sub" / "f.md").exists()


def test_search_content_case_insensitive(tools):
    result = tools["search_content"].handler(keyword="mcp toolkit")
    assert result["ok"]
    assert result["result"]["match_count"] >= 1
    match = result["result"]["matches"][0]
    assert match["file"] == "news/a.jsonl"


def test_read_file_not_found(tools):
    result = tools["read_file"].handler(path="missing.md")
    assert result["ok"] is False and result["error"]["type"] == "not_found"


# —— 路径边界（安全边界测试，不可砍）——


@pytest.mark.parametrize("path", [
    "../outside.txt",           # 相对越界
    "..",                       # 父目录本身
    "news/../../escape.txt",    # 折叠后越界
    "/etc/passwd",              # POSIX 绝对路径
    "C:\\Windows\\win.ini",     # Windows 绝对路径（反斜杠）
    "C:/Windows/win.ini",       # Windows 绝对路径（正斜杠）
])
def test_path_escape_rejected(tools, tmp_path, path):
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    result = tools["read_file"].handler(path=path)
    assert result["ok"] is False
    assert result["error"]["type"] == "permission_denied"
    assert "相对" in result["error"]["message"]  # 错误信息重申约定


def test_write_escape_rejected_no_side_effect(tools, tmp_path):
    result = tools["write_file"].handler(path="../evil.txt", content="bad")
    assert result["ok"] is False
    assert not (tmp_path / "evil.txt").exists()


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    run = tmp_path / "run"
    (run / "news").mkdir(parents=True)
    link = run / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境无法创建符号链接（Windows 需开发者模式/管理员）")
    tools = {t.name: t for t in build_fs_tools(run)}
    result = tools["read_file"].handler(path="link.txt")
    assert result["ok"] is False
    assert result["error"]["type"] == "permission_denied"
