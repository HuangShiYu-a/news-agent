"""RSS 抓取层测试：解析、部分失败语义（D13）、news_id 分配、真实模式落盘。"""
from __future__ import annotations

import json

import pytest

import app.fetcher.rss as rss
from app.fetcher.rss import (FetchError, assign_news_ids, fetch_all,
                              parse_feed_bytes, source_name)

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Hello &amp; World</title><link>https://e.example/1</link>
<pubDate>Fri, 16 Oct 2026 09:00:00 GMT</pubDate>
<description>&lt;p&gt;A &lt;b&gt;summary&lt;/b&gt; here&lt;/p&gt;</description></item>
<item><title>No Link Item</title></item>
</channel></rss>"""


def test_parse_feed_bytes_strips_html_and_entities():
    items = parse_feed_bytes(RSS_XML)
    assert len(items) == 1  # 无 link 的条目被跳过
    assert items[0]["title"] == "Hello & World"
    assert items[0]["url"] == "https://e.example/1"
    assert items[0]["published_at"] == "2026-10-16 09:00"
    assert "<" not in items[0]["summary"] and "summary" in items[0]["summary"]


def test_parse_garbage_raises():
    with pytest.raises(FetchError):
        parse_feed_bytes(b"this is not a feed")


def test_source_name_from_url():
    assert source_name("https://TechCrunch.com/category/feed/") == "techcrunch.com"
    assert source_name("https://www.jiqizhixin.com/rss") == "jiqizhixin.com"


def test_fetch_all_partial_failure_continues(monkeypatch):
    def fake_fetch(url):
        if "dead" in url:
            raise OSError("DNS fail")
        return [{"title": "t", "url": "https://x/1",
                 "published_at": "2026-10-16 09:00", "summary": "s"}]
    monkeypatch.setattr(rss, "fetch_source", fake_fetch)
    by_source, reports = fetch_all(
        ["https://live.example/feed", "https://dead.example/feed"])
    assert set(by_source) == {"live.example"}
    assert len(reports) == 2
    dead = next(r for r in reports if not r["ok"])
    assert dead["source"] == "dead.example" and "DNS fail" in dead["error"]


def test_fetch_all_total_failure_raises(monkeypatch):
    def boom(url):
        raise OSError("down")
    monkeypatch.setattr(rss, "fetch_source", boom)
    with pytest.raises(FetchError, match="全部 RSS 源抓取失败"):
        fetch_all(["https://a.example/f", "https://b.example/f"])


def test_assign_news_ids_sorted_and_sequential():
    by_source = {
        "a.example": [{"title": "old", "url": "u1",
                       "published_at": "2026-10-15 08:00", "summary": ""}],
        "b.example": [{"title": "new", "url": "u2",
                       "published_at": "2026-10-16 09:00", "summary": ""}],
    }
    out = assign_news_ids(by_source)
    assert out["b.example"][0]["news_id"] == "N0001"  # 新发布的排前
    assert out["b.example"][0]["source"] == "b.example"
    assert out["a.example"][0]["news_id"] == "N0002"


def test_prepare_workspace_rss_mode(tmp_path, monkeypatch):
    """真实模式落盘：每源一个 jsonl、news_id 注入、fetch_report 返回部分失败。"""
    from app import config
    from app.services import briefing

    monkeypatch.setattr(config.settings, "news_source", "rss")

    def fake_fetch_all(urls):
        return ({"live.example": [
                    {"title": "t1", "url": "https://x/1",
                     "published_at": "2026-10-16 09:00", "summary": "s1"}]},
                [{"url": urls[0], "source": "live.example", "ok": True,
                  "items": 1, "error": ""},
                 {"url": urls[1], "source": "dead.example", "ok": False,
                  "items": 0, "error": "OSError: DNS fail"}])
    monkeypatch.setattr(briefing, "fetch_all", fake_fetch_all)

    settings = {"name": "测试", "persona": "", "topics": ["AI"], "keywords": []}
    report = briefing._prepare_workspace(tmp_path, settings)

    files = sorted((tmp_path / "news").glob("*.jsonl"))
    assert [f.name for f in files] == ["live.example.jsonl"]
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert item["news_id"] == "N0001" and item["source"] == "live.example"
    assert (tmp_path / "preferences.md").exists()
    assert any(not r["ok"] for r in report)  # 部分失败被记录


def test_prepare_workspace_rss_all_failed(tmp_path, monkeypatch):
    from app import config
    from app.services import briefing

    monkeypatch.setattr(config.settings, "news_source", "rss")
    monkeypatch.setattr(
        briefing, "fetch_all",
        lambda urls: (_ for _ in ()).throw(FetchError("全部 RSS 源抓取失败")))
    with pytest.raises(FetchError):
        briefing._prepare_workspace(tmp_path, {"name": "t", "persona": "",
                                               "topics": [], "keywords": []})
