"""Markdown 日报生成（确定性模板，保证数字与来源不走样）。

结构（对齐任务书 7.5）：
1. 今日核心情报（S/A 级）
2. 公司动态（逐家公司）
3. 行业趋势与竞争格局
4. 市场表现（价格口径明确）
5. 后续观察清单
6. 数据质量与运行说明
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .classify import TOPICS

CATEGORY_LABELS = {
    "technology": "技术",
    "capital": "资本",
    "business": "商业",
    "financial": "财务",
    "other": "其他",
}

CONTENT_TYPE_LABELS = {"official": "官方", "news": "报道", "opinion": "观点"}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
GRADE_LABELS = {5: "S", 4: "A", 3: "B", 2: "C", 1: "C"}
MARKET_STATUS_LABELS = {
    "open": "交易中",
    "closed": "已收盘",
    "after": "盘后",
    "pre": "盘前",
}


def _grade(importance: int) -> str:
    return GRADE_LABELS.get(int(importance or 0), "C")


def _fmt_cap(value: float) -> str:
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _fmt_change(change: float | None, pct: float | None) -> str:
    if change is None and pct is None:
        return "—"
    change_text = f"{change:+.2f}" if change is not None else "—"
    pct_text = f"{pct:+.2f}%" if pct is not None else "—"
    return f"{change_text}（{pct_text}）"


def _local_time(iso: str | None, tz_name: str) -> str:
    if not iso:
        return "未知"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
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


def _event_url(event: dict[str, Any]) -> str:
    cluster = event.get("_cluster")
    if cluster and cluster.sources:
        return cluster.sources[0]["url"]
    return ""


def _event_block(idx: int, event: dict[str, Any], analysis: dict[str, Any] | None) -> str:
    cluster = event["_cluster"]
    category = CATEGORY_LABELS.get(event.get("category", "other"), "其他")
    content_type = CONTENT_TYPE_LABELS.get(event.get("content_type", "news"), "报道")
    headline = event.get("title") or cluster.primary.get("title", "")
    url = _event_url(event)
    lines = [f"### {idx}. [{headline}]({url})" if url else f"### {idx}. {headline}"]
    lines.append(
        f"- 公司：{cluster.company_name}（{event.get('ticker')}）｜等级：{_grade(event.get('importance'))}"
        f"（{event.get('importance')}/5）｜分类：{category}｜材料类型：{content_type}｜"
        f"置信度：{event.get('confidence')}"
    )
    related = cluster.related_companies
    if related:
        names = "、".join(f"{r['company_name']}（{r['ticker']}）" for r in related[:4])
        lines.append(f"- 关联公司：{names}（同一事件的多公司报道，已归并）")
    topic_names = [TOPICS[code] for code in (event.get("topics") or []) if code in TOPICS]
    if topic_names:
        lines.append(f"- 主题：{'、'.join(topic_names)}")
    flags = []
    if event.get("needs_verification"):
        flags.append("待核验（来源不足 2 个）")
    if event.get("is_duplicate"):
        flags.append("多来源报道，已归并")
    if flags:
        lines.append(f"- 标注：{'；'.join(flags)}")
    if cluster.published_date:
        lines.append(f"- 发布时间：{cluster.published_date}（来源精度：日期）")
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
            ("market_expectation", "资本市场预期（推断）"),
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


def _company_dynamics(company: dict[str, Any], events: list[dict[str, Any]], analyses: dict[str, dict[str, Any]]) -> str:
    ticker = company["ticker"]
    own = [event for event in events if event.get("ticker") == ticker]
    lines = [f"### {company['company_name']}（{ticker}）"]

    def group(category: str, limit: int = 2) -> list[dict[str, Any]]:
        return [
            event
            for event in own
            if event.get("category") == category and event.get("importance", 0) >= 3
        ][:limit]

    sections = [
        ("科技动态", group("technology")),
        ("资本动态", group("capital")),
        ("其他动态", [e for e in own if e.get("category") not in ("technology", "capital") and e.get("importance", 0) >= 3][:2]),
    ]
    for label, items in sections:
        if not items:
            lines.append(f"- {label}：—")
            continue
        rendered = "；".join(
            f"[{event.get('title')}]({_event_url(event)})（{_grade(event.get('importance'))}）"
            for event in items
        )
        lines.append(f"- {label}：{rendered}")

    high = [event for event in own if event.get("importance", 0) >= 4]
    medium = [event for event in own if event.get("importance", 0) == 3]
    if own:
        lines.append(f"- 今日重要事件：S/A {len(high)} 条，B {len(medium)} 条")
    else:
        lines.append("- 今日重要事件：—")

    signals: list[str] = []
    for event in own:
        analysis = analyses.get(event["event_key"]) or {}
        signals.extend(str(item) for item in (analysis.get("follow_up_signals") or []))
    signals = list(dict.fromkeys(signals))
    if signals:
        lines.append("- 后续观察：")
        for signal in signals[:3]:
            lines.append(f"  - {signal}")
    else:
        lines.append("- 后续观察：—")
    return "\n".join(lines)


def _market_section(market_snapshots: list[dict[str, Any]], day: date, tz_name: str) -> list[str]:
    lines = ["", "## 四、市场表现", ""]
    if not market_snapshots:
        lines.append("本次未取得行情数据，详见第六部分运行说明。")
        return lines

    lines.extend(
        [
            "| 公司 | 代码 | 最新价 | 前收 | 当日收盘 | 日涨跌 | 快照时间（ET） | 市场状态 |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    intraday = False
    for snap in market_snapshots:
        close = snap.get("daily_close")
        basis = snap.get("change_basis")
        if basis == "intraday":
            intraday = True
        status = MARKET_STATUS_LABELS.get(snap.get("market_phase"), snap.get("market_status") or "—")
        stamp = snap.get("quote_timestamp_display") or "—"
        lines.append(
            "| {name} | {ticker} | {current} | {prev} | {close} | {change}{mark} | {stamp} | {status} |".format(
                name=snap.get("company_name"),
                ticker=snap.get("ticker"),
                current=_fmt_price(snap.get("current_price")),
                prev=_fmt_price(snap.get("previous_close")),
                close=_fmt_price(close) if close is not None else "未收盘",
                change=_fmt_change(snap.get("change"), snap.get("change_pct")),
                mark="*" if basis == "intraday" else "",
                stamp=stamp,
                status=status,
            )
        )
    historical_mode = any(snap.get("historical") for snap in market_snapshots)
    lines.append("")
    if historical_mode:
        lines.append("- 口径：历史日期报告只使用该交易日历史日线收盘价；最新价、快照时间列留空。")
    else:
        lines.append(
            f"- 口径：最新价为行情快照（{_local_time(market_snapshots[0].get('fetched_at'), tz_name)} 北京时间采集），"
            "不能等同于收盘价。"
        )
    lines.append(
        f"- 当日收盘：{day.isoformat()}（美东交易日）的历史日线 close；尚未收盘时显示「未收盘」，不以盘中价代替。"
    )
    if intraday:
        lines.append("- 带 * 的日涨跌为「盘中最新价 - 前一交易日收盘」，不是收盘涨跌。")
    lines.append("- 数据源：api.nasdaq.com（行情快照与历史日线）；前收为上一个交易日收盘价。")
    return lines


def build_report(
    *,
    day: date,
    snapshot: dict[str, Any],
    prev_snapshot: dict[str, Any] | None,
    events: list[dict[str, Any]],
    analyses: dict[str, dict[str, Any]],
    llm_mode: str,
    run: dict[str, Any],
    tz_name: str = "Asia/Shanghai",
    max_events: int = 8,
    collection: dict[str, Any] | None = None,
    market_snapshots: list[dict[str, Any]] | None = None,
    trend_summary: dict[str, list[str]] | None = None,
    llm_stats: dict[str, Any] | None = None,
    quality_issues: list[dict[str, str]] | None = None,
    db_summary: dict[str, int] | None = None,
) -> str:
    collection = collection or {}
    market_snapshots = market_snapshots or []
    quality_issues = quality_issues or []
    llm_stats = llm_stats or {}
    incomplete = snapshot.get("status") != "ok"
    status_line = "✅ 正常" if not incomplete and run.get("status") == "ok" else "⚠️ 存在异常"

    total_companies = len(snapshot.get("companies") or [])
    covered_companies = collection.get("company_count") or total_companies
    header = [
        f"# Nasdaq Top 10 科技与资本情报日报 · {day.isoformat()}（{WEEKDAYS[day.weekday()]}）",
        "",
        f"- 报告日期（美东交易日）：{day.isoformat()}",
        f"- 数据截止：{_local_time(snapshot.get('generated_at'), 'UTC')} UTC"
        f"（{_local_time(snapshot.get('generated_at'), tz_name)} 北京时间）",
        f"- 覆盖公司：{covered_companies}/{total_companies}",
        f"- 数据来源：市值 Nasdaq 官方筛选器；行情 api.nasdaq.com；新闻 Nasdaq News / PR Newswire；公告 SEC EDGAR",
        f"- 运行状态：{status_line}（耗时 {run.get('duration_seconds', 0)} 秒）",
    ]
    if incomplete:
        header.append("- ⚠️ **排名不完整**：本次市值候选池低于预期下限，不作完整 Top 10 结论")
    if market_snapshots and market_snapshots[0].get("market_phase") == "open":
        header.append("- ⚠️ 本次运行时美股仍处于交易中，最新价为盘中快照，当日收盘价尚未形成")

    # 1. 今日核心情报
    core = [event for event in events if event.get("importance", 0) >= 4][:5]
    core_lines = ["", "## 一、今日核心情报", ""]
    if core:
        for idx, event in enumerate(core, start=1):
            core_lines.append(_event_block(idx, event, analyses.get(event["event_key"])))
            core_lines.append("")
    else:
        core_lines.append("今日未发现达到 S/A 级的事件。")

    # 2. 公司动态
    company_lines = ["", "## 二、公司动态", ""]
    companies = snapshot.get("companies") or []
    if companies:
        for company in companies:
            company_lines.append(_company_dynamics(company, events, analyses))
            company_lines.append("")
    else:
        company_lines.append("本次无有效公司名单。")

    # 3. 趋势
    trend_lines = ["", "## 三、行业趋势与竞争格局", ""]
    if trend_summary:
        for key, label in (
            ("technology_trends", "技术趋势"),
            ("competition", "竞争格局"),
            ("capital_signals", "资本信号"),
        ):
            items = trend_summary.get(key) or []
            if items:
                trend_lines.append(f"**{label}**")
                for item in items[:4]:
                    trend_lines.append(f"- {item}")
        trend_lines.append("")

    counts: dict[str, int] = {}
    company_counts: dict[str, int] = {}
    for event in events:
        counts[event.get("category", "other")] = counts.get(event.get("category", "other"), 0) + 1
        company_counts[event["ticker"]] = company_counts.get(event["ticker"], 0) + 1
    if counts:
        rendered_counts = "，".join(
            f"{CATEGORY_LABELS.get(key, key)} {value} 条" for key, value in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        trend_lines.append(f"- 事件分布：{rendered_counts}（共 {len(events)} 个归并事件）。")
        top_companies = sorted(company_counts.items(), key=lambda kv: -kv[1])[:3]
        if top_companies:
            names = "、".join(f"{ticker}（{count} 条）" for ticker, count in top_companies)
            trend_lines.append(f"- 信息最集中的公司：{names}。")
    cross_merged = [event for event in events if event.get("_cluster") and event["_cluster"].related_companies]
    if cross_merged:
        trend_lines.append(f"- 跨公司归并事件：{len(cross_merged)} 条（同一事件涉及多家公司）。")
    if not trend_summary:
        trend_lines.append("- 说明：以上为确定性分类统计；接入 LLM 趋势总结后可补充跨公司判断。")

    # 4. 市场表现
    market_lines = _market_section(market_snapshots, day, tz_name)

    # 5. 观察清单
    watch_lines = ["", "## 五、后续观察清单", ""]
    signals: list[str] = []
    for analysis in analyses.values():
        signals.extend(str(item) for item in (analysis.get("follow_up_signals") or []))
    if trend_summary:
        signals.extend(trend_summary.get("watch_items") or [])
    signals = list(dict.fromkeys(signals))
    if signals:
        for signal in signals[:10]:
            watch_lines.append(f"- {signal}")
    else:
        watch_lines.append("- 本次未生成后续观察项。")
    pending_count = len([event for event in events if event.get("needs_verification")])
    if pending_count:
        watch_lines.append(f"- 待人工核验事件 {pending_count} 条（单一来源或缺少正文）。")

    # 6. 数据质量
    limit_lines = ["", "## 六、数据质量与运行说明", ""]
    if incomplete:
        limit_lines.append(f"- 排名状态：{snapshot.get('status')}；{'；'.join(snapshot.get('warnings') or [])}")
    shown_errors: set[str] = set()
    for err in (collection.get("errors") or [])[:10]:
        if err in shown_errors:
            continue
        shown_errors.add(err)
        limit_lines.append(f"- 采集/分析异常：{err}")
    for err in (run.get("errors") or [])[:10]:
        if err in shown_errors:
            continue
        shown_errors.add(err)
        limit_lines.append(f"- 运行异常：{err}")
    for warning in (run.get("warnings") or [])[:10]:
        limit_lines.append(f"- 说明：{warning}")

    if quality_issues:
        errors = [issue for issue in quality_issues if issue.get("level") == "error"]
        warnings = [issue for issue in quality_issues if issue.get("level") != "error"]
        if errors:
            limit_lines.append(f"- 数据校验错误 {len(errors)} 项：")
            for issue in errors[:6]:
                limit_lines.append(f"  - [{issue.get('code')}] {issue.get('message')}")
        if warnings:
            limit_lines.append(f"- 数据校验提示 {len(warnings)} 项：")
            for issue in warnings[:6]:
                limit_lines.append(f"  - [{issue.get('code')}] {issue.get('message')}")

    if pending_count:
        limit_lines.append(f"- 待核验事件：{pending_count} 条（单一来源或缺少正文）")
    duplicates = [event for event in events if event.get("is_duplicate")]
    if events:
        limit_lines.append(
            f"- 去重结果：{len(events)} 个事件中包含 {len(duplicates)} 个多来源归并事件"
            f"（归并比例 {len(duplicates) / len(events):.0%}）"
        )
    limit_lines.append(
        f"- 采集覆盖：{collection.get('company_count', 0)} 家公司，"
        f"新闻 {collection.get('news_count', 0)} 条，SEC 申报 {collection.get('filing_count', 0)} 条，"
        f"行情 {len(market_snapshots)} 家公司。"
    )
    if "规则" in llm_mode:
        limit_lines.append("- 分析模式为规则回退：分类与评分基于关键词启发式，未做语义推理，结论仅可用于初筛。")
    if llm_stats.get("calls"):
        cost = (
            f"${llm_stats.get('cost_usd'):.4f}"
            if llm_stats.get("priced")
            else "未配置单价，仅记录 Token"
        )
        limit_lines.append(
            f"- LLM 调用：{llm_stats['calls']} 次，Token {llm_stats.get('total_tokens', 0):,}"
            f"（输入 {llm_stats.get('prompt_tokens', 0):,} / 输出 {llm_stats.get('completion_tokens', 0):,}），估算成本 {cost}。"
        )
    if db_summary:
        limit_lines.append(
            f"- 历史库：公司快照 {db_summary.get('companies', 0)} 行，"
            f"材料 {db_summary.get('materials', 0)} 行，事件 {db_summary.get('events', 0)} 行，"
            f"LLM 调用记录 {db_summary.get('llm_calls', 0)} 行。"
        )
    limit_lines.append("- 新闻发布时间仅精确到日期，非精确到分钟；未获取全文的报道证据等级较低。")
    limit_lines.append("- 最新价与收盘价口径不同：本报告不以盘中价代替收盘价，市场状态以采集时点为准。")

    footer = [
        "",
        "---",
        f"生成时间：{_local_time(run.get('finished_at'), tz_name)} 北京时间｜"
        f"事件总数：{len(events)}｜报告事件：{len(core)}",
    ]

    return "\n".join(
        header + core_lines + company_lines + trend_lines + market_lines + watch_lines + limit_lines + footer
    ) + "\n"
