"""重要事件深度分析：仅处理重要性达到阈值的事件，避免对每条新闻长篇分析。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..llm import LLMClient, LLMError


def summarize_trends(
    events: list[dict[str, Any]],
    *,
    llm: LLMClient,
    prompts_dir: Path,
    max_events: int = 40,
) -> tuple[dict[str, list[str]] | None, list[str], bool]:
    """跨公司趋势总结（LLM）。返回 (趋势, 错误, 是否使用 LLM)。"""
    if not llm.available or not events:
        return None, [], False
    payload = [
        {
            "event_key": event.get("event_key"),
            "ticker": event.get("ticker"),
            "title": event.get("title"),
            "category": event.get("category"),
            "importance": event.get("importance"),
            "fact_summary": (event.get("fact_summary") or "")[:200],
        }
        for event in sorted(events, key=lambda e: e.get("importance", 0), reverse=True)[:max_events]
    ]
    system_prompt = (prompts_dir / "trend.txt").read_text(encoding="utf-8")
    try:
        raw = llm.chat_json(
            system_prompt,
            json.dumps({"events": payload}, ensure_ascii=False, indent=1),
            label="trend",
        )
    except LLMError as exc:
        return None, [f"趋势总结失败：{exc}"], False
    if not isinstance(raw, dict):
        return None, ["趋势总结返回格式异常，已跳过"], False

    cleaned: dict[str, list[str]] = {}
    for key in ("technology_trends", "competition", "capital_signals", "watch_items"):
        values = raw.get(key) or []
        if not isinstance(values, list):
            values = []
        cleaned[key] = [str(value)[:300] for value in values[:4] if str(value).strip()]
    return cleaned, [], True


def analyze_events(
    events: list[dict[str, Any]],
    *,
    llm: LLMClient,
    prompts_dir: Path,
    threshold: int = 4,
    max_items: int = 6,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    """返回 (event_key -> 分析结果, 错误信息, 是否真正使用了 LLM)。"""
    targets = [e for e in events if e.get("importance", 0) >= threshold][:max_items]
    if not targets or not llm.available:
        return {}, [], False

    system_prompt = (prompts_dir / "analyze.txt").read_text(encoding="utf-8")
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for event in targets:
        cluster = event["_cluster"]
        bundle = {
            "event_key": event["event_key"],
            "ticker": event["ticker"],
            "company": cluster.company_name,
            "title": event.get("title"),
            "fact_summary": event.get("fact_summary"),
            "category": event.get("category"),
            "importance": event.get("importance"),
            "published_date": cluster.published_date,
            "sources": cluster.sources,
            "source_materials": [
                {
                    "kind": item.get("kind"),
                    "title": item.get("title"),
                    "summary": (item.get("summary") or "")[:500],
                    "published_date": item.get("published_date"),
                    "url": item.get("url"),
                }
                for item in cluster.items[:4]
            ],
        }
        try:
            raw = llm.chat_json(
                system_prompt,
                json.dumps(bundle, ensure_ascii=False, indent=1),
                label=f"analyze:{event['event_key']}",
            )
            if isinstance(raw, dict):
                results[event["event_key"]] = raw
        except LLMError as exc:
            errors.append(f"{event['event_key']} 深度分析失败：{exc}")
    return results, errors, bool(results)
