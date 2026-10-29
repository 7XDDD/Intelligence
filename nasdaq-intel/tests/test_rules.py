"""规则层验收：去重、分类评分、LLM 审计日志。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from nasdaq_intel.llm import LLMClient, LLMError, parse_json_loose
from nasdaq_intel.pipeline.classify import classify_clusters
from nasdaq_intel.pipeline.normalize import cluster_items, extract_amounts

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

    def test_opinion_confidence_is_capped(self) -> None:
        clusters = cluster_items(
            [
                make_item(
                    ticker="META",
                    title="Meta Just Priced Stock Sale at $625 a Share. I'd Consider Buying.",
                    summary="Meta priced 40 million Class A shares at $625 each in a $25 billion offering.",
                    publisher="The Motley Fool",
                    url="https://fool.example/meta",
                )
            ]
        )
        payload = {
            "items": [
                {
                    "event_key": clusters[0].event_key,
                    "ticker": "META",
                    "title": "Meta priced 40 million shares at $625 each in a $25 billion offering",
                    "category": "capital",
                    "importance": 4,
                    "confidence": 0.9,
                    "is_duplicate": False,
                    "fact_summary": "Meta priced a $25 billion share offering.",
                    "needs_verification": False,
                }
            ]
        }
        with patch("nasdaq_intel.llm.requests.post", return_value=self._response(payload)):
            events, _ = classify_clusters(clusters, llm=self.client, prompts_dir=PROMPTS_DIR, threshold=4)
        self.assertEqual(events[0]["content_type"], "opinion")
        self.assertLessEqual(events[0]["confidence"], 0.6)
        self.assertTrue(events[0]["needs_verification"])

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


class ReportTests(unittest.TestCase):
    def test_duplicate_errors_rendered_once(self) -> None:
        from nasdaq_intel.pipeline.report import build_report

        snapshot = {
            "generated_at": "2026-10-29T12:00:00+00:00",
            "data_source": "test",
            "status": "ok",
            "ranking_method": "market_cap_desc",
            "universe_size": 10,
            "companies": [],
        }
        markdown = build_report(
            day=date(2026, 10, 29),
            snapshot=snapshot,
            prev_snapshot=None,
            events=[],
            analyses={},
            llm_mode="规则回退（关键词启发式）",
            run={
                "status": "ok",
                "finished_at": "2026-10-29T12:00:00+00:00",
                "duration_seconds": 1,
                "errors": ["E1"],
                "warnings": [],
            },
            tz_name="America/New_York",
            collection={"company_count": 0, "news_count": 0, "filing_count": 0, "errors": ["E1"]},
        )
        self.assertEqual(markdown.count("E1"), 1)


class JsonRepairTests(unittest.TestCase):
    def test_unescaped_quotes_inside_string(self) -> None:
        raw = '{"event_key":"x","facts":["仅"电话会提及"这一点"],"source_urls":[]}'
        parsed = parse_json_loose(raw)
        self.assertEqual(parsed["facts"][0], '仅"电话会提及"这一点')

    def test_literal_newline_inside_string(self) -> None:
        raw = '{"facts":["第一行\n第二行"]}'
        parsed = parse_json_loose(raw)
        self.assertEqual(parsed["facts"][0], "第一行\n第二行")

    def test_error_message_does_not_leak_payload(self) -> None:
        with self.assertRaises(LLMError) as ctx:
            parse_json_loose("{not json at all")
        message = str(ctx.exception)
        self.assertNotIn("\n", message)
        self.assertLessEqual(len(message), 200)


class CrossCompanyDedupeTests(unittest.TestCase):
    def test_cross_language_same_investment_merges(self) -> None:
        items = [
            make_item(
                ticker="META",
                company_name="Meta Platforms Inc.",
                title="Nvidia and AMD investing $4 billion in Meta's AI ambitions",
                summary="Nvidia and AMD are putting $4 billion into Meta's AI infrastructure.",
                url="https://a.example/meta-nvda",
            ),
            make_item(
                ticker="NVDA",
                company_name="NVIDIA Corporation",
                title="NVIDIA与AMD向Meta AI投资40亿美元",
                summary="英伟达与AMD向Meta的AI业务投资40亿美元。",
                url="https://b.example/nvda-meta",
            ),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertTrue(cluster.related_companies)
        self.assertEqual(len(cluster.sources), 2)
        self.assertEqual(len(cluster.items), 2)

    def test_cross_company_regulatory_event_merges(self) -> None:
        items = [
            make_item(
                ticker="AAPL",
                company_name="Apple Inc.",
                title="South Korea fines Google and Apple over app store payments",
                summary="The regulator fined both companies and ordered corrective measures.",
                url="https://a.example/fine",
            ),
            make_item(
                ticker="GOOGL",
                company_name="Alphabet Inc.",
                title="Google and Apple fined by South Korea over app store rules",
                summary="Both companies were fined for forcing payment methods.",
                url="https://b.example/fine",
            ),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 1)

    def test_distinct_events_with_mutual_mentions_stay_separate(self) -> None:
        items = [
            make_item(
                ticker="MSFT",
                company_name="Microsoft Corporation",
                title="Microsoft to invest $10 billion in Nvidia AI chips",
                summary="Microsoft announced a $10 billion investment.",
            ),
            make_item(
                ticker="NVDA",
                company_name="NVIDIA Corporation",
                title="Nvidia and Microsoft launch AI PC platform",
                summary="The two companies unveiled a new platform.",
            ),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 2)

    def test_one_sided_mention_does_not_merge(self) -> None:
        items = [
            make_item(
                ticker="AAPL",
                company_name="Apple Inc.",
                title="Apple launches new AI feature for developers",
                summary="Apple introduced the feature at its event.",
            ),
            make_item(
                ticker="GOOGL",
                company_name="Alphabet Inc.",
                title="Google says Apple's AI efforts lag behind",
                summary="Google compared its models with Apple's.",
            ),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 2)

    def test_separate_earnings_reports_not_merged(self) -> None:
        items = [
            make_item(
                ticker="MSFT",
                company_name="Microsoft Corporation",
                title="Microsoft and Alphabet just reported earnings, putting the AI boom to the test",
                summary="Microsoft reported quarterly revenue of $70 billion.",
                url="https://a.example/msft-earnings",
            ),
            make_item(
                ticker="GOOGL",
                company_name="Alphabet Inc.",
                title="Alphabet Q3 earnings: how key metrics compare to Wall Street estimates",
                summary="Alphabet reported quarterly revenue of $90 billion.",
                url="https://b.example/googl-earnings",
            ),
        ]
        clusters = cluster_items(items)
        self.assertEqual(len(clusters), 2)

    def test_chinese_amount_parsing(self) -> None:
        self.assertEqual(extract_amounts("向Meta AI投资40亿美元")[0], 4e9)
        self.assertEqual(extract_amounts("630亿美元SPV")[0], 6.3e10)

    def test_merge_keeps_subject_company(self) -> None:
        from nasdaq_intel.pipeline.normalize import (
            EventCluster,
            _cluster_weight,
            build_company_lexicon,
        )

        items = [
            make_item(
                ticker="GOOGL",
                company_name="Alphabet Inc.",
                title="Alphabet Q3 earnings beat estimates",
            ),
            make_item(
                ticker="MSFT",
                company_name="Microsoft Corporation",
                title="Alphabet Q3 earnings preview",
            ),
            make_item(
                ticker="MSFT",
                company_name="Microsoft Corporation",
                title="Alphabet Q3 earnings metrics vs estimates",
            ),
        ]
        lexicon = build_company_lexicon(items)
        googl = EventCluster(
            ticker="GOOGL",
            company_name="Alphabet Inc.",
            items=[items[0]],
            norm_title="alphabet q3 earnings beat estimates",
        )
        msft = EventCluster(
            ticker="MSFT",
            company_name="Microsoft Corporation",
            items=items[1:],
            norm_title="alphabet q3 earnings preview",
        )
        # MSFT 材料更多，但标题都没提到自己；归并后应保留讲 Alphabet 的那条
        self.assertLess(_cluster_weight(googl, lexicon), _cluster_weight(msft, lexicon))


if __name__ == "__main__":
    unittest.main()
