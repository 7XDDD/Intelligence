"""系统级测试：交易日历、行情口径、质量校验、SQLite、日报结构与重试分类。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nasdaq_intel import quality
from nasdaq_intel.db import IntelligenceDB
from nasdaq_intel.http import HttpClient, HttpError
from nasdaq_intel.llm import LLMClient
from nasdaq_intel.market_calendar import is_trading_day, latest_trading_day, market_phase, ny_today
from nasdaq_intel.pipeline.normalize import cluster_items
from nasdaq_intel.pipeline.report import build_report
from nasdaq_intel.sources import market


class CalendarTests(unittest.TestCase):
    def test_weekend_not_trading(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 10, 31)))  # Saturday

    def test_thanksgiving_not_trading(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 11, 26)))
        self.assertEqual(latest_trading_day(date(2026, 11, 26)), date(2026, 11, 25))

    def test_latest_trading_day_on_weekend(self) -> None:
        self.assertEqual(latest_trading_day(date(2026, 10, 31)), date(2026, 10, 30))

    def test_market_phase_after_close(self) -> None:
        moment = datetime(2026, 10, 30, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(market_phase(moment), "after")


class FakeMarketHttp:
    def __init__(self, quote: dict, rows: list[dict]) -> None:
        self.quote = quote
        self.rows = rows

    def get_json(self, url: str, *, params=None, headers=None):
        if "historical" in url:
            return {"data": {"tradesTable": {"rows": self.rows}}}
        return {"data": self.quote}


def quote_payload(status: str = "Open", stamp: str = "Oct 30, 2026 3:59 PM ET") -> dict:
    return {
        "symbol": "NVDA",
        "marketStatus": status,
        "currency": "USD",
        "primaryData": {"lastSalePrice": "$208.51", "lastTradeTimestamp": stamp, "volume": "28,071,874"},
        "secondaryData": None,
    }


class MarketTests(unittest.TestCase):
    COMPANY = {"ticker": "NVDA", "company_name": "NVIDIA Corporation"}

    def test_parse_helpers(self) -> None:
        self.assertEqual(market.parse_number("$208.51"), 208.51)
        self.assertEqual(market.parse_number("+1.89%"), 1.89)
        self.assertEqual(market.parse_history_date("10/30/2026"), date(2026, 10, 30))
        stamp = market.parse_quote_time("Oct 30, 2026 3:59 PM ET")
        self.assertIsNotNone(stamp)
        self.assertTrue(stamp.endswith("-04:00") or stamp.endswith("-05:00"))

    def test_close_and_previous_close_are_separate(self) -> None:
        http = FakeMarketHttp(
            quote_payload(),
            [
                {"date": "10/30/2026", "close": "$204.64", "open": "$208.94"},
                {"date": "10/29/2026", "close": "$213.66", "open": "$214.36"},
            ],
        )
        with patch("nasdaq_intel.sources.market.ny_today", return_value=date(2026, 10, 30)):
            snap = market.build_snapshot(http, company=self.COMPANY, day=date(2026, 10, 30))
        self.assertEqual(snap["daily_close"], 204.64)
        self.assertEqual(snap["previous_close"], 213.66)
        self.assertEqual(snap["current_price"], 208.51)
        self.assertEqual(snap["price_date"], "2026-10-30")
        self.assertEqual(snap["change_basis"], "close")
        self.assertAlmostEqual(snap["change"], -9.02)
        self.assertNotEqual(snap["daily_close"], snap["current_price"])

    def test_intraday_does_not_invent_close(self) -> None:
        http = FakeMarketHttp(
            quote_payload(),
            [{"date": "10/30/2026", "close": "$204.64", "open": "$208.94"}],
        )
        with patch("nasdaq_intel.sources.market.ny_today", return_value=date(2026, 11, 2)):
            snap = market.build_snapshot(http, company=self.COMPANY, day=date(2026, 11, 2))
        self.assertIsNone(snap["daily_close"])
        self.assertEqual(snap["previous_close"], 204.64)
        self.assertEqual(snap["change_basis"], "intraday")

    def test_quality_flags_missing_close_for_past_day(self) -> None:
        snap = {
            "ticker": "NVDA",
            "current_price": 208.51,
            "previous_close": 204.64,
            "daily_close": None,
            "price_date": None,
            "quote_timestamp": "2026-10-30T15:59:00-04:00",
            "change": 3.87,
            "change_basis": "intraday",
            "historical": False,
        }
        issues = quality.validate_market([snap], day=date(2026, 10, 30), today=date(2026, 11, 2))
        self.assertTrue(any(issue["code"] == "missing_daily_close" for issue in issues))

    def test_historical_snapshot_has_no_current_price(self) -> None:
        http = FakeMarketHttp(
            quote_payload(),
            [
                {"date": "10/30/2026", "close": "$204.64", "open": "$208.94"},
                {"date": "10/29/2026", "close": "$213.66", "open": "$214.36"},
            ],
        )
        snap = market.build_snapshot(http, company=self.COMPANY, day=date(2026, 10, 30))
        self.assertTrue(snap["historical"])
        self.assertIsNone(snap["current_price"])
        self.assertIsNone(snap["quote_timestamp"])
        self.assertEqual(snap["daily_close"], 204.64)
        self.assertEqual(snap["market_status"], "Historical")


class ClassificationTopicsTests(unittest.TestCase):
    def test_rule_topics_cover_document_dimensions(self) -> None:
        from nasdaq_intel.pipeline.classify import derive_topics

        topics = derive_topics("Microsoft to acquire AI startup in $7 billion deal", "capital")
        self.assertIn("investment", topics)
        self.assertIn("ai_models", topics)

        topics = derive_topics("NVIDIA unveils next-gen AI chip for data centers", "technology")
        self.assertIn("chips_infra", topics)
        self.assertIn("product", topics)


class QualityAndDbTests(unittest.TestCase):
    def test_report_structure_validation(self) -> None:
        issues = quality.validate_report(
            "# 报告\n生成时间：2026-10-30 21:01\n正文",
            ["## 一、今日核心情报", "## 二、公司动态"],
        )
        self.assertEqual(len(issues), 2)

    def test_sqlite_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = IntelligenceDB(Path(tmp) / "test.db")
            day = date(2026, 10, 30)
            db.record_ranking(
                day,
                {
                    "companies": [
                        {
                            "ticker": "NVDA",
                            "rank": 1,
                            "tickers": ["NVDA"],
                            "company_name": "NVIDIA Corporation",
                            "market_cap": 5.19e12,
                            "sector": "Technology",
                            "industry": "Semiconductors",
                            "country": "United States",
                        }
                    ]
                },
            )
            db.record_materials(
                day,
                [{"id": "m1", "ticker": "NVDA", "kind": "news", "title": "t", "url": "https://x"}],
            )
            db.record_events(
                day,
                [
                    {
                        "event_key": "nvda-1",
                        "ticker": "NVDA",
                        "title": "t",
                        "category": "technology",
                        "importance": 4,
                        "confidence": 0.8,
                        "content_type": "news",
                        "fact_summary": "f",
                        "needs_verification": True,
                        "sources": [{"url": "https://x"}],
                    }
                ],
                {},
            )
            db.record_run(
                {
                    "started_at": "2026-10-30T21:00:00+00:00",
                    "finished_at": "2026-10-30T21:01:00+00:00",
                    "status": "ok",
                    "nodes": [],
                    "errors": [],
                    "warnings": [],
                    "report": "/tmp/r.md",
                },
                day,
            )
            summary = db.summary(day)
            db.close()
        self.assertEqual(summary["companies"], 1)
        self.assertEqual(summary["materials"], 1)
        self.assertEqual(summary["events"], 1)

    def test_events_primary_key_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(path))
            conn.executescript(
                """
                CREATE TABLE events (
                    event_key TEXT PRIMARY KEY, report_date TEXT, ticker TEXT, company_name TEXT,
                    title TEXT, category TEXT, importance INTEGER, confidence REAL, content_type TEXT,
                    fact_summary TEXT, needs_verification INTEGER, related_tickers TEXT,
                    sources_json TEXT, analysis_json TEXT, updated_at TEXT
                );
                INSERT INTO events VALUES
                ('k1','2026-10-30','NVDA','NVIDIA','t','technology',4,0.8,'news','f',0,'[]','[]','{}','');
                """
            )
            conn.commit()
            conn.close()
            db = IntelligenceDB(path)
            info = db.conn.execute("PRAGMA table_info(events)").fetchall()
            pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            self.assertEqual(pk, ["report_date", "event_key"])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            db.close()


class ReportStructureTests(unittest.TestCase):
    def test_sections_and_market_disclaimer(self) -> None:
        companies = [
            {
                "rank": 1,
                "ticker": "NVDA",
                "tickers": ["NVDA"],
                "company_name": "NVIDIA Corporation",
                "market_cap": 5.19e12,
                "sector": "Technology",
            }
        ]
        snapshot = {
            "generated_at": "2026-10-30T21:00:00+00:00",
            "data_source": "test",
            "status": "ok",
            "ranking_method": "market_cap_desc",
            "universe_size": 3000,
            "companies": companies,
        }
        item = {
            "id": "x",
            "ticker": "NVDA",
            "company_name": "NVIDIA Corporation",
            "kind": "news",
            "source_name": "Nasdaq News",
            "publisher": "RTTNews",
            "title": "NVIDIA unveils next-gen AI chip platform",
            "summary": "NVIDIA announced a new chip.",
            "url": "https://x/nvda",
            "published_date": "2026-10-30",
            "fetched_at": "2026-10-30T12:00:00+00:00",
        }
        cluster = cluster_items([item])[0]
        event = {
            "event_key": cluster.event_key,
            "ticker": "NVDA",
            "title": item["title"],
            "category": "technology",
            "importance": 4,
            "confidence": 0.8,
            "content_type": "news",
            "fact_summary": "NVIDIA announced a new chip.",
            "needs_verification": True,
            "_cluster": cluster,
        }
        market_snap = {
            "ticker": "NVDA",
            "company_name": "NVIDIA Corporation",
            "current_price": 208.51,
            "previous_close": 213.66,
            "daily_close": 204.64,
            "quote_timestamp_display": "Oct 30, 2026 3:59 PM ET",
            "market_phase": "after",
            "change": -9.02,
            "change_pct": -4.22,
            "change_basis": "close",
            "fetched_at": "2026-10-31T05:00:00-04:00",
        }
        markdown = build_report(
            day=date(2026, 10, 30),
            snapshot=snapshot,
            prev_snapshot=None,
            events=[event],
            analyses={},
            llm_mode="LLM（语义分类、深度分析与趋势总结）",
            run={
                "status": "ok",
                "finished_at": "2026-10-30T21:01:00+00:00",
                "duration_seconds": 60,
                "errors": [],
                "warnings": [],
            },
            market_snapshots=[market_snap],
            trend_summary={"technology_trends": ["AI 基础设施投入延续"], "competition": [], "capital_signals": []},
            llm_stats={"calls": 3, "total_tokens": 1000, "prompt_tokens": 700, "completion_tokens": 300,
                       "cost_usd": 0.0, "priced": False},
            quality_issues=[],
            db_summary={"companies": 1, "materials": 1, "events": 1, "llm_calls": 3},
        )
        for section in (
            "## 一、今日核心情报",
            "## 二、公司动态",
            "## 三、行业趋势与竞争格局",
            "## 四、市场表现",
            "## 五、后续观察清单",
            "## 六、数据质量与运行说明",
        ):
            self.assertIn(section, markdown)
        self.assertIn("A（4/5）", markdown)
        self.assertIn("不以盘中价代替", markdown)
        self.assertIn("当日收盘", markdown)


class HttpClassificationTests(unittest.TestCase):
    def _client(self) -> HttpClient:
        return HttpClient("ua", max_retries=0)

    def test_permanent_and_temporary_status(self) -> None:
        client = self._client()
        for status, expected in ((401, True), (403, True), (500, False), (429, False)):
            resp = MagicMock(status_code=status, text="err")
            with patch.object(client.session, "get", return_value=resp):
                with self.assertRaises(HttpError) as ctx:
                    client.get("https://example.com/x")
            self.assertEqual(ctx.exception.permanent, expected)


class LlmStatsTests(unittest.TestCase):
    def test_token_and_cost_accounting(self) -> None:
        os.environ["TEST_LLM_KEY"] = "sk-test"
        client = LLMClient(
            {
                "base_url": "https://llm.example/v1",
                "model": "test",
                "api_key_env": "TEST_LLM_KEY",
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
        )
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }
        with patch("nasdaq_intel.llm.requests.post", return_value=resp):
            client.chat_json("s", "u", label="test")
            client.chat_json("s", "u", label="test")
        stats = client.stats()
        os.environ.pop("TEST_LLM_KEY", None)
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["total_tokens"], 3000)
        self.assertAlmostEqual(stats["cost_usd"], 0.004)


if __name__ == "__main__":
    unittest.main()
