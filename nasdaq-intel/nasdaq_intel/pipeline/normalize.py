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
    def event_key(self) -> str:
        digest = hashlib.sha1(
            f"{self.ticker}|{self.norm_title}|{self.published_date or ''}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.ticker.lower()}-{self.published_date or 'na'}-{digest}"


def cluster_items(items: list[dict[str, Any]], threshold: float = 0.86) -> list[EventCluster]:
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
    return [c for bucket in clusters.values() for c in bucket if c.items]
