"""RSS 抓取与规范化落盘（design.md §4 / §10.9 / §10.11）。

固定步骤，非 Agent 决策。逐源独立抓取：部分源失败继续并在报告中记录，
全部失败抛 FetchError（绝不静默回退到样例数据，D13）。
必须显式带浏览器 User-Agent：量子位、Solidot 对默认 UA 返回 403（实测）。
"""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import feedparser

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 news-agent-demo/1.0")
FETCH_TIMEOUT_S = 15
MAX_ITEMS_PER_SOURCE = 12
MAX_FEED_BYTES = 5_000_000
SUMMARY_LIMIT = 400

_TAG_RE = re.compile(r"<[^>]+>")


class FetchError(RuntimeError):
    """新闻材料准备失败（全部源失败 / 无可用条目）。"""


def source_name(url: str) -> str:
    """从 URL 提取来源文件名（域名，去 www.，仅保留安全字符）。"""
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    name = re.sub(r"[^a-z0-9.-]+", "", netloc) or "source"
    return name


def _clean_summary(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    return " ".join(text.split())[:SUMMARY_LIMIT]


def _parse_time(entry) -> str:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        return datetime(*st[:6]).strftime("%Y-%m-%d %H:%M")
    return (entry.get("published") or entry.get("updated") or "").strip()


def parse_feed_bytes(data: bytes) -> list[dict]:
    """解析 RSS/Atom 字节流为规范条目（与 fixtures 字段一致，除 news_id 外）。"""
    feed = feedparser.parse(data)
    if feed.bozo and not feed.entries:
        raise FetchError(f"RSS 解析失败：{feed.bozo_exception}")
    items: list[dict] = []
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = (e.get("title") or "").strip()
        url = (e.get("link") or "").strip()
        if not title or not url:
            continue
        items.append({"title": title, "url": url,
                      "published_at": _parse_time(e),
                      "summary": _clean_summary(e.get("summary", ""))})
    return items


def fetch_source(url: str) -> list[dict]:
    """抓取单个源（带浏览器 UA 与超时）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        data = resp.read(MAX_FEED_BYTES)
    return parse_feed_bytes(data)


def fetch_all(urls: list[str]) -> tuple[dict[str, list[dict]], list[dict]]:
    """逐源抓取。返回 (来源名 -> 条目列表, 抓取报告)；部分失败继续，全部失败抛错。"""
    by_source: dict[str, list[dict]] = {}
    reports: list[dict] = []
    for url in urls:
        name = source_name(url)
        try:
            items = fetch_source(url)
        except Exception as e:  # 单源失败不影响其余源（D13）
            reports.append({"url": url, "source": name, "ok": False,
                            "items": 0, "error": f"{type(e).__name__}: {e}"})
            continue
        reports.append({"url": url, "source": name, "ok": True,
                         "items": len(items), "error": ""})
        if items:
            by_source[name] = items
    if not by_source:
        detail = "; ".join(f"{r['url']}: {r['error'] or '无条目'}" for r in reports)
        raise FetchError(f"全部 RSS 源抓取失败或无条目（共 {len(urls)} 个）：{detail}")
    return by_source, reports


def assign_news_ids(by_source: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """跨源按发布时间倒序统一分配 news_id（N0001 起），写明 source 字段。"""
    flat: list[tuple[str, str, dict]] = []
    for src, items in by_source.items():
        for it in items:
            flat.append((it.get("published_at") or "", src, it))
    flat.sort(key=lambda t: t[0], reverse=True)
    out: dict[str, list[dict]] = {}
    for i, (_, src, it) in enumerate(flat, 1):
        out.setdefault(src, []).append({**it, "news_id": f"N{i:04d}", "source": src})
    return out
