"""Markdown 日报生成（确定性模板，保证数字与来源不走样）。

七段结构：
1. 日期 / 数据截止 / 数据源 / 运行状态
2. 今日摘要（最多 3 条）
3. 市值前十与较前一交易日变化
4. 重大事件（按重要性排序，最多 8 条）
5. 跨公司科技与资本趋势
6. 后续观察清单
7. 数据局限、采集失败与待验证事项
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

CATEGORY_LABELS = {
    "technology": "技术",
    "capital": "资本",
    "business": "商业",
    "financial": "财务",
    "other": "其他",
}

CONTENT_TYPE_LABELS = {"official": "官方", "news": "报道", "opinion": "观点"}


def _fmt_cap(value: float) -> str:
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


def _local_time(iso: str | None, tz_name: str) -> str:
    if not iso:
        return "未知"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return iso


def _rank_change(company: dict[str, Any], prev: dict[str, Any] | None) -> str:
    if not prev:
        return "—"
    prev_rank: dict[str, int] = {}
    for item in prev.get("companies", []):
        for ticker in item.get("tickers", [item.get("ticker")]):
            prev_rank[ticker] = item.get("rank", 0)
    ranks = [prev_rank[t] for t in company.get("tickers", []) if t in prev_rank]
    if not ranks:
        return "新进"
    before = min(ranks)
    delta = before - company["rank"]
    if delta > 0:
        return f"↑{delta}"
    if delta < 0:
        return f"↓{abs(delta)}"
    return "持平"


def _event_block(idx: int, event: dict[str, Any], analysis: dict[str, Any] | None) -> str:
    cluster = event["_cluster"]
    category = CATEGORY_LABELS.get(event.get("category", "other"), "其他")
    content_type = CONTENT_TYPE_LABELS.get(event.get("content_type", "news"), "报道")
    headline = event.get("title") or cluster.primary.get("title", "")
    url = (cluster.sources[0]["url"] if cluster.sources else "")
    lines = [f"### {idx}. [{headline}]({url})" if url else f"### {idx}. {headline}"]
    lines.append(
        f"- 公司：{cluster.company_name}（{event.get('ticker')}）｜分类：{category}｜"
        f"材料类型：{content_type}｜重要性：{event.get('importance')}/5｜置信度：{event.get('confidence')}"
    )
    flags = []
    if event.get("needs_verification"):
        flags.append("待核验（来源不足 2 个）")
    if event.get("is_duplicate"):
        flags.append("多来源报道，已归并")
    if flags:
        lines.append(f"- 标注：{'；'.join(flags)}")
    fact = event.get("fact_summary") or cluster.primary.get("summary") or ""
    if fact:
        lines.append(f"- 事实：{fact}")
    if analysis:
        facts = analysis.get("facts") or []
        if facts:
            lines.append("- 已确认事实：")
            for fact_item in facts[:5]:
                lines.append(f"  - {fact_item}")
        for field, label in (
            ("strategic_analysis", "战略意图（推断）"),
            ("technology_impact", "技术影响（推断）"),
            ("capital_impact", "资本影响（推断）"),
            ("competitive_impact", "竞争影响（推断）"),
        ):
            value = analysis.get(field)
            if value:
                lines.append(f"- {label}：{value}")
        follow = analysis.get("follow_up_signals") or []
        if follow:
            lines.append("- 后续观察信号：")
            for signal in follow[:4]:
                lines.append(f"  - {signal}")
        limitations = analysis.get("limitations") or []
        if limitations:
            lines.append(f"- 分析局限：{'；'.join(str(x) for x in limitations[:3])}")
    sources = cluster.sources
    if sources:
        rendered = " · ".join(f"[{s['name'] or s['publisher']}]({s['url']})" for s in sources[:4])
        lines.append(f"- 来源：{rendered}")
    return "\n".join(lines)


def build_report(
    *,
    day: date,
    snapshot: dict[str, Any],
    prev_snapshot: dict[str, Any] | None,
    events: list[dict[str, Any]],
    analyses: dict[str, dict[str, Any]],
    llm_mode: str,
    run: dict[str, Any],
    tz_name: str,
    max_events: int = 8,
    collection: dict[str, Any] | None = None,
) -> str:
    collection = collection or {}
    incomplete = snapshot.get("status") != "ok"
    status_line = "✅ 正常" if not incomplete and run.get("status") == "ok" else "⚠️ 存在异常"

    header = [
        f"# 纳斯达克科技与资本情报日报 · {day.isoformat()}",
        "",
        f"- 数据截止：{_local_time(snapshot.get('generated_at'), tz_name)}",
        f"- 市值数据源：{snapshot.get('data_source')}",
        f"- 新闻/公告源：Nasdaq 个股新闻、PR Newswire、SEC EDGAR",
        f"- 分析模式：{llm_mode}",
        f"- 运行状态：{status_line}（耗时 {run.get('duration_seconds', 0)} 秒）",
    ]
    if incomplete:
        header.append("- ⚠️ **排名不完整**：本次市值候选池低于预期下限，不作完整 Top 10 结论")

    # 2. 今日摘要
    important = [e for e in events if e.get("importance", 0) >= 4][:3]
    summary_lines = ["", "## 一、今日摘要", ""]
    if important:
        for event in important:
            summary_lines.append(
                f"- **{event['_cluster'].company_name}（{event['ticker']}）**：{event.get('title')}"
            )
    else:
        summary_lines.append("- 本次采集未发现达到筛选阈值的重大事件。")

    # 3. 排名
    rank_lines = [
        "",
        "## 二、市值排名前十",
        "",
        f"口径：{snapshot.get('ranking_method')}；覆盖 {snapshot.get('universe_size')} 只上市证券。",
        "",
        "> 说明：多股类公司按已上市类别合并计算，与公司披露的总市值口径可能存在差异；"
        "排行榜仅用于确定追踪名单，不构成估值结论。",
        "",
        "| 排名 | 公司 | 代码 | 市值 | 较上次 | 板块 |",
        "|---|---|---|---|---|---|",
    ]
    for company in snapshot.get("companies", []):
        rank_lines.append(
            "| {rank} | {name} | {ticker} | {cap} | {change} | {sector} |".format(
                rank=company["rank"],
                name=company["company_name"],
                ticker="/".join(company.get("tickers", [company["ticker"]])),
                cap=_fmt_cap(company["market_cap"]),
                change=_rank_change(company, prev_snapshot),
                sector=company.get("sector") or "—",
            )
        )
    if not snapshot.get("companies"):
        rank_lines.append("| — | 无有效数据 | — | — | — | — |")

    # 4. 重大事件
    event_lines = ["", "## 三、重大事件", ""]
    selected = [e for e in events if e.get("importance", 0) >= 3][:max_events]
    if not selected:
        event_lines.append("本次采集未发现达到筛选阈值的重大事件。")
    else:
        for idx, event in enumerate(selected, start=1):
            event_lines.append(_event_block(idx, event, analyses.get(event["event_key"])))
            event_lines.append("")

    # 5. 趋势
    trend_lines = ["## 四、跨公司科技与资本趋势", ""]
    counts: dict[str, int] = {}
    company_counts: dict[str, int] = {}
    for event in events:
        counts[event.get("category", "other")] = counts.get(event.get("category", "other"), 0) + 1
        company_counts[event["ticker"]] = company_counts.get(event["ticker"], 0) + 1
    if counts:
        rendered_counts = "，".join(
            f"{CATEGORY_LABELS.get(k, k)} {v} 条" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        trend_lines.append(f"- 事件分布：{rendered_counts}（共 {len(events)} 个归并事件）。")
        top_companies = sorted(company_counts.items(), key=lambda kv: -kv[1])[:3]
        if top_companies:
            names = "、".join(f"{t}（{c} 条）" for t, c in top_companies)
            trend_lines.append(f"- 信息最集中的公司：{names}。")
        big_capital = [
            e for e in events if e.get("category") == "capital" and e.get("importance", 0) >= 4
        ]
        if big_capital:
            trend_lines.append(
                f"- 资本层面出现 {len(big_capital)} 条高重要性动作，需关注其对资本开支与竞争格局的影响。"
            )
    else:
        trend_lines.append("- 本次采集无足够事件支撑趋势判断。")
    trend_lines.append("- 说明：以上为分类统计结果，不含股价预测。")

    # 6. 观察清单
    watch_lines = ["", "## 五、后续观察清单", ""]
    signals: list[str] = []
    for analysis in analyses.values():
        for signal in (analysis.get("follow_up_signals") or []):
            signals.append(str(signal))
    if signals:
        for signal in signals[:8]:
            watch_lines.append(f"- {signal}")
    else:
        watch_lines.append("- 规则模式未生成后续观察项；接入 LLM 后将输出可验证的指标与里程碑。")

    # 7. 局限
    limit_lines = ["", "## 六、数据局限与待验证事项", ""]
    if incomplete:
        limit_lines.append(f"- 排名状态：{snapshot.get('status')}；{'；'.join(snapshot.get('warnings') or [])}")
    if collection.get("errors"):
        for err in collection["errors"][:10]:
            limit_lines.append(f"- 采集失败：{err}")
    if run.get("errors"):
        for err in run["errors"][:10]:
            limit_lines.append(f"- 运行异常：{err}")
    if run.get("warnings"):
        for warning in run["warnings"][:10]:
            limit_lines.append(f"- 说明：{warning}")
    pending = [e for e in events if e.get("needs_verification")]
    if pending:
        limit_lines.append(f"- 待核验事件：{len(pending)} 条（单一来源或缺少正文）")
    dup = [e for e in events if e.get("is_duplicate")]
    if events:
        limit_lines.append(
            f"- 去重结果：{len(events)} 个事件中包含 {len(dup)} 个多来源归并事件"
            f"（重复率 {len(dup) / len(events):.0%}）"
        )
    if "规则" in llm_mode:
        limit_lines.append(
            "- 分析模式为规则回退：分类与评分基于关键词启发式，未做语义推理，结论仅可用于初筛。"
        )
    limit_lines.append(
        f"- 采集覆盖：{collection.get('company_count', 0)} 家公司，"
        f"新闻 {collection.get('news_count', 0)} 条，SEC 申报 {collection.get('filing_count', 0)} 条。"
    )
    limit_lines.append("- 新闻发布时间仅精确到日期，非精确到分钟；未获取全文的报道证据等级较低。")

    footer = [
        "",
        "---",
        f"生成时间：{_local_time(run.get('finished_at'), tz_name)}｜"
        f"事件总数：{len(events)}｜报告事件：{len(selected)}",
    ]

    return "\n".join(
        header + summary_lines + rank_lines + event_lines + trend_lines + watch_lines + limit_lines + footer
    ) + "\n"
