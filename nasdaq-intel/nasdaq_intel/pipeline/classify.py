"""情报分类与重要性评分。

优先调用 LLM（prompts/classify.txt），未配置模型时使用可解释的规则回退，
两者输出同一套 JSON 契约，便于后续替换与对比。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..llm import LLMClient, LLMError
from .normalize import EventCluster

CATEGORIES = {"technology", "capital", "business", "financial", "other"}

KEYWORDS: dict[str, list[str]] = {
    "capital": [
        "acquire", "acquisition", "merger", "takeover", "stake", "investment", "invests",
        "buyback", "repurchase", "offering", "financing", "dividend", "capex",
        "capital expenditure", "joint venture", "deal", "ipo", "debt", "convertible",
    ],
    "technology": [
        "ai", "artificial intelligence", "chip", "semiconductor", "gpu", "model", "llm",
        "data center", "cloud", "robot", "autonomous", "quantum", "software", "platform",
        "product", "launch", "unveil", "introduc", "algorithm", "inference", "training",
    ],
    "financial": [
        "earnings", "revenue", "profit", "margin", "guidance", "quarter", "fiscal",
        "eps", "results", "forecast", "outlook", "cash flow", "beats", "misses",
    ],
    "business": [
        "partnership", "contract", "customer", "pricing", "expansion", "competition",
        "agreement", "collaboration", "supply", "market share", "subscription", "advertis",
    ],
}

_OPINION_MARKERS = [
    "zacks", "motley fool", "should you buy", "price target", "better buy", "prediction",
    "what to expect", "here's why", "heres why", "analyst", "rating", "upgrade", "downgrade",
    "bullish", "bearish", "what you think", "biggest risk", "huge winner", "my top",
]
_OPINION_PUBLISHERS = {
    "the motley fool", "motley fool", "zacks", "seeking alpha", "investorplace",
    "24/7 wall st.", "24/7 wall st", "barchart", "insider monkey",
}
_CONTENT_TYPE_LABELS = {"official": "官方", "news": "报道", "opinion": "观点"}
_ROUNDUP_MARKERS = [
    "stocks close", "market wrap", "futures", "premarket", "dow jones", "s&p 500",
    "wall street", "what to know", "markets today", "stock market today",
]
# 程序性/例行信息，除非有金额或明确事件，否则价值极低
_ROUTINE_MARKERS = [
    "options now available", "new options", "ex-dividend", "earnings call transcript",
    "what to watch", "preview", "how to", "trading higher", "trading lower",
    "stock moves", "shares are", "week ahead",
]
_AMOUNT_RE = re.compile(r"\$\s?([0-9]+(?:\.[0-9]+)?)\s?(trillion|billion|million|bn|b|m)\b", re.I)

# 这些关键词是词干（introduces/introducing、advertised/advertising），按前缀匹配；
# 其余关键词必须整词匹配，避免 "ai" 命中 "airline"、"deal" 命中 "dealer" 这类误判。
_PREFIX_KEYWORDS = {"introduc", "advertis"}


def _match_category(text: str) -> tuple[str, dict[str, int]]:
    scores: dict[str, int] = {}
    for category, words in KEYWORDS.items():
        hits = 0
        for word in words:
            escaped = re.escape(word)
            pattern = rf"\b{escaped}" if word in _PREFIX_KEYWORDS else rf"\b{escaped}\b"
            if re.search(pattern, text):
                hits += 1
        if hits:
            scores[category] = hits
    if not scores:
        return "other", {}
    return max(scores.items(), key=lambda kv: kv[1])[0], scores


def _extract_amounts(text: str) -> list[float]:
    values: list[float] = []
    for amount, unit in _AMOUNT_RE.findall(text):
        number = float(amount)
        unit = unit.lower()
        if unit.startswith("t"):
            number *= 1e12
        elif unit in ("billion", "bn", "b"):
            number *= 1e9
        else:
            number *= 1e6
        values.append(number)
    return values


def _content_type(primary: dict[str, Any], text: str) -> str:
    if primary.get("kind") in ("filing", "press_release"):
        return "official"
    publisher = (primary.get("publisher") or "").lower().strip()
    if publisher in _OPINION_PUBLISHERS or any(m in text for m in _OPINION_MARKERS):
        return "opinion"
    return "news"


def _rule_score(cluster: EventCluster, threshold: int) -> dict[str, Any]:
    primary = cluster.primary
    title = primary.get("title", "")
    text = f"{title} {primary.get('summary','')}".lower()
    category, _ = _match_category(text)
    # 官方申报自带类别提示（如 10-Q / 10-K → 财务）
    if primary.get("category_hint"):
        category = primary["category_hint"]
    amounts = _extract_amounts(text)
    biggest = max(amounts) if amounts else 0.0
    content_type = _content_type(primary, text)

    importance = 3
    if any(marker in text for marker in _ROUNDUP_MARKERS):
        importance = 1
    elif any(marker in text for marker in _ROUTINE_MARKERS):
        importance = 1
    elif content_type == "opinion":
        # 观点类文章可以提到金额，但本身不是事件，重要性封顶 2
        importance = 2
    elif category == "capital" and re.search(r"acqui|merger|takeover|stake", text):
        importance = 5 if biggest >= 5e9 else 4
    elif category == "capital" and biggest >= 1e9:
        importance = 4
    elif category == "financial" and re.search(r"guidance|beats|misses|raise|lowers", text):
        importance = 4
    elif category == "technology" and re.search(r"launch|unveil|introduc|announce|release", text) and re.search(
        r"ai|chip|model|semiconductor|gpu|device|product|software|display|oled", text
    ):
        importance = 4
    elif primary.get("kind") == "filing":
        importance = max(importance, 3)

    return {
        "event_key": cluster.event_key,
        "ticker": cluster.ticker,
        "title": title,
        "category": category,
        "importance": importance,
        "confidence": 0.6 if content_type != "opinion" else 0.45,
        "is_duplicate": len(cluster.items) > 1,
        "fact_summary": (primary.get("summary") or title)[:400],
        "source_urls": [s["url"] for s in cluster.sources],
        "needs_verification": content_type != "official" and len(cluster.sources) < 2,
        "content_type": content_type,
        "mode": "rule",
    }


def _load_prompt(prompts_dir: Path, name: str) -> str:
    return (prompts_dir / name).read_text(encoding="utf-8")


def _llm_batch(
    llm: LLMClient,
    system_prompt: str,
    clusters: list[EventCluster],
    label: str = "classify",
) -> dict[str, dict[str, Any]]:
    payload = []
    for cluster in clusters:
        primary = cluster.primary
        payload.append(
            {
                "event_key": cluster.event_key,
                "ticker": cluster.ticker,
                "company": cluster.company_name,
                "title": primary.get("title", ""),
                "summary": (primary.get("summary") or "")[:500],
                "source": primary.get("source_name", ""),
                "kind": primary.get("kind", "news"),
                "published_date": primary.get("published_date"),
                "source_count": len(cluster.sources),
            }
        )
    user = json.dumps({"news_items": payload}, ensure_ascii=False, indent=1)
    result = llm.chat_json(system_prompt, user, label=label)
    items = result.get("items") if isinstance(result, dict) else result
    out: dict[str, dict[str, Any]] = {}
    for raw in items or []:
        key = raw.get("event_key")
        if not key:
            continue
        category = raw.get("category") if raw.get("category") in CATEGORIES else "other"
        try:
            importance = int(raw.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        importance = max(1, min(5, importance))
        try:
            confidence = float(raw.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        out[key] = {
            "event_key": key,
            "ticker": raw.get("ticker") or "",
            "title": raw.get("title") or "",
            "category": category,
            "importance": importance,
            "confidence": max(0.0, min(1.0, confidence)),
            "is_duplicate": bool(raw.get("is_duplicate", False)),
            "fact_summary": (raw.get("fact_summary") or "")[:400],
            "source_urls": raw.get("source_urls") or [],
            "needs_verification": bool(raw.get("needs_verification", False)),
            "mode": "llm",
        }
    return out


def classify_clusters(
    clusters: list[EventCluster],
    *,
    llm: LLMClient,
    prompts_dir: Path,
    threshold: int = 4,
    batch_size: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (事件列表, 说明性提示)。LLM 失败时自动回退规则模式。"""
    notes: list[str] = []
    results: dict[str, dict[str, Any]] = {}

    if llm.available:
        system_prompt = _load_prompt(prompts_dir, "classify.txt")
        for start in range(0, len(clusters), batch_size):
            batch = clusters[start : start + batch_size]
            try:
                results.update(
                    _llm_batch(llm, system_prompt, batch, label=f"classify:batch{start // batch_size + 1}")
                )
            except LLMError as exc:
                notes.append(f"分类 LLM 调用失败，已回退规则模式：{exc}")
                break

    for cluster in clusters:
        if cluster.event_key in results:
            event = results[cluster.event_key]
            event["source_urls"] = [s["url"] for s in cluster.sources]
            event["content_type"] = _content_type(
                cluster.primary, f"{cluster.primary.get('title','')} {cluster.primary.get('summary','')}".lower()
            )
        else:
            event = _rule_score(cluster, threshold)
        event["_cluster"] = cluster
        results[cluster.event_key] = event

    if not llm.available:
        notes.append("未配置 LLM API Key，本次使用规则模式（分类与评分为启发式结果）")

    events = sorted(
        results.values(),
        key=lambda e: (e.get("importance", 0), e.get("confidence", 0)),
        reverse=True,
    )
    return events, notes
