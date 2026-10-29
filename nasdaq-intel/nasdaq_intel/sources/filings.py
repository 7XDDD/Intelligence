"""SEC EDGAR 官方披露采集：8-K 重大事项、10-Q / 10-K 定期报告、6-K 外国发行人报告。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..http import HttpClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

FORM_LABELS = {
    "8-K": "8-K 重大事项公告",
    "8-K/A": "8-K 重大事项公告（修订）",
    "10-Q": "10-Q 季度报告",
    "10-K": "10-K 年度报告",
    "6-K": "6-K 外国发行人报告",
}
FORM_CATEGORY_HINT = {"10-Q": "financial", "10-K": "financial"}


def load_cik_map(http: HttpClient, cache_path, max_age_days: int = 30) -> dict[str, str]:
    """ticker -> 10 位 CIK，带本地缓存，避免每次请求 SEC。"""
    if cache_path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            cache_path.stat().st_mtime, tz=timezone.utc
        )
        if age < timedelta(days=max_age_days):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached:
                    return cached
            except ValueError:
                pass

    raw = http.get_json(TICKERS_URL)
    mapping: dict[str, str] = {}
    for entry in (raw or {}).values():
        ticker = str(entry.get("ticker", "")).upper()
        cik = int(entry.get("cik_str", 0))
        if ticker and cik:
            mapping[ticker] = f"{cik:010d}"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def fetch_filings(
    http: HttpClient,
    *,
    cik: str,
    ticker: str,
    company_name: str,
    forms: list[str],
    lookback_start: date,
    max_items: int,
) -> list[dict[str, Any]]:
    raw = http.get_json(SUBMISSIONS_URL.format(cik=cik))
    recent = (raw or {}).get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber") or []
    form_list = recent.get("form") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    descriptions = recent.get("primaryDocDescription") or []

    form_set = {f.upper() for f in forms}
    items: list[dict[str, Any]] = []
    for idx, accession in enumerate(accessions):
        form = (form_list[idx] if idx < len(form_list) else "").strip()
        if form.upper() not in form_set:
            continue
        filed = dates[idx] if idx < len(dates) else ""
        try:
            filed_date = date.fromisoformat(filed)
        except ValueError:
            continue
        if filed_date < lookback_start:
            continue
        doc = docs[idx] if idx < len(docs) else ""
        description = descriptions[idx] if idx < len(descriptions) else ""
        if doc:
            url = ARCHIVE_URL.format(cik=str(int(cik)), accession=accession.replace("-", ""), doc=doc)
        else:
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
        items.append(
            {
                "id": f"sec-{accession}",
                "ticker": ticker,
                "company_name": company_name,
                "kind": "filing",
                "source_name": "SEC EDGAR",
                "publisher": "SEC",
                "title": f"{company_name} · {FORM_LABELS.get(form.upper(), form)}",
                "summary": (
                    f"{company_name} 于 {filed} 向 SEC 提交 {form} 文件"
                    + (f"（{description}）" if description and description.upper() != form.upper() else "")
                    + "。"
                ),
                "url": url,
                "published_date": filed,
                "published_precision": "date",
                "category_hint": FORM_CATEGORY_HINT.get(form.upper()),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(items) >= max_items:
            break
    return items


def collect_filings(
    http: HttpClient,
    *,
    companies: list[dict[str, Any]],
    cik_map: dict[str, str],
    lookback_start: date,
    sec_forms: list[str],
    max_per_company: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for company in companies:
        cik = cik_map.get(company["ticker"])
        if not cik:
            errors.append(f"{company['ticker']} 未找到 CIK 映射，跳过 SEC 申报")
            continue
        try:
            items.extend(
                fetch_filings(
                    http,
                    cik=cik,
                    ticker=company["ticker"],
                    company_name=company["company_name"],
                    forms=sec_forms,
                    lookback_start=lookback_start,
                    max_items=max_per_company,
                )
            )
        except Exception as exc:
            errors.append(f"{company['ticker']} SEC 申报获取失败：{exc}")
    return items, errors
