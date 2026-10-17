"""文件工具：list_dir / read_file / search_content / write_file（design.md §7.3）。

路径约定（D7）：一律为相对工作目录的相对路径（正斜杠）；
绝对路径或越界路径返回 permission_denied，错误信息重申约定，
让模型看到错误后能自我纠正。
"""
from __future__ import annotations

import os
from pathlib import Path

from app import config
from app.agent.tools.base import Tool, err, ok

_SEARCH_MATCH_LIMIT = 50
_SEARCH_LINE_EXCERPT = 200


def resolve_inside_workdir(workdir: Path, rel: str) -> tuple[Path | None, str]:
    """解析相对路径并做目录边界检查。

    返回 (解析后的绝对路径, "") 或 (None, 错误消息)。
    realpath 解析符号链接后必须仍位于工作目录内（防符号链接逃逸）。
    """
    if not isinstance(rel, str) or not rel.strip():
        return None, "路径不能为空，请使用相对工作目录的相对路径，如 news/xxx.jsonl"
    p = Path(rel.strip())
    if p.is_absolute() or p.drive or p.root:
        return None, (f"不支持绝对路径：{rel}。请使用相对工作目录的相对路径"
                      "（正斜杠），如 news/xxx.jsonl")
    workdir_real = workdir.resolve()
    target_real = (workdir / p).resolve()
    w = os.path.normcase(str(workdir_real))
    t = os.path.normcase(str(target_real))
    if t != w and not t.startswith(w + os.sep):
        return None, (f"路径越界：{rel}。只能访问工作目录内的相对路径"
                      "（如 news/xxx.jsonl、preferences.md）")
    return target_real, ""


def _rel_display(workdir: Path, path: Path) -> str:
    try:
        return path.relative_to(workdir).as_posix()
    except ValueError:
        return path.as_posix()


def build_fs_tools(workdir: Path) -> list[Tool]:
    workdir = workdir.resolve()

    def list_dir(path: str = ".") -> dict:
        resolved, msg = resolve_inside_workdir(workdir, path or ".")
        if resolved is None:
            return err("permission_denied", msg)
        if not resolved.exists():
            return err("not_found", f"目录不存在：{path}")
        if not resolved.is_dir():
            return err("invalid_args", f"不是目录：{path}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            entries.append({"name": child.name,
                            "type": "dir" if child.is_dir() else "file",
                            "size": child.stat().st_size if child.is_file() else None})
        return ok({"path": path or ".", "entries": entries})

    def read_file(path: str) -> dict:
        resolved, msg = resolve_inside_workdir(workdir, path)
        if resolved is None:
            return err("permission_denied", msg)
        if not resolved.exists():
            return err("not_found", f"文件不存在：{path}")
        if resolved.is_dir():
            return err("invalid_args", f"是目录不是文件：{path}")
        text = resolved.read_text(encoding="utf-8", errors="replace")
        limit = config.TOOL_OUTPUT_LIMIT
        truncated = len(text) > limit
        return ok({"path": path,
                   "content": text[:limit] + ("\n…[内容过长已截断]" if truncated else ""),
                   "truncated": truncated})

    def search_content(keyword: str, dir: str = ".") -> dict:
        if not isinstance(keyword, str) or not keyword.strip():
            return err("invalid_args", "keyword 不能为空")
        resolved, msg = resolve_inside_workdir(workdir, dir or ".")
        if resolved is None:
            return err("permission_denied", msg)
        if not resolved.exists():
            return err("not_found", f"目录不存在：{dir}")
        if not resolved.is_dir():
            return err("invalid_args", f"不是目录：{dir}")
        kw = keyword.lower()
        matches: list[dict] = []
        scanned = 0
        truncated = False
        for f in sorted(resolved.rglob("*")):
            if not f.is_file():
                continue
            scanned += 1
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, 1):
                if kw in line.lower():
                    if len(matches) >= _SEARCH_MATCH_LIMIT:
                        truncated = True
                        break
                    matches.append({"file": _rel_display(workdir, f),
                                    "line": lineno,
                                    "text": line.strip()[:_SEARCH_LINE_EXCERPT]})
            if truncated:
                break
        return ok({"keyword": keyword, "dir": dir or ".",
                   "scanned_files": scanned, "match_count": len(matches),
                   "matches": matches,
                   "truncated": truncated})

    def write_file(path: str, content: str) -> dict:
        if not isinstance(content, str):
            return err("invalid_args", "content 必须是字符串")
        if len(content) > config.WRITE_LIMIT:
            return err("invalid_args",
                       f"内容过长（{len(content)} 字符，上限 {config.WRITE_LIMIT}）")
        resolved, msg = resolve_inside_workdir(workdir, path)
        if resolved is None:
            return err("permission_denied", msg)
        if resolved == workdir:
            return err("invalid_args", "path 不能是工作目录本身")
        if resolved.exists() and resolved.is_dir():
            return err("invalid_args", f"目标已是目录：{path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return ok({"path": path, "bytes_written": len(content.encode("utf-8"))})

    return [
        Tool(name="list_dir",
             description=("列出目录内容。path 为相对工作目录的相对路径（正斜杠），"
                          "默认 '.' 列出工作目录根。"),
             parameters={"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": []},
             handler=list_dir),
        Tool(name="read_file",
             description=("读取文本文件内容（UTF-8，超长自动截断）。"
                          "path 为相对工作目录的相对路径（正斜杠），如 news/xxx.jsonl。"),
             parameters={"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": ["path"]},
             handler=read_file),
        Tool(name="search_content",
             description=("在指定目录（含子目录）下按关键词做不区分大小写的全文搜索，"
                          "返回 文件/行号/内容片段。keyword 必填；dir 默认 '.'，"
                          "均为相对工作目录的相对路径。"),
             parameters={"type": "object",
                         "properties": {"keyword": {"type": "string"},
                                        "dir": {"type": "string"}},
                         "required": ["keyword"]},
             handler=search_content),
        Tool(name="write_file",
             description=("把文本内容写入文件（UTF-8，覆盖写，自动创建父目录）。"
                          "path 为相对工作目录的相对路径（正斜杠），如 briefing.md。"),
             parameters={"type": "object",
                         "properties": {"path": {"type": "string"},
                                        "content": {"type": "string"}},
                         "required": ["path", "content"]},
             handler=write_file),
    ]
