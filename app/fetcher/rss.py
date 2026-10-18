"""RSS 抓取与确定性数据清洗（design.md §4 / §10.9 / §10.11 / D25）。

固定步骤，非 Agent 决策。清洗顺序（全部在采集层确定性完成）：
解析 → 时间统一为 Asia/Shanghai → 新鲜度窗口过滤 → 保守 URL 归一化去重 → 分配 news_id。

- 部分源失败继续并在报告中记录；全部源失败或窗口内无有效新闻抛 FetchError
  （绝不静默回退到样例数据，D13）；
- 缺发布时间的条目无法判断时效，一律排除并在报告中计数；
- 必须显式带浏览器 User-Agent：量子位、Solidot 对默认 UA 返回 403（实测）。
"""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser

from app import config

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 news-agent-demo/1.0")
FETCH_TIMEOUT_S = 15
MAX_ITEMS_PER_SOURCE = 12
MAX_FEED_BYTES = 5_000_000
SUMMARY_LIMIT = 400

_TZ = ZoneInfo(config.TIMEZONE)
_TAG_RE = re.compile(r"<[^>]+>")
# 保守的跟踪参数名单：只删"确定与内容无关"的参数，
# 其余查询参数一律保留（部分新闻站的参数本身是文章标识）
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "fbclid", "gclid", "yclid", "spm",
                    "mc_cid", "mc_eid", "igshid", "ref_src"}


class FetchError(RuntimeError):
    """新闻材料准备失败（全部源失败 / 窗口内无有效新闻）。"""


def source_name(url: str) -> str:
    """从 URL 提取来源文件名（域名，去 www.，仅保留安全字符）。"""
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    name = re.sub(r"[^a-z0-9.-]+", "", netloc) or "source"
    return name


def _clean_summary(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    return " ".join(text.split())[:SUMMARY_LIMIT]


def _parse_time(entry) -> tuple[str, float | None]:
    """返回 (Asia/Shanghai 显示时间, epoch 秒)；无可用时间返回 ("", None)。

    feedparser 的 *_parsed 是 UTC，统一转换为 Asia/Shanghai 再输出；
    只有无法解析的原始字符串时视为"无发布时间"（无法可靠判断时效）。
    """
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(_TZ)
        return dt.strftime("%Y-%m-%d %H:%M"), dt.timestamp()
    return "", None


def parse_feed_bytes(data: bytes) -> list[dict]:
    """解析 RSS/Atom 字节流为规范条目。

    _published_ts 为内部键（供过滤/排序），落盘前由 collect_materials 剥离。
    """
    feed = feedparser.parse(data)
    if feed.bozo and not feed.entries:
        raise FetchError(f"RSS 解析失败：{feed.bozo_exception}")
    items: list[dict] = []
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = (e.get("title") or "").strip()
        url = (e.get("link") or "").strip()
        if not title or not url:
            continue
        published_at, ts = _parse_time(e)
        items.append({"title": title, "url": url,
                      "published_at": published_at,
                      "summary": _clean_summary(e.get("summary", "")),
                      "_published_ts": ts})
    return items


def _read_body(resp, limit: int) -> bytes:
    """读取响应体；超过上限抛错（该源记为失败，不解析残缺 XML）。"""
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise FetchError(f"响应体超过 {limit // 1_000_000}MB 上限")
    return data


def fetch_source(url: str) -> list[dict]:
    """抓取单个源（带浏览器 UA 与超时）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        data = _read_body(resp, MAX_FEED_BYTES)
    return parse_feed_bytes(data)


def normalize_url(url: str) -> str:
    """保守归一化：域名小写、去 fragment、仅删已知跟踪参数。

    不删除其他查询参数，不改路径——避免把参数本身就是文章标识的
    链接误判为重复。
    """
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in
                       parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in _TRACKING_PARAMS])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path, query, ""))


def collect_materials(urls: list[str], *, now: datetime,
                      freshness_hours: int) -> tuple[dict[str, list[dict]], dict]:
    """抓取 + 确定性清洗，返回 (来源名 → 带 news_id 的条目, 抓取报告)。

    now 取任务启动时间（Asia/Shanghai）；freshness_hours=0 关闭时效窗口
    （缺发布时间的条目仍排除）。
    """
    sources: list[dict] = []
    pool: list[dict] = []
    cutoff = (now - timedelta(hours=freshness_hours)) if freshness_hours > 0 else None

    for url in urls:
        name = source_name(url)
        rep = {"url": url, "source": name, "ok": False, "items": 0, "kept": 0,
               "stale_filtered": 0, "missing_date": 0, "error": ""}
        try:
            items = fetch_source(url)
        except Exception as e:  # 单源失败不影响其余源（D13）
            rep["error"] = f"{type(e).__name__}: {e}"
            sources.append(rep)
            continue
        rep["items"] = len(items)
        for it in items:
            ts = it.get("_published_ts")
            if ts is None:
                rep["missing_date"] += 1  # 无法判断时效，不进入材料
                continue
            if cutoff is not None and \
                    datetime.fromtimestamp(ts, tz=timezone.utc) < cutoff:
                rep["stale_filtered"] += 1
                continue
            it["_source"] = name
            pool.append(it)
        rep["ok"] = True
        rep["kept"] = rep["items"] - rep["missing_date"] - rep["stale_filtered"]
        sources.append(rep)

    # 跨源保守去重（首见保留）
    seen: set[str] = set()
    deduped: list[dict] = []
    duplicates = 0
    for it in pool:
        key = normalize_url(it["url"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(it)

    if not deduped:
        failed = [s for s in sources if not s["ok"]]
        if failed and len(failed) == len(sources):
            detail = "; ".join(f"{s['url']}: {s['error']}" for s in sources)
            raise FetchError(f"全部 RSS 源抓取失败（共 {len(sources)} 个）：{detail}")
        stale = sum(s["stale_filtered"] for s in sources)
        missing = sum(s["missing_date"] for s in sources)
        raise FetchError(
            f"窗口内无有效新闻：过期过滤 {stale} 条、缺发布时间 {missing} 条"
            + (f"，另有 {len(failed)} 个源抓取失败" if failed else ""))

    # 按发布时间倒序统一分配 news_id（N0001 起）
    deduped.sort(key=lambda it: it["_published_ts"], reverse=True)
    by_source: dict[str, list[dict]] = {}
    for i, it in enumerate(deduped, 1):
        source = it.pop("_source")
        it.pop("_published_ts", None)
        item = {**it, "news_id": f"N{i:04d}", "source": source}
        by_source.setdefault(source, []).append(item)

    report = {"sources": sources,
              "kept_total": len(deduped),
              "duplicates_removed": duplicates}
    return by_source, report
