"""产物校验器测试（design.md §9）。"""
from __future__ import annotations

import json

from app.services.briefing import validate_briefing


def make_workdir(tmp_path):
    run = tmp_path / "run"
    (run / "news").mkdir(parents=True)
    items = [
        {"news_id": "N0001", "url": "https://x/N0001", "title": "t1"},
        {"news_id": "N0002", "url": "https://x/N0002", "title": "t2"},
    ]
    (run / "news" / "a.jsonl").write_text(
        "\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n",
        encoding="utf-8")
    return run


def write_briefing(run, text):
    (run / "briefing.md").write_text(text, encoding="utf-8")


VALID = """# 简报
1. [N0001] 标题一
   链接：https://x/N0001
   摘要：一
2. [N0002] 标题二
   链接：https://x/N0002
   摘要：二
"""


def test_valid_briefing_passes(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, VALID)
    ok, errors = validate_briefing(run)
    assert ok, errors


def test_missing_file(tmp_path):
    run = make_workdir(tmp_path)
    ok, errors = validate_briefing(run)
    assert not ok and "不存在" in errors[0]


def test_empty_file(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, "   \n")
    ok, errors = validate_briefing(run)
    assert not ok and "空文件" in errors[0]


def test_unknown_news_id(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, "1. [N0099] 编造\n   链接：https://x/N0099\n")
    ok, errors = validate_briefing(run)
    assert not ok and any("N0099" in e for e in errors)


def test_duplicate_news_id(tmp_path):
    run = make_workdir(tmp_path)
    text = ("1. [N0001] a\n   链接：https://x/N0001\n"
            "2. [N0001] b\n   链接：https://x/N0001\n")
    write_briefing(run, text)
    ok, errors = validate_briefing(run)
    assert not ok and any("重复" in e for e in errors)


def test_url_mismatch(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, "1. [N0001] a\n   链接：https://wrong/N0001\n")
    ok, errors = validate_briefing(run)
    assert not ok and any("链接" in e for e in errors)


def test_swapped_urls_rejected(tmp_path):
    """交换两条新闻的链接：两个 URL 都出现在全文中，但各自不在自己的条目里。"""
    run = make_workdir(tmp_path)
    text = ("1. [N0001] a\n   链接：https://x/N0002\n"
            "2. [N0002] b\n   链接：https://x/N0001\n")
    write_briefing(run, text)
    ok, errors = validate_briefing(run)
    assert not ok and any("链接" in e for e in errors)


def test_missing_link_rejected(tmp_path):
    """条目只写 news_id 不写链接，应被拒绝。"""
    run = make_workdir(tmp_path)
    write_briefing(run, "1. [N0001] a\n   摘要：忘了写链接\n")
    ok, errors = validate_briefing(run)
    assert not ok and any("链接" in e for e in errors)


def test_prose_mention_does_not_break_entries(tmp_path):
    """正文中非条目位置提及 [news_id]，不应干扰条目校验（条目按结构提取）。"""
    run = make_workdir(tmp_path)
    text = ("# 简报\n\n延伸阅读：[N0002] 与本主题相关。\n\n"
            "1. [N0001] a\n   链接：https://x/N0001\n")
    write_briefing(run, text)
    ok, errors = validate_briefing(run)
    assert ok, errors


def test_no_match_phrase_allowed(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, "# 简报\n\n本期暂无匹配新闻。\n")
    ok, errors = validate_briefing(run)
    assert ok, errors


def test_no_ids_and_no_phrase(tmp_path):
    run = make_workdir(tmp_path)
    write_briefing(run, "# 简报\n\n今天没有想给你看的。\n")
    ok, errors = validate_briefing(run)
    assert not ok
