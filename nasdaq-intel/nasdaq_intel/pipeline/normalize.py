"""标准化与确定性去重。

规则（与方案一致，先确定性、再 AI 辅助）：
1. URL 规范化后相同 → 同一来源（合并）；
2. 同一公司、标题相似度 ≥ 阈值 → 疑似重复，归并为一个事件；
3. 同一事件保留多个独立来源；
4. 无法确认的候选标记 needs_verification，而不是直接删除。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "sid", "mod", "cmpid", "fbclid", "gclid", "mc_cid", "mc_eid",
}

# 来源权威层级：官方申报 > 官方新闻稿 > 媒体报道
KIND_WEIGHT = {"filing": 0, "press_release": 1, "news": 2}

# 公司名中没有区分度的词，不参与“哪家公司在标题里”的识别
_COMPANY_STOPWORDS = {
    "inc", "corp", "corporation", "company", "co", "ltd", "limited", "plc",
    "holdings", "holding", "group", "the", "of", "and", "new", "common", "class",
    "capital", "com", "technologies", "technology", "platforms", "systems",
    "solutions", "global", "international", "nv", "sa",
}

# 法定名与常用品牌名不一致时的补充别名（只做增量，不影响常规词匹配）
_COMPANY_ALIASES = {
    "alphabet": ["google"],
    "meta": ["facebook"],
    "space": ["spacex"],
    "amazon": ["aws"],
}

# 金额：英文 $4 billion / 中文 40亿美元、630亿
_AMOUNT_RE_EN = re.compile(
    r"\$\s?([0-9]+(?:\.[0-9]+)?)\s?(trillion|billion|million|bn|b|m)\b", re.I
)
_AMOUNT_RE_ZH = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万亿|亿|万)(?:美元|美金|元)?")


def extract_amounts(text: str) -> list[float]:
    """抽取文本中的金额，用于跨公司/跨语言比对同一事件。"""
    values: list[float] = []
    for amount, unit in _AMOUNT_RE_EN.findall(text or ""):
        number = float(amount)
        unit = unit.lower()
        if unit.startswith("t"):
            number *= 1e12
        elif unit in ("billion", "bn", "b"):
            number *= 1e9
        else:
            number *= 1e6
        values.append(number)
    for amount, unit in _AMOUNT_RE_ZH.findall(text or ""):
        number = float(amount)
        if unit == "万亿":
            number *= 1e12
        elif unit == "亿":
            number *= 1e8
        else:
            number *= 1e4
        values.append(number)
    return values


def _company_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return [w for w in words if len(w) >= 4 and w not in _COMPANY_STOPWORDS]


def build_company_lexicon(
    items: list[dict[str, Any]],
    companies: list[dict[str, Any]] | None = None,
) -> dict[str, set[str]]:
    """token（代码/公司名/品牌别名）→ 公司代码集合，用于识别标题里提到了哪些公司。"""
    lexicon: dict[str, set[str]] = {}

    def add(token: str, ticker: str) -> None:
        token = (token or "").lower().strip()
        if len(token) >= 2 and ticker:
            lexicon.setdefault(token, set()).add(ticker.upper())

    def add_company(ticker: str, name: str, symbols: list[str] | None = None) -> None:
        if not ticker:
            return
        for symbol in symbols or [ticker]:
            add(symbol, ticker)
        for token in _company_tokens(name):
            add(token, ticker)
            for alias in _COMPANY_ALIASES.get(token, []):
                add(alias, ticker)

    for item in items:
        add_company(item.get("ticker") or "", item.get("company_name") or "")
    for company in companies or []:
        add_company(
            company.get("ticker") or "",
            company.get("company_name") or "",
            list(company.get("tickers") or []),
        )
    return lexicon


def _mentioned(text: str, lexicon: dict[str, set[str]]) -> set[str]:
    """返回文本中出现的公司代码集合；用自定义边界，兼容中文紧邻英文名（NVIDIA与AMD）。"""
    low = (text or "").lower()
    found: set[str] = set()
    for token, tickers in lexicon.items():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
        if re.search(pattern, low):
            found |= tickers
    return found


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            urlencode(query),
            "",
        )
    )


def normalize_title(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# 事件类型签名：用于识别"同一事件的多种写法"（如两家媒体对同一财报的不同标题）
EVENT_SIGNATURES: dict[str, list[str]] = {
    "earnings": ["earnings", "quarter", "q1", "q2", "q3", "q4", "results", "profit", "revenue", "guidance"],
    "mna": ["acquisition", "acquire", "merger", "takeover", "stake", "buyout"],
    "investment": ["investment", "invests", "funding", "financing", "offering", "ipo"],
    "capital_return": ["buyback", "repurchase", "dividend"],
    "product": ["launch", "unveil", "introduces", "releases", "announces new", "product"],
    "partnership": ["partnership", "collaboration", "contract", "agreement", "deal with"],
    "regulatory": ["fine", "fined", "lawsuit", "probe", "investigation", "regulator", "antitrust"],
    "ai_infra": ["data center", "ai factory", "gpu", "chip", "semiconductor", "model"],
}


def event_signature(title: str) -> str | None:
    text = title.lower()
    for signature, words in EVENT_SIGNATURES.items():
        if any(word in text for word in words):
            return signature
    return None


@dataclass
class EventCluster:
    ticker: str
    company_name: str
    items: list[dict[str, Any]] = field(default_factory=list)
    norm_title: str = ""
    category: str | None = None
    importance: int | None = None
    confidence: float | None = None

    @property
    def primary(self) -> dict[str, Any]:
        """代表性材料：优先官方申报，其次新闻稿，再媒体；同级取信息量最大的。"""
        return sorted(
            self.items,
            key=lambda it: (KIND_WEIGHT.get(it.get("kind", "news"), 2), -len(it.get("summary") or "")),
        )[0]

    @property
    def sources(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in self.items:
            url = normalize_url(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(
                {
                    "name": item.get("source_name", ""),
                    "publisher": item.get("publisher", ""),
                    "kind": item.get("kind", "news"),
                    "url": item.get("url", ""),
                    "published_date": item.get("published_date"),
                }
            )
        return out

    @property
    def published_date(self) -> str | None:
        dates = [i.get("published_date") for i in self.items if i.get("published_date")]
        return min(dates) if dates else None

    @property
    def related_companies(self) -> list[dict[str, str]]:
        """同一事件涉及的其他追踪公司（跨公司归并后才有值）。"""
        seen: dict[str, str] = {}
        primary = (self.ticker or "").upper()
        for item in self.items:
            ticker = (item.get("ticker") or "").upper()
            if ticker and ticker != primary:
                seen.setdefault(ticker, item.get("company_name") or ticker)
        return [{"ticker": t, "company_name": n} for t, n in sorted(seen.items())]

    @property
    def event_key(self) -> str:
        digest = hashlib.sha1(
            f"{self.ticker}|{self.norm_title}|{self.published_date or ''}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.ticker.lower()}-{self.published_date or 'na'}-{digest}"


def _cluster_signature(cluster: EventCluster) -> str | None:
    return event_signature(cluster.primary.get("title") or "")


def _cluster_amount(cluster: EventCluster) -> float:
    values: list[float] = []
    for item in cluster.items[:3]:
        values.extend(extract_amounts(f"{item.get('title', '')} {item.get('summary', '')}"))
    return max(values) if values else 0.0


def _cluster_mentions(cluster: EventCluster, lexicon: dict[str, set[str]], include_summary: bool = True) -> set[str]:
    text = " ".join(
        f"{item.get('title', '')} {item.get('summary', '') if include_summary else ''}"
        for item in cluster.items[:3]
    )
    return _mentioned(text, lexicon)


def _cross_company_same_event(
    a: EventCluster,
    b: EventCluster,
    lexicon: dict[str, set[str]],
) -> bool:
    """跨公司同一事件的保守判定：互相提到对方 + 共同提及至少 2 家公司 + 同日 + 硬证据。"""
    if (a.ticker or "").upper() == (b.ticker or "").upper():
        return False
    if not a.published_date or a.published_date != b.published_date:
        return False

    ticker_a, ticker_b = a.ticker.upper(), b.ticker.upper()
    title_mentions_a = _cluster_mentions(a, lexicon, include_summary=False)
    title_mentions_b = _cluster_mentions(b, lexicon, include_summary=False)
    full_mentions_a = _cluster_mentions(a, lexicon)
    full_mentions_b = _cluster_mentions(b, lexicon)

    # 标题必须互相点名对方，否则只是“顺带提到”，不能据此归并（避免把两家公司各自的财报合并）
    if ticker_a not in title_mentions_b or ticker_b not in title_mentions_a:
        return False
    if ticker_a not in full_mentions_b or ticker_b not in full_mentions_a:
        return False
    if len(full_mentions_a & full_mentions_b) < 2:
        return False

    signature_a, signature_b = _cluster_signature(a), _cluster_signature(b)
    if signature_a and signature_a == signature_b:
        return True

    amount_a, amount_b = _cluster_amount(a), _cluster_amount(b)
    if amount_a >= 5e8 and amount_b >= 5e8:
        return abs(amount_a - amount_b) / max(amount_a, amount_b) <= 0.1
    return False


def _subject_ratio(cluster: EventCluster, lexicon: dict[str, set[str]]) -> float:
    """该公司的材料里，有多少标题真的在讲它自己（而不是在讲别家公司）。"""
    own = (cluster.ticker or "").upper()
    titles = [item.get("title") or "" for item in cluster.items[:5]]
    if not titles:
        return 0.0
    return sum(1 for title in titles if own in _mentioned(title, lexicon)) / len(titles)


def _cluster_weight(cluster: EventCluster, lexicon: dict[str, set[str]]) -> tuple:
    """归并时保留哪一条：来源更权威、更像“讲自己”、材料更多、摘要更长，最后按代码保证确定性。"""
    primary = cluster.primary
    return (
        KIND_WEIGHT.get(primary.get("kind", "news"), 2),
        -_subject_ratio(cluster, lexicon),
        -len(cluster.items),
        -len(primary.get("summary") or ""),
        cluster.ticker,
    )


def _merge_cross_company(
    clusters: list[EventCluster],
    lexicon: dict[str, set[str]],
) -> list[EventCluster]:
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if not _cross_company_same_event(clusters[i], clusters[j], lexicon):
                    continue
                if _cluster_weight(clusters[i], lexicon) <= _cluster_weight(clusters[j], lexicon):
                    keep, drop, drop_index = clusters[i], clusters[j], j
                else:
                    keep, drop, drop_index = clusters[j], clusters[i], i
                keep.items.extend(drop.items)
                clusters.pop(drop_index)
                merged = True
                break
            if merged:
                break
    return clusters


def cluster_items(
    items: list[dict[str, Any]],
    threshold: float = 0.86,
    companies: list[dict[str, Any]] | None = None,
) -> list[EventCluster]:
    clusters: dict[str, list[EventCluster]] = {}
    for item in items:
        ticker = item.get("ticker") or ""
        norm = normalize_title(item.get("title") or "")
        if not norm:
            continue
        url_key = normalize_url(item.get("url") or "")
        bucket = clusters.setdefault(ticker, [])
        signature = event_signature(item.get("title") or "")
        published = item.get("published_date")
        for cluster in bucket:
            same_url = url_key and any(
                normalize_url(i.get("url") or "") == url_key for i in cluster.items
            )
            if same_url or similarity(norm, cluster.norm_title) >= threshold:
                cluster.items.append(item)
                break
            # 同一公司、同一事件类型、同一天，标题写法不同也归并（需较强相似度作为护栏）
            cluster_signature = event_signature(cluster.primary.get("title") or "")
            if (
                signature
                and signature == cluster_signature
                and published
                and published == cluster.published_date
                and similarity(norm, cluster.norm_title) >= 0.4
            ):
                cluster.items.append(item)
                break
        else:
            bucket.append(
                EventCluster(
                    ticker=ticker,
                    company_name=item.get("company_name") or "",
                    items=[item],
                    norm_title=norm,
                )
            )
    flat = [c for bucket in clusters.values() for c in bucket if c.items]
    lexicon = build_company_lexicon(items, companies)
    return _merge_cross_company(flat, lexicon)
