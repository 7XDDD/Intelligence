"""新闻采集：纳斯达克个股新闻接口 + PR Newswire RSS 补充。"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

from ..http import HttpClient

NASDAQ_NEWS_URL = "https://api.nasdaq.com/api/news/topic/articlebysymbol"
NASDAQ_ARTICLE_BASE = "https://www.nasdaq.com"

# 公司名中不具区分度、参与匹配反而造成误报的词
_NAME_STOPWORDS = {
    "inc", "inc.", "corp", "corp.", "corporation", "company", "co", "co.", "ltd", "ltd.",
    "limited", "plc", "holdings", "holding", "group", "the", "of", "and", "nv", "n.v.",
    "sa", "s.a.", "technologies", "technology", "platforms", "systems", "solutions",
    "global", "international", "common", "class", "capital", "com",
}


def clean_text(raw: str | None, limit: int = 280) -> str:
    """去掉 HTML/CSS、通讯社电头、多余空白和新闻站点的模板前缀。"""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"^\s*key points\s*", "", text, flags=re.I)
    # 去掉 "(RTTNews) - "、"(Reuters) - " 这类电头
    text = re.sub(r"^\s*(?:\([A-Za-z0-9 .&/-]{2,30}\))\s*[-–]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _distinctive_tokens(company_name: str) -> list[str]:
    words = re.findall(r"[a-z0-9&]+", company_name.lower())
    return [w for w in words if len(w) >= 4 and w not in _NAME_STOPWORDS]


def _is_relevant(ticker: str, company_name: str, title: str, summary: str, primary: str) -> bool:
    """只保留与目标公司直接相关的报道，避免把大盘综述、他人持仓分析计入该公司。"""
    if primary and primary.lower() == ticker.lower():
        return True
    haystack = f"{title} {summary}".lower()
    if re.search(rf"\b{re.escape(ticker.lower())}\b", haystack):
        return True
    tokens = _distinctive_tokens(company_name)
    return any(re.search(rf"\b{re.escape(tok)}\b", haystack) for tok in tokens)


def _parse_created(value: str | None) -> date | None:
    """纳斯达克接口的 created 字段形如 'Oct 29, 2026'。"""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def fetch_nasdaq_news(
    http: HttpClient,
    *,
    ticker: str,
    company_name: str,
    lookback_start: date,
    limit: int = 30,
) -> list[dict[str, Any]]:
    raw = http.get_json(
        NASDAQ_NEWS_URL,
        params={"q": f"{ticker}|stocks", "offset": 0, "limit": limit, "fallback": "true"},
    )
    rows = ((raw or {}).get("data") or {}).get("rows") or []

    items: list[dict[str, Any]] = []
    for row in rows:
        published = _parse_created(row.get("created"))
        if published and published < lookback_start:
            continue
        primary = str(row.get("primarysymbol") or "").lower()
        title = clean_text(row.get("title"), limit=200)
        summary = clean_text(row.get("description"))
        if not _is_relevant(ticker, company_name, title, summary, primary):
            continue
        url = row.get("url") or ""
        if url and not url.startswith("http"):
            url = NASDAQ_ARTICLE_BASE + url
        items.append(
            {
                "id": f"nasdaq-news-{row.get('id') or abs(hash(url))}",
                "ticker": ticker,
                "company_name": company_name,
                "kind": "news",
                "source_name": f"Nasdaq News / {row.get('publisher') or 'unknown'}",
                "publisher": row.get("publisher") or "",
                "title": title,
                "summary": summary,
                "url": url,
                "published_date": published.isoformat() if published else None,
                "published_precision": "date",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return items


def fetch_prnewswire(
    http: HttpClient,
    *,
    companies: list[dict[str, Any]],
    lookback_start: date,
    rss_url: str,
) -> list[dict[str, Any]]:
    """PR Newswire 全量 RSS 中按公司名过滤，作为官方新闻稿补充。"""
    try:
        text = http.get_text(rss_url)
        root = ElementTree.fromstring(text)
    except Exception:
        return []

    names = [(c["ticker"], c["company_name"]) for c in companies]
    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = (node.findtext("description") or "").strip()
        pub_raw = (node.findtext("pubDate") or "").strip()
        published: date | None = None
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw).date()
            except Exception:
                published = None
        if published and published < lookback_start:
            continue
        haystack = f"{title} {desc}".lower()
        matched = None
        for ticker, company_name in names:
            token = re.sub(r"[^a-z0-9 ]", " ", company_name.lower()).strip()
            if token and token in haystack:
                matched = (ticker, company_name)
                break
        if not matched:
            continue
        items.append(
            {
                "id": f"prnewswire-{abs(hash(link))}",
                "ticker": matched[0],
                "company_name": matched[1],
                "kind": "press_release",
                "source_name": "PR Newswire",
                "publisher": "PR Newswire",
                "title": clean_text(title, limit=200),
                "summary": clean_text(desc, limit=400),
                "url": link,
                "published_date": published.isoformat() if published else None,
                "published_precision": "datetime",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(items) >= 40:
            break
    return items


def collect_news(
    http: HttpClient,
    *,
    companies: list[dict[str, Any]],
    lookback_start: date,
    collect_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    providers = collect_cfg.get("news_providers") or ["nasdaq_news"]
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    if "nasdaq_news" in providers:
        per_company = int(collect_cfg.get("max_news_per_company", 25))
        limit = int(collect_cfg.get("news_limit_per_request", 30))
        for company in companies:
            try:
                found = fetch_nasdaq_news(
                    http,
                    ticker=company["ticker"],
                    company_name=company["company_name"],
                    lookback_start=lookback_start,
                    limit=limit,
                )
                items.extend(found[:per_company])
            except Exception as exc:
                errors.append(f"{company['ticker']} 新闻获取失败：{exc}")

    if "prnewswire_rss" in providers:
        try:
            items.extend(
                fetch_prnewswire(
                    http,
                    companies=companies,
                    lookback_start=lookback_start,
                    rss_url=collect_cfg.get("prnewswire_rss"),
                )
            )
        except Exception as exc:
            errors.append(f"PR Newswire RSS 获取失败：{exc}")

    return items, errors


def lookback_start_from(now: datetime, hours: int) -> date:
    return (now - timedelta(hours=hours)).date()
