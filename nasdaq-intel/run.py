"""CLI 入口：一次运行即完成「排名 → 采集 → 去重 → 分类 → 分析 → 日报 → 归档」。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nasdaq_intel.config import load_config, resolve_path
from nasdaq_intel.http import HttpClient
from nasdaq_intel.llm import LLMClient
from nasdaq_intel.pipeline.analyze import analyze_events
from nasdaq_intel.pipeline.classify import classify_clusters
from nasdaq_intel.pipeline.normalize import cluster_items
from nasdaq_intel.pipeline.report import build_report
from nasdaq_intel.sources import filings, news, ranking
from nasdaq_intel.storage import Storage

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def build_http(cfg: dict) -> HttpClient:
    http_cfg = cfg["http"]
    return HttpClient(
        user_agent=http_cfg["user_agent"],
        sec_user_agent=http_cfg.get("sec_user_agent"),
        timeout=int(http_cfg["timeout"]),
        max_retries=int(http_cfg["max_retries"]),
        min_intervals={
            "sec.gov": float(http_cfg.get("sec_min_interval", 0.2)),
            "nasdaq.com": float(http_cfg.get("nasdaq_min_interval", 0.3)),
            "nasdaqtrader.com": 0.3,
        },
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.top:
        cfg["ranking"]["top_n"] = args.top
    if args.no_llm:
        cfg["llm"]["api_key_env"] = "__DISABLED__"

    root = Path(cfg["_root"])
    storage = Storage(
        data_dir=resolve_path(cfg, "data_dir"),
        cache_dir=resolve_path(cfg, "cache_dir"),
    )
    http = build_http(cfg)
    llm = LLMClient(
        cfg["llm"],
        log_dir=storage.data_dir / "llm_logs" if cfg["llm"].get("audit", True) else None,
    )
    day = date.fromisoformat(args.date) if args.date else date.today()

    started = datetime.now(timezone.utc)
    nodes: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []

    def node(name: str, status: str, detail: str = "") -> None:
        nodes.append({"name": name, "status": status, "detail": detail})

    # 1) 市值排名
    t0 = time.time()
    try:
        snapshot = ranking.build_snapshot(http, day=day, cfg=cfg)
        storage.save_ranking(day, snapshot)
        node("ranking", "ok", f"{snapshot['company_count']} 家公司，候选池 {snapshot['universe_size']}")
        warnings.extend(snapshot.get("warnings") or [])
    except Exception as exc:
        errors.append(f"市值排名失败：{exc}")
        node("ranking", "failed", str(exc))
        return _finish(storage, started, nodes, errors, warnings, day, report_path=None, exit_code=2)

    prev_snapshot = storage.previous_ranking(day)
    companies = snapshot["companies"][: args.limit or cfg["ranking"]["top_n"]]

    # 2) 采集窗口
    last_run = storage.last_success_run_time()
    lookback_hours = int(cfg["collect"]["lookback_hours"])
    lookback_days = lookback_hours // 24 or 1
    if last_run:
        delta_hours = int((datetime.now(timezone.utc) - last_run).total_seconds() // 3600)
        lookback_days = max(1, min(delta_hours // 24 + 1, 7))
    lookback_start = day - timedelta(days=lookback_days)
    node("window", "ok", f"新闻窗口自 {lookback_start.isoformat()}")

    # 3) 新闻采集
    t1 = time.time()
    raw_news, news_errors = news.collect_news(
        http,
        companies=companies,
        lookback_start=lookback_start,
        collect_cfg=cfg["collect"],
    )
    errors.extend(news_errors)
    node("news", "ok" if raw_news else "empty", f"{len(raw_news)} 条原始新闻")

    # 4) SEC 申报
    try:
        cik_map = filings.load_cik_map(http, storage.cache_file("company_tickers.json"))
        raw_filings, filing_errors = filings.collect_filings(
            http,
            companies=companies,
            cik_map=cik_map,
            lookback_start=lookback_start,
            sec_forms=cfg["collect"]["sec_forms"],
            max_per_company=int(cfg["collect"]["sec_max_per_company"]),
        )
        errors.extend(filing_errors)
        node("filings", "ok" if raw_filings else "empty", f"{len(raw_filings)} 条 SEC 申报")
    except Exception as exc:
        raw_filings = []
        errors.append(f"SEC 申报采集失败：{exc}")
        node("filings", "failed", str(exc))

    # 5) 去重归并
    clusters = cluster_items(
        raw_news + raw_filings,
        threshold=float(cfg["dedupe"]["title_similarity_threshold"]),
        companies=companies,
    )
    node("dedupe", "ok", f"{len(raw_news) + len(raw_filings)} 条原始材料 → {len(clusters)} 个事件")

    # 6) 分类评分
    events, cls_notes = classify_clusters(
        clusters,
        llm=llm,
        prompts_dir=PROMPTS_DIR,
        threshold=int(cfg["analysis"]["importance_threshold"]),
        batch_size=int(cfg["analysis"].get("classify_batch_size", 20)),
    )
    warnings.extend(cls_notes)
    node("classify", "ok", f"{len(events)} 个事件，模式：{'LLM' if llm.available else '规则'}")

    # 7) 深度分析
    analyses, ana_errors, used_llm = analyze_events(
        events,
        llm=llm,
        prompts_dir=PROMPTS_DIR,
        threshold=int(cfg["analysis"]["importance_threshold"]),
        max_items=int(cfg["analysis"]["max_deep_analysis"]),
    )
    errors.extend(ana_errors)
    node("analyze", "ok" if used_llm else "skipped", f"{len(analyses)} 个事件完成深度分析")

    # 8) 日报
    llm_mode = "LLM（语义分类与深度分析）" if (llm.available and used_llm) else (
        "LLM（语义分类）" if llm.available else "规则回退（关键词启发式）"
    )
    dataset = {
        "as_of_date": day.isoformat(),
        "generated_at": snapshot["generated_at"],
        "llm_mode": llm_mode,
        "companies": companies,
        "events": [_serializable(e) for e in events],
    }
    storage.save_events(day, dataset)

    finished = datetime.now(timezone.utc)
    run_meta = {
        "finished_at": finished.isoformat(),
        "duration_seconds": round(time.time() - t0, 1),
        "status": "ok" if not any(n["status"] == "failed" for n in nodes) else "partial",
        "errors": errors,
        "warnings": warnings,
    }
    markdown = build_report(
        day=day,
        snapshot=snapshot,
        prev_snapshot=prev_snapshot,
        events=events,
        analyses=analyses,
        llm_mode=llm_mode,
        run=run_meta,
        tz_name=cfg["project"]["report_timezone"],
        max_events=int(cfg["analysis"]["max_events_in_report"]),
        collection={
            "company_count": len(companies),
            "news_count": len(raw_news),
            "filing_count": len(raw_filings),
            "errors": errors,
        },
    )
    report_path = storage.save_report(day, markdown)
    node("report", "ok", str(report_path))

    exit_code = 0 if run_meta["status"] == "ok" else 1
    _finish(storage, started, nodes, errors, warnings, day, report_path=report_path, exit_code=exit_code)
    print(f"\n日报已生成：{report_path}")
    print(f"事件 {len(events)} 个｜原始材料 {len(raw_news) + len(raw_filings)} 条｜状态 {run_meta['status']}")
    if errors:
        print("异常：")
        for err in errors[:5]:
            print(f"  - {err}")
    return exit_code


def cmd_rank(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.top:
        cfg["ranking"]["top_n"] = args.top
    http = build_http(cfg)
    storage = Storage(resolve_path(cfg, "data_dir"), resolve_path(cfg, "cache_dir"))
    day = date.fromisoformat(args.date) if args.date else date.today()
    snapshot = ranking.build_snapshot(http, day=day, cfg=cfg)
    path = storage.save_ranking(day, snapshot)
    print(f"排名快照已保存：{path}")
    for company in snapshot["companies"]:
        print(
            f"{company['rank']:>2}. {company['company_name'][:40]:<40} "
            f"{company['ticker']:<6} {_fmt(company['market_cap'])}"
        )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    storage = Storage(resolve_path(cfg, "data_dir"), resolve_path(cfg, "cache_dir"))
    log_dir = storage.data_dir / "llm_logs"
    day = date.fromisoformat(args.date) if args.date else date.today()
    path = log_dir / f"{day.isoformat()}.jsonl"
    if not path.exists():
        print(f"没有日志：{path}")
        return 1

    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if args.label and not str(entry.get("label", "")).startswith(args.label):
            continue
        entries.append(entry)
    if not entries:
        print(f"没有匹配记录：{path}")
        return 1

    shown = entries[-args.limit :]
    print(f"{path}｜匹配 {len(entries)} 条，显示最近 {len(shown)} 条")
    for entry in shown:
        if args.full:
            print(json.dumps(entry, ensure_ascii=False, indent=2))
            continue
        print(
            f"\n[{entry.get('ts', '')}] {entry.get('label', '')} · {entry.get('status', '')} · "
            f"{entry.get('duration_ms', 0)}ms · {entry.get('model', '')} · {entry.get('request_id', '')}"
        )
        user_text = (entry.get("request") or {}).get("user", "")
        print("  input :", user_text[:220].replace("\n", " "))
        output = entry.get("response") or entry.get("error") or ""
        print("  output:", output[:220].replace("\n", " "))
    return 0


def _fmt(value: float) -> str:
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    return f"${value:,.0f}"


def _serializable(event: dict) -> dict:
    out = {k: v for k, v in event.items() if k != "_cluster"}
    cluster = event["_cluster"]
    out["company_name"] = cluster.company_name
    out["sources"] = cluster.sources
    related = cluster.related_companies
    out["related_tickers"] = [item["ticker"] for item in related]
    out["related_companies"] = related
    return out


def _finish(storage, started, nodes, errors, warnings, day, *, report_path, exit_code) -> int:
    finished = datetime.now(timezone.utc)
    entry = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "status": "ok" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "nodes": nodes,
        "errors": errors,
        "warnings": warnings,
        "report": str(report_path) if report_path else None,
    }
    storage.append_run(entry)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纳斯达克科技与资本情报系统 MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="完整流水线：采集 → 分析 → 日报")
    run.add_argument("--date", help="运行日期（YYYY-MM-DD），默认今天")
    run.add_argument("--top", type=int, help="追踪前 N 家公司")
    run.add_argument("--limit", type=int, help="只处理前 N 家公司（用于验证闭环，例如 3）")
    run.add_argument("--no-llm", action="store_true", help="强制使用规则模式")
    run.add_argument("--config", help="配置文件路径")
    run.set_defaults(func=cmd_run)

    rank = sub.add_parser("rank", help="只生成市值排名快照")
    rank.add_argument("--date", help="运行日期（YYYY-MM-DD）")
    rank.add_argument("--top", type=int)
    rank.add_argument("--config")
    rank.set_defaults(func=cmd_rank)

    audit = sub.add_parser("audit", help="查看 LLM 原始输入/输出日志")
    audit.add_argument("--date", help="日志日期（YYYY-MM-DD），默认今天")
    audit.add_argument("--label", help="按 label 过滤，如 classify / analyze")
    audit.add_argument("--limit", type=int, default=10, help="显示最近 N 条，默认 10")
    audit.add_argument("--full", action="store_true", help="输出完整 JSON（含 system prompt）")
    audit.add_argument("--config")
    audit.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
