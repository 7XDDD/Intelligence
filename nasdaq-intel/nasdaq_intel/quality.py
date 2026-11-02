"""数据质量校验：在生成报告前检查口径、完整性和异常值。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def validate_snapshot(snapshot: dict[str, Any], *, expected_count: int = 10) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    companies = snapshot.get("companies") or []
    if snapshot.get("status") != "ok":
        issues.append(_issue("warning", "ranking_incomplete", "市值排名候选池不完整"))
    if len(companies) != expected_count:
        issues.append(
            _issue("warning", "ranking_count", f"返回 {len(companies)} 家公司，预期 {expected_count} 家")
        )
    tickers = [c.get("ticker") for c in companies]
    if len(set(tickers)) != len(tickers):
        issues.append(_issue("error", "duplicate_ticker", "市值排名出现重复代码"))
    for company in companies:
        ticker = company.get("ticker") or ""
        if not TICKER_RE.match(ticker):
            issues.append(_issue("error", "invalid_ticker", f"代码格式异常：{ticker}"))
        if not company.get("market_cap") or company["market_cap"] <= 0:
            issues.append(_issue("error", "invalid_market_cap", f"{ticker} 市值缺失或非正数"))
    return issues


def validate_materials(
    items: list[dict[str, Any]],
    *,
    covered_tickers: set[str],
    day: date,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    duplicates = 0
    for item in items:
        ticker = (item.get("ticker") or "").upper()
        if ticker not in covered_tickers:
            issues.append(_issue("error", "unexpected_ticker", f"采集到名单外公司：{ticker}"))
        for field in ("title", "url", "kind", "published_date"):
            if not item.get(field):
                issues.append(_issue("warning", "missing_field", f"{ticker} 材料缺少字段 {field}"))
        url = item.get("url") or ""
        if url and not url.startswith(("http://", "https://")):
            issues.append(_issue("warning", "invalid_url", f"{ticker} 来源链接格式异常"))
        if url:
            if url in seen_urls:
                duplicates += 1
            seen_urls.add(url)
        published = item.get("published_date")
        if published:
            try:
                published_date = date.fromisoformat(published)
            except ValueError:
                issues.append(_issue("warning", "invalid_date", f"{ticker} 发布时间格式异常：{published}"))
                continue
            if published_date > day + timedelta(days=1):
                issues.append(
                    _issue("warning", "future_date", f"{ticker} 发布时间晚于报告日：{published}")
                )
    if items and duplicates / len(items) > 0.15:
        issues.append(
            _issue("warning", "high_duplicate_ratio", f"原始材料重复链接比例 {duplicates / len(items):.0%}")
        )
    return issues


def validate_market(snapshots: list[dict[str, Any]], *, day: date, today: date) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for snap in snapshots:
        ticker = snap.get("ticker") or ""
        current = snap.get("current_price")
        previous = snap.get("previous_close")
        close = snap.get("daily_close")
        historical = bool(snap.get("historical"))
        if not historical and (current is None or current <= 0):
            issues.append(_issue("error", "invalid_price", f"{ticker} 最新价缺失或非正数"))
        if previous is not None and previous <= 0:
            issues.append(_issue("error", "invalid_prev_close", f"{ticker} 前收盘价非正数"))
        if day < today and close is None:
            issues.append(_issue("warning", "missing_daily_close", f"{ticker} 缺少 {day} 收盘价"))
        if close is not None:
            if close <= 0:
                issues.append(_issue("error", "invalid_close", f"{ticker} 收盘价非正数"))
            if snap.get("price_date") != day.isoformat():
                issues.append(
                    _issue("error", "close_date_mismatch", f"{ticker} 收盘价日期与报告日不一致")
                )
        stamp = snap.get("quote_timestamp")
        if stamp and not historical:
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                issues.append(_issue("warning", "invalid_quote_time", f"{ticker} 行情快照时间格式异常"))
            else:
                if parsed.date() > today:
                    issues.append(_issue("error", "future_quote_time", f"{ticker} 行情时间晚于今天"))
        if snap.get("change_basis") == "close" and close is not None and previous:
            expected = round(close - previous, 2)
            if snap.get("change") is not None and abs(snap["change"] - expected) > 0.02:
                issues.append(_issue("error", "change_mismatch", f"{ticker} 日涨跌与收盘价不一致"))
    return issues


def validate_report(markdown: str, required_sections: list[str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for section in required_sections:
        if section not in markdown:
            issues.append(_issue("error", "missing_section", f"日报缺少章节：{section}"))
    if "生成时间" not in markdown:
        issues.append(_issue("warning", "missing_footer", "日报缺少生成时间"))
    return issues
