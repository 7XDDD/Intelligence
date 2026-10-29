"""市值排名：纳斯达克交易所上市公司 → 按总市值排序 → 公司级去重 → Top N 快照。

数据源（免费、无需 API Key）：
1. https://api.nasdaq.com/api/screener/stocks  —— 纳斯达克官方筛选器，返回 code/marketCap/sector 等
2. https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt —— 官方证券目录，用于识别 ETF / 测试证券
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from ..http import HttpClient

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SYMBOL_DIR_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"

# 需要剔除的非普通股证券（权证、单元、优先股等不构成“公司市值”口径）
_EXCLUDED_TITLE_PATTERNS = [
    r"\bwarrant", r"\bunit(s)?\b", r"\bright(s)?\b", r"\bpreferred\b", r"\bnotes?\b",
    r"\bdepositary shares\b", r"\bsubordinated\b", r"\bconvertible\b", r"\bdebenture",
]
_CLASS_SUFFIX_PATTERNS = [
    r"\bclass\s+[a-z]\b", r"\bclass\s+[a-z]\s+common\s+stock\b", r"\bcommon\s+stock\b",
    r"\bordinary\s+shares?\b", r"\bcapital\s+stock\b", r"\bamerican\s+depositary\s+shares?\b",
    r"\bads\b", r"\badr\b", r"\bregistered\s+shares?\b", r"\bnew\b", r"\bthe\b",
]
_LEGAL_SUFFIX_PATTERNS = [
    r",?\s+inc\.?$", r",?\s+incorporated$", r",?\s+corp\.?$", r",?\s+corporation$",
    r",?\s+company$", r",?\s+co\.?$", r",?\s+ltd\.?$", r",?\s+limited$", r",?\s+plc$",
    r",?\s+holdings?$", r",?\s+group$", r",?\s+n\.?v\.?$", r",?\s+s\.?a\.?$",
]


def normalize_company_name(raw: str) -> str:
    """把 'Alphabet Inc. Class A Common Stock' 归一为 'alphabet'，用于多股类合并。"""
    name = raw.lower()
    name = re.sub(r"^\s*the\s+", "", name)
    for pat in _EXCLUDED_TITLE_PATTERNS:
        if re.search(pat, name):
            return ""  # 非普通股，不进入公司级排名
    for pat in _CLASS_SUFFIX_PATTERNS:
        name = re.sub(pat, " ", name)
    name = re.sub(r"[^a-z0-9& ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    for pat in _LEGAL_SUFFIX_PATTERNS:
        name = re.sub(pat, "", name).strip()
    return name


def display_company_name(raw: str) -> str:
    """去掉证券类型后缀，保留公司名：'Alphabet Inc. Class A Common Stock' → 'Alphabet Inc.'"""
    name = re.sub(r"\s+", " ", raw).strip()
    suffixes = [
        r"class\s+[a-z]\s+common\s+stock", r"class\s+[a-z]\s+capital\s+stock",
        r"class\s+[a-z]\s+ordinary\s+shares?", r"class\s+[a-z]", r"common\s+stock",
        r"capital\s+stock", r"ordinary\s+shares?", r"american\s+depositary\s+shares?",
        r"registered\s+shares?", r"depositary\s+shares?", r"new",
    ]
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            new_name = re.sub(rf",?\s+{suffix}\s*$", "", name, flags=re.I)
            if new_name != name:
                name, changed = new_name, True
    return name.strip(" ,") or raw


def _parse_market_cap(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").replace("$", "").strip()
    try:
        cap = float(cleaned)
    except ValueError:
        return None
    return cap if cap > 0 else None


def fetch_symbol_directory(http: HttpClient) -> dict[str, dict[str, Any]]:
    """解析官方证券目录，返回 symbol -> {name, etf, test_issue}。"""
    text = http.get_text(SYMBOL_DIR_URL)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("证券目录为空")
    header = lines[0].split("|")
    out: dict[str, dict[str, Any]] = {}
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) < len(header):
            continue
        row = dict(zip(header, cols))
        symbol = row.get("Symbol", "").strip()
        if not symbol:
            continue
        out[symbol] = {
            "name": row.get("Security Name", "").strip(),
            "etf": row.get("ETF", "N").strip().upper() == "Y",
            "test_issue": row.get("Test Issue", "N").strip().upper() == "Y",
        }
    return out


def fetch_screener(http: HttpClient, exchange: str = "NASDAQ") -> list[dict[str, Any]]:
    raw = http.get_json(
        SCREENER_URL,
        params={
            "tableonly": "true",
            "limit": "10000",
            "offset": "0",
            "exchange": exchange,
            "download": "true",
        },
    )
    data = (raw or {}).get("data") or {}
    rows = data.get("rows") or []
    if not rows:
        raise ValueError("纳斯达克筛选器未返回任何数据")
    return rows


def build_snapshot(
    http: HttpClient,
    *,
    day: date,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    ranking_cfg = cfg["ranking"]
    top_n = int(ranking_cfg["top_n"])
    warnings: list[str] = []
    errors: list[str] = []

    directory: dict[str, dict[str, Any]] = {}
    try:
        directory = fetch_symbol_directory(http)
    except Exception as exc:  # 目录缺失不阻断主流程，但需标注
        errors.append(f"证券目录获取失败：{exc}")
        warnings.append("未取得官方证券目录，ETF/测试证券可能混入排名")

    rows = fetch_screener(http, exchange=ranking_cfg.get("exchange", "NASDAQ"))

    candidates: list[dict[str, Any]] = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        info = directory.get(symbol, {})
        if ranking_cfg.get("exclude_etf", True) and info.get("etf"):
            continue
        if ranking_cfg.get("exclude_test_issues", True) and info.get("test_issue"):
            continue
        cap = _parse_market_cap(row.get("marketCap"))
        if cap is None:
            continue
        display = re.sub(r"\s+", " ", (row.get("name") or info.get("name") or symbol)).strip()
        if any(re.search(pat, display.lower()) for pat in _EXCLUDED_TITLE_PATTERNS):
            continue
        candidates.append(
            {
                "ticker": symbol,
                "name": display,
                "market_cap": cap,
                "sector": row.get("sector") or "",
                "industry": row.get("industry") or "",
                "country": row.get("country") or "",
                "last_sale": row.get("lastsale") or "",
            }
        )

    if len(candidates) < int(ranking_cfg.get("min_universe_size", 3000)):
        warnings.append(
            f"候选池仅 {len(candidates)} 只证券，低于预期下限 "
            f"{ranking_cfg.get('min_universe_size')}，排名可能不完整"
        )

    # 公司级合并：同一公司多个股票代码合并市值
    companies: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = normalize_company_name(item["name"]) if ranking_cfg.get("merge_share_classes", True) else item["ticker"]
        if not key:
            key = item["ticker"].lower()
        if key not in companies:
            companies[key] = {
                "company_name": display_company_name(item["name"]),
                "market_cap": 0.0,
                "symbols": [],
                "sector": item["sector"],
                "industry": item["industry"],
                "country": item["country"],
            }
        entry = companies[key]
        if item["market_cap"] > entry["market_cap"]:
            # 用市值最大的一类股票名称作为公司代表名称
            entry["company_name"] = display_company_name(item["name"])
        entry["market_cap"] += item["market_cap"]
        entry["symbols"].append({"ticker": item["ticker"], "market_cap": item["market_cap"]})
        if not entry["sector"]:
            entry["sector"] = item["sector"]

    ranked = sorted(companies.values(), key=lambda c: c["market_cap"], reverse=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    top: list[dict[str, Any]] = []
    for idx, company in enumerate(ranked[:top_n], start=1):
        primary = max(company["symbols"], key=lambda s: s["market_cap"])
        top.append(
            {
                "rank": idx,
                "ticker": primary["ticker"],
                "tickers": [s["ticker"] for s in sorted(company["symbols"], key=lambda s: -s["market_cap"])],
                "company_name": company["company_name"],
                "market_cap": round(company["market_cap"], 2),
                "market_cap_display": format_cap(company["market_cap"]),
                "market_cap_timestamp": generated_at,
                "sector": company["sector"],
                "industry": company["industry"],
                "country": company["country"],
            }
        )

    status = "ok" if not warnings else "incomplete"
    return {
        "as_of_date": day.isoformat(),
        "generated_at": generated_at,
        "data_source": "api.nasdaq.com/api/screener/stocks + nasdaqtrader.com 证券目录",
        "exchange_filter": ranking_cfg.get("exchange", "NASDAQ"),
        "currency": "USD",
        "ranking_method": "market_cap_desc (company-level, share classes merged)",
        "universe_size": len(candidates),
        "company_count": len(ranked),
        "status": status,
        "warnings": warnings,
        "errors": errors,
        "companies": top,
    }


def format_cap(value: float) -> str:
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"
