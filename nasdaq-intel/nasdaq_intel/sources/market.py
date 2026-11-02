"""行情采集：把「最新价、前收盘、指定交易日收盘价」拆成独立字段，避免口径混淆。

数据源（免费，无需 API Key）：
- https://api.nasdaq.com/api/quote/{symbol}/info      最新行情 + 市场状态
- https://api.nasdaq.com/api/quote/{symbol}/historical 历史日线（含收盘价）
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..http import HttpClient
from ..market_calendar import TRADING_TZ, market_phase, ny_today, session_label

QUOTE_URL = "https://api.nasdaq.com/api/quote/{symbol}/info"
HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
SOURCE = "api.nasdaq.com/api/quote"

_NUMBER_RE = re.compile(r"[-+]?[0-9,]*\.?[0-9]+")


def parse_number(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).replace("$", "").replace("%", "").replace(",", "").strip()
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_quote_time(raw: str | None) -> str | None:
    """'Nov 2, 2026 10:45 AM ET' → 带时区的 ISO 时间。"""
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw.strip(), "%b %d, %Y %I:%M %p ET")
    except ValueError:
        return None
    return parsed.replace(tzinfo=TRADING_TZ).isoformat()


def parse_history_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _cache_path(cache_dir: Path, day: date, ticker: str) -> Path:
    # v2：历史日期与盘中快照分字段存储；旧缓存缺少 historical 标记，直接换目录避免误用
    return Path(cache_dir) / "market-v2" / day.isoformat() / f"{ticker}.json"


def _load_cached(path: Path, *, allow_open: bool, max_age_seconds: int = 900) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if payload.get("market_status") == "Open" and not allow_open:
        return None
    fetched_at = payload.get("fetched_at")
    if payload.get("market_status") == "Open" and fetched_at:
        try:
            age = time.time() - datetime.fromisoformat(fetched_at).timestamp()
        except ValueError:
            return None
        if age > max_age_seconds:
            return None
    return payload


def fetch_quote(http: HttpClient, ticker: str) -> dict[str, Any]:
    raw = http.get_json(QUOTE_URL.format(symbol=ticker), params={"assetclass": "stocks"})
    return ((raw or {}).get("data") or {})


def fetch_history(http: HttpClient, ticker: str, day: date, days: int = 45) -> list[dict[str, Any]]:
    params = {
        "assetclass": "stocks",
        "fromdate": (day - timedelta(days=days)).isoformat(),
        "todate": day.isoformat(),
        "limit": str(days + 15),
    }
    raw = http.get_json(HISTORY_URL.format(symbol=ticker), params=params)
    rows = (((raw or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        row_date = parse_history_date(row.get("date"))
        if not row_date:
            continue
        parsed.append(
            {
                "date": row_date,
                "close": parse_number(row.get("close")),
                "open": parse_number(row.get("open")),
                "high": parse_number(row.get("high")),
                "low": parse_number(row.get("low")),
                "volume": parse_number(row.get("volume")),
            }
        )
    return sorted(parsed, key=lambda item: item["date"], reverse=True)


def build_snapshot(
    http: HttpClient,
    *,
    company: dict[str, Any],
    day: date,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    ticker = company["ticker"]
    today_ny = ny_today()
    historical = day < today_ny
    cache_file = _cache_path(cache_dir, day, ticker) if cache_dir else None
    if cache_file:
        cached = _load_cached(cache_file, allow_open=True)
        if cached:
            return cached

    if historical:
        # 历史日期报告只展示该交易日收盘价，不把当前盘中价混进历史报告
        quote: dict[str, Any] = {}
        primary: dict[str, Any] = {}
        secondary: dict[str, Any] = {}
        status = "Historical"
    else:
        quote = fetch_quote(http, ticker)
        primary = quote.get("primaryData") or {}
        secondary = quote.get("secondaryData") or {}
        status = str(quote.get("marketStatus") or "").strip() or "Unknown"
    history = fetch_history(http, ticker, day)

    current_price = None if historical else parse_number(primary.get("lastSalePrice"))
    quote_time = None if historical else parse_quote_time(primary.get("lastTradeTimestamp"))

    daily_row = next((row for row in history if row["date"] == day), None)
    prior_rows = [row for row in history if row["date"] < day]
    daily_close = daily_row["close"] if daily_row else None
    previous_close = prior_rows[0]["close"] if prior_rows else parse_number(secondary.get("lastSalePrice"))

    warnings: list[str] = []
    if historical:
        warnings.append(f"历史日期报告，仅展示 {day.isoformat()} 收盘价")
    if day < today_ny and daily_close is None:
        warnings.append(f"{ticker} 缺少 {day.isoformat()} 的日线收盘价")
    if day > today_ny:
        warnings.append(f"报告日期 {day.isoformat()} 晚于美东今天 {today_ny.isoformat()}")

    change = None
    change_pct = None
    basis = "none"
    if daily_close is not None and previous_close:
        change = round(daily_close - previous_close, 2)
        change_pct = round(change / previous_close * 100, 2)
        basis = "close"
    elif current_price is not None and previous_close:
        change = round(current_price - previous_close, 2)
        change_pct = round(change / previous_close * 100, 2)
        basis = "intraday"

    phase = "closed" if historical else market_phase()

    snapshot = {
        "ticker": ticker,
        "company_name": company.get("company_name") or ticker,
        "current_price": current_price,
        "previous_close": previous_close,
        "daily_close": daily_close,
        "price_date": day.isoformat() if daily_close is not None else None,
        "quote_timestamp": quote_time,
        "quote_timestamp_display": primary.get("lastTradeTimestamp"),
        "market_status": status,
        "market_phase": phase,
        "market_phase_label": session_label(phase),
        "historical": historical,
        "change": change,
        "change_pct": change_pct,
        "change_basis": basis,
        "volume": parse_number(primary.get("volume")),
        "currency": quote.get("currency") or primary.get("currency") or "USD",
        "source": SOURCE,
        "fetched_at": datetime.now(TRADING_TZ).isoformat(),
        "warnings": warnings,
    }
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def collect_market(
    http: HttpClient,
    *,
    companies: list[dict[str, Any]],
    day: date,
    cache_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    snapshots: list[dict[str, Any]] = []
    errors: list[str] = []
    for company in companies:
        try:
            snapshots.append(build_snapshot(http, company=company, day=day, cache_dir=cache_dir))
        except Exception as exc:
            errors.append(f"{company.get('ticker')} 行情获取失败：{exc}")
    return snapshots, errors
