"""RSS 采集层测试：时间统一、新鲜度窗口（含边界与缺时间）、保守去重、
超限失败语义、部分失败继续、演示模式不受影响（D13/D25）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.fetcher.rss as rss
from app.fetcher.rss import (FetchError, _read_body, collect_materials,
                              normalize_url, parse_feed_bytes, source_name)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Hello &amp; World</title><link>https://e.example/1</link>
<pubDate>Fri, 16 Oct 2026 09:00:00 GMT</pubDate>
<description>&lt;p&gt;A &lt;b&gt;summary&lt;/b&gt; here&lt;/p&gt;</description></item>
<item><title>No Link Item</title></item>
<item><title>No Date Item</title><link>https://e.example/2</link></item>
</channel></rss>"""


def _item(url, *, hours_ago=None, ts=None, title="t"):
    if ts is None and hours_ago is not None:
        ts = (NOW - timedelta(hours=hours_ago)).timestamp()
    return {"title": title, "url": url, "published_at": "2026-10-18 12:00",
            "summary": "s", "_published_ts": ts}


def _fake_fetch(mapping):
    def fetch(url):
        result = mapping[url]
        if isinstance(result, Exception):
            raise result
        return result
    return fetch


# —— 解析与时间统一 ——


def test_parse_feed_bytes_time_unified_to_shanghai():
    items = parse_feed_bytes(RSS_XML)
    assert len(items) == 2  # 无 link 的条目被跳过
    by_title = {i["title"]: i for i in items}
    dated = by_title["Hello & World"]
    assert dated["published_at"] == "2026-10-16 17:00"  # UTC 09:00 → +8h
    assert dated["_published_ts"] is not None
    assert "<" not in dated["summary"] and "summary" in dated["summary"]
    assert by_title["No Date Item"]["published_at"] == ""
    assert by_title["No Date Item"]["_published_ts"] is None


def test_parse_garbage_raises():
    with pytest.raises(FetchError):
        parse_feed_bytes(b"this is not a feed")


def test_source_name_from_url():
    assert source_name("https://TechCrunch.com/category/feed/") == "techcrunch.com"
    assert source_name("https://www.jiqizhixin.com/rss") == "jiqizhixin.com"


# —— 超限与归一化 ——


def test_read_body_over_limit_marks_failure():
    class FakeResp:
        def __init__(self, data):
            self._data = data

        def read(self, n):
            return self._data[:n]

    assert _read_body(FakeResp(b"x" * 10), 10) == b"x" * 10
    with pytest.raises(FetchError, match="上限"):
        _read_body(FakeResp(b"x" * 11), 10)


def test_normalize_url_conservative():
    # 域名小写、去 fragment、删跟踪参数
    assert normalize_url("https://Example.COM/a/b?utm_source=x&id=7#frag") == \
        "https://example.com/a/b?id=7"
    # 非跟踪参数保留（可能是文章标识），不改路径
    assert normalize_url("https://e.com/a?p=123&t=abc") == \
        "https://e.com/a?p=123&t=abc"


# —— 采集流水线：窗口 / 缺时间 / 去重 / 失败 ——


def test_collect_materials_window_and_missing_date(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://a.example/feed": [
            _item("https://a.example/1", hours_ago=1),     # 窗口内
            _item("https://a.example/2", hours_ago=47.9),  # 窗口内（边界内）
            _item("https://a.example/3", hours_ago=49),    # 过期
            _item("https://a.example/4", ts=None),         # 缺发布时间
        ],
    }))
    by_source, report = collect_materials(["https://a.example/feed"],
                                          now=NOW, freshness_hours=48)
    assert report["kept_total"] == 2
    rep = report["sources"][0]
    assert rep["stale_filtered"] == 1 and rep["missing_date"] == 1
    urls = [i["url"] for i in by_source["a.example"]]
    assert "https://a.example/1" in urls and "https://a.example/3" not in urls
    # 按发布时间倒序编号；内部键已剥离
    assert by_source["a.example"][0]["news_id"] == "N0001"
    assert by_source["a.example"][0]["url"] == "https://a.example/1"
    assert "_published_ts" not in by_source["a.example"][0]


def test_collect_materials_boundary_exactly_48h_kept(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://a.example/feed": [
            _item("https://a.example/1",
                  ts=(NOW - timedelta(hours=48)).timestamp())],
    }))
    by_source, report = collect_materials(["https://a.example/feed"],
                                          now=NOW, freshness_hours=48)
    assert report["kept_total"] == 1  # 恰好位于窗口边界视为保留


def test_collect_materials_window_off_keeps_stale_but_not_missing_date(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://a.example/feed": [
            _item("https://a.example/1", hours_ago=500),
            _item("https://a.example/2", ts=None),
        ],
    }))
    by_source, report = collect_materials(["https://a.example/feed"],
                                          now=NOW, freshness_hours=0)
    assert report["kept_total"] == 1
    rep = report["sources"][0]
    assert rep["stale_filtered"] == 0 and rep["missing_date"] == 1


def test_collect_materials_dedup_across_sources(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://a.example/feed": [
            _item("https://a.example/post?utm_source=x", hours_ago=1, title="first")],
        "https://b.example/feed": [
            _item("https://a.example/post#top", hours_ago=2, title="dup")],
    }))
    by_source, report = collect_materials(
        ["https://a.example/feed", "https://b.example/feed"],
        now=NOW, freshness_hours=48)
    assert report["duplicates_removed"] == 1
    assert report["kept_total"] == 1
    assert list(by_source) == ["a.example"]  # 首见保留
    assert by_source["a.example"][0]["title"] == "first"


def test_collect_materials_partial_failure_continues(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://live.example/feed": [_item("https://live.example/1", hours_ago=1)],
        "https://dead.example/feed": OSError("DNS fail"),
    }))
    by_source, report = collect_materials(
        ["https://live.example/feed", "https://dead.example/feed"],
        now=NOW, freshness_hours=48)
    assert set(by_source) == {"live.example"}
    dead = next(s for s in report["sources"] if not s["ok"])
    assert "DNS fail" in dead["error"]


def test_collect_materials_all_sources_failed(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source",
                        lambda url: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(FetchError, match="全部 RSS 源抓取失败"):
        collect_materials(["https://a.example/f"], now=NOW, freshness_hours=48)


def test_collect_materials_all_filtered_raises(monkeypatch):
    monkeypatch.setattr(rss, "fetch_source", _fake_fetch({
        "https://a.example/feed": [
            _item("https://a.example/1", hours_ago=100),
            _item("https://a.example/2", ts=None),
        ],
    }))
    with pytest.raises(FetchError, match="窗口内无有效新闻"):
        collect_materials(["https://a.example/feed"], now=NOW, freshness_hours=48)


# —— 与业务层的接口 ——


def test_prepare_workspace_rss_mode(tmp_path, monkeypatch):
    """真实模式落盘：每源一个 jsonl、news_id 注入、报告返回。"""
    from app import config
    from app.services import briefing

    monkeypatch.setattr(config.settings, "news_source", "rss")
    monkeypatch.setattr(briefing, "collect_materials",
                        lambda urls, now, freshness_hours: (
        {"live.example": [{"title": "t1", "url": "https://x/1",
                           "published_at": "2026-10-16 17:00",
                           "summary": "s1", "news_id": "N0001",
                           "source": "live.example"}]},
        {"sources": [{"url": urls[0], "source": "live.example", "ok": True,
                      "items": 1, "kept": 1, "stale_filtered": 0,
                      "missing_date": 0, "error": ""}],
         "kept_total": 1, "duplicates_removed": 0}))

    settings = {"name": "测试", "persona": "", "topics": ["AI"], "keywords": []}
    report = briefing._prepare_workspace(tmp_path, settings)

    files = sorted((tmp_path / "news").glob("*.jsonl"))
    assert [f.name for f in files] == ["live.example.jsonl"]
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert item["news_id"] == "N0001" and item["source"] == "live.example"
    assert (tmp_path / "preferences.md").exists()
    assert report["kept_total"] == 1


def test_prepare_workspace_rss_all_failed(tmp_path, monkeypatch):
    from app import config
    from app.services import briefing

    monkeypatch.setattr(config.settings, "news_source", "rss")
    monkeypatch.setattr(
        briefing, "collect_materials",
        lambda urls, now, freshness_hours: (_ for _ in ()).throw(
            FetchError("全部 RSS 源抓取失败")))
    with pytest.raises(FetchError):
        briefing._prepare_workspace(tmp_path, {"name": "t", "persona": "",
                                               "topics": [], "keywords": []})


def test_prepare_workspace_demo_mode_unaffected(tmp_path, monkeypatch):
    """演示模式复制 fixtures，完全不经过 RSS 清洗逻辑。"""
    from app import config
    from app.services import briefing

    monkeypatch.setattr(config.settings, "news_source", "demo")

    def boom(*args, **kwargs):
        raise AssertionError("演示模式不应调用 RSS 采集")
    monkeypatch.setattr(briefing, "collect_materials", boom)

    report = briefing._prepare_workspace(
        tmp_path, {"name": "t", "persona": "", "topics": [], "keywords": []})
    assert report is None
    assert len(list((tmp_path / "news").glob("*.jsonl"))) == 3
