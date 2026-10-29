"""规则层验收：去重、分类评分、LLM 审计日志。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nasdaq_intel.llm import LLMClient, LLMError
from nasdaq_intel.pipeline.classify import classify_clusters
from nasdaq_intel.pipeline.normalize import cluster_items

PROMPTS_DIR = PROJECT_ROOT / "prompts"
DAY = "2026-10-29"


class NoLLM:
    """未配置 Key 的客户端，强制走规则回退。"""

    available = False


def make_item(
    *,
    ticker: str,
    title: str,
    summary: str = "",
    url: str = "",
    kind: str = "news",
    publisher: str = "RTTNews",
    published: str = DAY,
    company_name: str | None = None,
    category_hint: str | None = None,
) -> dict:
    return {
        "id": url or title,
        "ticker": ticker,
        "company_name": company_name or f"{ticker} Inc.",
        "kind": kind,
        "source_name": f"Nasdaq News / {publisher}",
        "publisher": publisher,
        "title": title,
        "summary": summary or title,
        "url": url or f"https://example.com/{abs(hash(title))}",
        "published_date": published,
        "published_precision": "date",
        "fetched_at": f"{DAY}T12:00:00+00:00",
        "category_hint": category_hint,
    }


def event_for(items: list[dict], threshold: int = 4) -> dict:
    clusters = cluster_items(items)
    events, _ = classify_clusters(clusters, llm=NoLLM(), prompts_dir=PROMPTS_DIR, threshold=threshold)
    assert len(events) == 1, f"期望 1 个事件，实际 {len(events)}"
    return events[0]


class DedupeTests(unittest.TestCase):
    def test_same_url_with_tracking_params_merges(self) -> None:
        items = [
            make_item(ticker="NVDA", title="NVIDIA unveils AI chip", url="https://www.nasdaq.com/a/x?utm_source=feed"),
            make_item(ticker="NVDA", title="NVIDIA unveils AI chip", url="https://www.nasdaq.com/a/x?utm_campaign=daily"),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].sources), 1)  # 同一 URL 只算一个来源

    def test_near_duplicate_titles_merge_across_publishers(self) -> None:
        items = [
            make_item(ticker="NVDA", title="NVIDIA unveils next-gen AI chip platform",
                      url="https://a.example/nvda1", publisher="RTTNews"),
            make_item(ticker="NVDA", title="NVIDIA unveils next generation AI chip platform",
                      url="https://b.example/nvda2", publisher="Reuters"),
        ]
        clusters = cluster_items(items, threshold=0.86)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].sources), 2)

    def test_same_event_written_differently_merges(self) -> None:
        items = [
            make_item(ticker="GOOGL", title="Alphabet Q3 Profit Falls Despite 25% Revenue Growth"),
            make_item(ticker="GOOGL", title="Alphabet Inc. Q3 Profit Drops"),
        ]
        clusters = cluster_items(items, threshold=0.86)
        self.assertEqual(len(clusters), 1)

    def test_distinct_events_same_day_stay_separate(self) -> None:
        items = [
            make_item(ticker="NVDA", title="NVIDIA unveils new AI chip platform"),
            make_item(ticker="NVDA", title="NVIDIA announces $10 billion buyback"),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 2)

    def test_same_title_different_companies_never_merge(self) -> None:
        items = [
            make_item(ticker="AAPL", title="Company announces record quarter"),
            make_item(ticker="MSFT", title="Company announces record quarter"),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 2)


class ClassificationTests(unittest.TestCase):
    def test_merger_with_big_amount_is_capital_5(self) -> None:
        event = event_for([
            make_item(
                ticker="MSFT",
                title="Microsoft to acquire AI startup in $7 billion deal",
                summary="Microsoft agreed to acquire the company for $7 billion in cash.",
            )
        ])
        self.assertEqual(event["category"], "capital")
        self.assertEqual(event["importance"], 5)

    def test_earnings_guidance_is_financial_4(self) -> None:
        event = event_for([
            make_item(
                ticker="AAPL",
                title="Apple raises full-year guidance after record revenue",
                summary="Apple reported record revenue and raised its outlook.",
            )
        ])
        self.assertEqual(event["category"], "financial")
        self.assertEqual(event["importance"], 4)

    def test_product_launch_is_technology_4(self) -> None:
        event = event_for([
            make_item(
                ticker="AAPL",
                title="Apple introduces M5 chip with on-device AI",
                summary="Apple unveiled a new chip and device lineup.",
            )
        ])
        self.assertEqual(event["category"], "technology")
        self.assertEqual(event["importance"], 4)

    def test_partnership_is_business_3(self) -> None:
        event = event_for([
            make_item(
                ticker="AMZN",
                title="Amazon signs multi-year cloud partnership with airline",
                summary="The agreement covers cloud infrastructure and services.",
            )
        ])
        self.assertEqual(event["category"], "business")
        self.assertEqual(event["importance"], 3)

    def test_opinion_capped_at_2_and_needs_verification(self) -> None:
        event = event_for([
            make_item(
                ticker="NVDA",
                title="Should You Buy Nvidia Stock Now?",
                summary="Our analyst explains why the stock could double.",
                publisher="The Motley Fool",
                url="https://fool.example/nvda",
            )
        ])
        self.assertEqual(event["content_type"], "opinion")
        self.assertEqual(event["importance"], 2)
        self.assertTrue(event["needs_verification"])
        self.assertLessEqual(event["confidence"], 0.5)

    def test_routine_notice_is_importance_1(self) -> None:
        event = event_for([
            make_item(
                ticker="NVDA",
                title="Nvidia options now available for trading",
                summary="New options contracts are listed.",
            )
        ])
        self.assertEqual(event["importance"], 1)

    def test_sec_10q_is_official_financial(self) -> None:
        event = event_for([
            make_item(
                ticker="GOOGL",
                title="Alphabet Inc. · 10-Q 季度报告",
                summary="Alphabet Inc. 于 2026-10-29 向 SEC 提交 10-Q 文件。",
                kind="filing",
                publisher="SEC EDGAR",
                url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
                category_hint="financial",
            )
        ])
        self.assertEqual(event["category"], "financial")
        self.assertEqual(event["content_type"], "official")
        self.assertFalse(event["needs_verification"])

    def test_press_release_is_official(self) -> None:
        event = event_for([
            make_item(
                ticker="MSFT",
                title="Microsoft to invest $10 billion in AI infrastructure",
                summary="Microsoft announced a multi-year investment.",
                kind="press_release",
                publisher="PR Newswire",
                url="https://pr.example/msft",
            )
        ])
        self.assertEqual(event["content_type"], "official")
        self.assertEqual(event["importance"], 4)
        self.assertFalse(event["needs_verification"])

    def test_regulatory_fine_is_other_3(self) -> None:
        event = event_for([
            make_item(
                ticker="AAPL",
                title="South Korea fines Apple over app store payment restrictions",
                summary="The regulator ordered corrective measures.",
            )
        ])
        self.assertEqual(event["category"], "other")
        self.assertEqual(event["importance"], 3)


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        os.environ["TEST_LLM_KEY"] = "sk-secret-should-not-be-logged"
        self.client = LLMClient(
            {
                "base_url": "https://llm.example/v1",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "temperature": 0.2,
                "max_tokens": 100,
                "timeout": 30,
            },
            log_dir=self.log_dir,
        )

    def tearDown(self) -> None:
        os.environ.pop("TEST_LLM_KEY", None)
        self.tmp.cleanup()

    def _fake_response(self, content: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
        return resp

    def test_logs_raw_input_and_output(self) -> None:
        with patch("nasdaq_intel.llm.requests.post", return_value=self._fake_response('{"items": []}')):
            parsed = self.client.chat_json("SYSTEM_PROMPT", '{"news_items": []}', label="classify:batch1")
        self.assertEqual(parsed, {"items": []})

        files = list(self.log_dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["label"], "classify:batch1")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["request"]["system"], "SYSTEM_PROMPT")
        self.assertEqual(entry["request"]["user"], '{"news_items": []}')
        self.assertEqual(entry["response"], '{"items": []}')
        self.assertEqual(entry["parsed"], {"items": []})
        self.assertEqual(entry["usage"]["total_tokens"], 18)
        self.assertNotIn("sk-secret", json.dumps(entry, ensure_ascii=False))

    def test_parse_error_is_logged(self) -> None:
        with patch("nasdaq_intel.llm.requests.post", return_value=self._fake_response("not json")):
            with self.assertRaises(LLMError):
                self.client.chat_json("S", "U", label="analyze:x")
        entry = json.loads(list(self.log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["status"], "parse_error")
        self.assertEqual(entry["response"], "not json")
        self.assertIn("无法解析", entry["error"])


class PromptWiringTests(unittest.TestCase):
    """验证 Prompt 占位符已移除、调用带 label、原始输入输出落到审计日志。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        os.environ["TEST_LLM_KEY"] = "sk-test"
        self.client = LLMClient(
            {
                "base_url": "https://llm.example/v1",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "timeout": 30,
            },
            log_dir=self.log_dir,
        )
        self.items = [
            make_item(
                ticker="NVDA",
                title="NVIDIA unveils next-gen AI chip platform",
                summary="NVIDIA announced a new chip architecture for data centers.",
            )
        ]

    def tearDown(self) -> None:
        os.environ.pop("TEST_LLM_KEY", None)
        self.tmp.cleanup()

    def _response(self, payload: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"total_tokens": 10},
        }
        return resp

    def _latest_log(self) -> dict:
        path = sorted(self.log_dir.glob("*.jsonl"))[-1]
        return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])

    def test_classify_prompt_and_log(self) -> None:
        clusters = cluster_items(self.items)
        key = clusters[0].event_key
        payload = {
            "items": [
                {
                    "event_key": key,
                    "ticker": "NVDA",
                    "title": "NVIDIA unveils next-gen AI chip platform",
                    "category": "technology",
                    "importance": 5,
                    "confidence": 0.9,
                    "is_duplicate": False,
                    "fact_summary": "NVIDIA announced a new chip architecture.",
                    "needs_verification": False,
                }
            ]
        }
        with patch("nasdaq_intel.llm.requests.post", return_value=self._response(payload)):
            events, _ = classify_clusters(clusters, llm=self.client, prompts_dir=PROMPTS_DIR, threshold=4)

        self.assertEqual(events[0]["mode"], "llm")
        self.assertEqual(events[0]["importance"], 5)
        entry = self._latest_log()
        self.assertEqual(entry["label"], "classify:batch1")
        self.assertIn("分类（category", entry["request"]["system"])
        self.assertNotIn("{{news_items}}", entry["request"]["system"])
        self.assertIn(key, entry["request"]["user"])
        self.assertEqual(entry["parsed"], payload)

    def test_analyze_prompt_and_log(self) -> None:
        from nasdaq_intel.pipeline.analyze import analyze_events

        clusters = cluster_items(self.items)
        events, _ = classify_clusters(clusters, llm=NoLLM(), prompts_dir=PROMPTS_DIR, threshold=4)
        payload = {
            "event_key": events[0]["event_key"],
            "facts": ["NVIDIA announced a new chip architecture."],
            "strategic_analysis": "巩固数据中心算力份额。",
            "technology_impact": "提升训练效率。",
            "capital_impact": "未披露。",
            "competitive_impact": "对同业形成压力。",
            "follow_up_signals": ["数据中心收入增速"],
            "limitations": ["仅基于标题与摘要"],
            "source_urls": ["https://example.com/nvda"],
        }
        with patch("nasdaq_intel.llm.requests.post", return_value=self._response(payload)):
            analyses, errors, used = analyze_events(
                events, llm=self.client, prompts_dir=PROMPTS_DIR, threshold=4, max_items=2
            )
        self.assertTrue(used)
        self.assertEqual(errors, [])
        entry = self._latest_log()
        self.assertTrue(entry["label"].startswith("analyze:"))
        self.assertIn(events[0]["event_key"], entry["label"])
        self.assertEqual(entry["parsed"]["facts"], payload["facts"])


if __name__ == "__main__":
    unittest.main()
