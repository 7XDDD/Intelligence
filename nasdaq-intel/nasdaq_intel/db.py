"""SQLite 历史存储：公司名单快照、新闻材料、事件分析、行情、运行日志、LLM 调用。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    snapshot_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    rank INTEGER,
    tickers TEXT,
    company_name TEXT,
    market_cap REAL,
    sector TEXT,
    industry TEXT,
    country TEXT,
    PRIMARY KEY (snapshot_date, ticker)
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    report_date TEXT,
    ticker TEXT,
    kind TEXT,
    title TEXT,
    summary TEXT,
    url TEXT,
    publisher TEXT,
    published_date TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    report_date TEXT,
    event_key TEXT,
    ticker TEXT,
    company_name TEXT,
    title TEXT,
    category TEXT,
    importance INTEGER,
    confidence REAL,
    content_type TEXT,
    topics_json TEXT,
    fact_summary TEXT,
    needs_verification INTEGER,
    related_tickers TEXT,
    sources_json TEXT,
    analysis_json TEXT,
    updated_at TEXT,
    PRIMARY KEY (report_date, event_key)
);
CREATE TABLE IF NOT EXISTS market (
    report_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    current_price REAL,
    previous_close REAL,
    daily_close REAL,
    change REAL,
    change_pct REAL,
    change_basis TEXT,
    quote_timestamp TEXT,
    market_status TEXT,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (report_date, ticker)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    report_date TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    report_path TEXT,
    nodes_json TEXT,
    errors_json TEXT,
    warnings_json TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    request_id TEXT PRIMARY KEY,
    ts TEXT,
    label TEXT,
    model TEXT,
    status TEXT,
    duration_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(report_date);
CREATE INDEX IF NOT EXISTS idx_materials_date ON materials(report_date);
CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_calls(ts);
"""


class IntelligenceDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self._migrate_events_pk()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate_events_pk(self) -> None:
        """旧库 events 以 event_key 单列做主键，历史重跑会覆盖其他日期的记录，迁移为 (报告日, event_key)。"""
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if not exists:
            return
        info = self.conn.execute("PRAGMA table_info(events)").fetchall()
        columns = [row[1] for row in info]
        pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        if pk == ["report_date", "event_key"] and "topics_json" in columns:
            return
        topics_expr = "topics_json" if "topics_json" in columns else "NULL"
        self.conn.executescript(
            f"""
            ALTER TABLE events RENAME TO events_legacy;
            CREATE TABLE events (
                report_date TEXT,
                event_key TEXT,
                ticker TEXT,
                company_name TEXT,
                title TEXT,
                category TEXT,
                importance INTEGER,
                confidence REAL,
                content_type TEXT,
                topics_json TEXT,
                fact_summary TEXT,
                needs_verification INTEGER,
                related_tickers TEXT,
                sources_json TEXT,
                analysis_json TEXT,
                updated_at TEXT,
                PRIMARY KEY (report_date, event_key)
            );
            INSERT OR IGNORE INTO events SELECT
                report_date, event_key, ticker, company_name, title, category, importance,
                confidence, content_type, {topics_expr}, fact_summary, needs_verification,
                related_tickers, sources_json, analysis_json, updated_at
            FROM events_legacy;
            DROP TABLE events_legacy;
            """
        )

    def close(self) -> None:
        self.conn.close()

    def record_ranking(self, day: date, snapshot: dict[str, Any]) -> None:
        rows = [
            (
                day.isoformat(),
                company.get("ticker"),
                company.get("rank"),
                json.dumps(company.get("tickers") or [], ensure_ascii=False),
                company.get("company_name"),
                company.get("market_cap"),
                company.get("sector"),
                company.get("industry"),
                company.get("country"),
            )
            for company in snapshot.get("companies") or []
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def record_materials(self, day: date, items: list[dict[str, Any]]) -> None:
        rows = [
            (
                item.get("id"),
                day.isoformat(),
                item.get("ticker"),
                item.get("kind"),
                item.get("title"),
                item.get("summary"),
                item.get("url"),
                item.get("publisher"),
                item.get("published_date"),
                item.get("fetched_at"),
            )
            for item in items
            if item.get("id")
        ]
        with self.conn:
            self.conn.execute("DELETE FROM materials WHERE report_date = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT OR REPLACE INTO materials VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def record_market(self, day: date, snapshots: list[dict[str, Any]]) -> None:
        rows = [
            (
                day.isoformat(),
                snap.get("ticker"),
                snap.get("company_name"),
                snap.get("current_price"),
                snap.get("previous_close"),
                snap.get("daily_close"),
                snap.get("change"),
                snap.get("change_pct"),
                snap.get("change_basis"),
                snap.get("quote_timestamp"),
                snap.get("market_status"),
                snap.get("source"),
                snap.get("fetched_at"),
            )
            for snap in snapshots
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO market VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def record_events(
        self,
        day: date,
        events: list[dict[str, Any]],
        analyses: dict[str, dict[str, Any]],
    ) -> None:
        rows = []
        for event in events:
            cluster = event.get("_cluster")
            related = cluster.related_companies if cluster else []
            rows.append(
                (
                    day.isoformat(),
                    event.get("event_key"),
                    event.get("ticker"),
                    cluster.company_name if cluster else "",
                    event.get("title"),
                    event.get("category"),
                    event.get("importance"),
                    event.get("confidence"),
                    event.get("content_type"),
                    json.dumps(event.get("topics") or [], ensure_ascii=False),
                    event.get("fact_summary"),
                    1 if event.get("needs_verification") else 0,
                    json.dumps([item["ticker"] for item in related], ensure_ascii=False),
                    json.dumps(event.get("sources") or (cluster.sources if cluster else []), ensure_ascii=False),
                    json.dumps(analyses.get(event.get("event_key")), ensure_ascii=False),
                    event.get("updated_at") or "",
                )
            )
        with self.conn:
            self.conn.execute("DELETE FROM events WHERE report_date = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def record_run(self, entry: dict[str, Any], day: date) -> None:
        row = (
            entry.get("run_id") or f"{entry.get('started_at')}-{uuid.uuid4().hex[:8]}",
            day.isoformat(),
            entry.get("started_at"),
            entry.get("finished_at"),
            entry.get("status"),
            entry.get("report"),
            json.dumps(entry.get("nodes") or [], ensure_ascii=False),
            json.dumps(entry.get("errors") or [], ensure_ascii=False),
            json.dumps(entry.get("warnings") or [], ensure_ascii=False),
        )
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            )

    def sync_llm_logs(self, log_dir: Path) -> int:
        """把 JSONL 审计日志补录到 SQLite（按 request_id 去重）。"""
        inserted = 0
        for path in sorted(Path(log_dir).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                usage = entry.get("usage") or {}
                row = (
                    entry.get("request_id"),
                    entry.get("ts"),
                    entry.get("label"),
                    entry.get("model"),
                    entry.get("status"),
                    entry.get("duration_ms"),
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                    entry.get("cost_usd"),
                )
                with self.conn:
                    cursor = self.conn.execute(
                        "INSERT OR IGNORE INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                        row,
                    )
                    inserted += cursor.rowcount
        return inserted

    def summary(self, day: date) -> dict[str, int]:
        cursor = self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM companies WHERE snapshot_date = ?),
              (SELECT COUNT(*) FROM materials WHERE report_date = ?),
              (SELECT COUNT(*) FROM events WHERE report_date = ?),
              (SELECT COUNT(*) FROM llm_calls WHERE substr(ts, 1, 10) = ?)
            """,
            (day.isoformat(),) * 4,
        )
        companies, materials, events, llm_calls = cursor.fetchone()
        return {
            "companies": companies,
            "materials": materials,
            "events": events,
            "llm_calls": llm_calls,
        }
