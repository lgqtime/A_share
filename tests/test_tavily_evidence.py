from __future__ import annotations

from datetime import datetime, timezone
from datetime import date
from pathlib import Path

from ai_agent_io import CacheStore
from ai_agent_models import Candidate
from tavily_evidence import TavilyEvidenceCollector


class FakeTavilyClient:
    def __init__(self, *, fail_risk: bool = False) -> None:
        self.fail_risk = fail_risk
        self.queries: list[str] = []

    def search(self, **kwargs: object) -> dict[str, object]:
        query = str(kwargs["query"])
        self.queries.append(query)
        if self.fail_risk and "利空" in query:
            raise TimeoutError("temporary outage")
        return {
            "results": [
                {
                    "title": f"{query} 标题",
                    "content": "可用于评分的近期财经证据",
                    "url": "https://example.test/evidence",
                    "published_date": "2026-08-14T08:00:00+08:00",
                }
            ]
        }


class StockNewsFailsThenSucceedsClient(FakeTavilyClient):
    def __init__(self) -> None:
        super().__init__()
        self.stock_news_attempts = 0

    def search(self, **kwargs: object) -> dict[str, object]:
        query = str(kwargs["query"])
        if "最新公告" in query:
            self.stock_news_attempts += 1
            if self.stock_news_attempts <= 2:
                self.queries.append(query)
                raise TimeoutError("temporary news outage")
        return super().search(**kwargs)


def test_collect_skips_board_query_without_primary_concept() -> None:
    client = FakeTavilyClient()
    collector = TavilyEvidenceCollector(
        client,
        max_workers=5,
        max_retries=0,
        sleep_func=lambda _: None,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )

    bundle = collector.collect(
        Candidate("000066", "中国长城"), primary_concept=None, analysis_year=2026
    )

    assert bundle.statuses["board_strength"] == "skipped"
    assert len(client.queries) == 4
    assert all("板块 最新政策" not in query for query in client.queries)
    assert bundle.dimensions["stock_news"].evidence[0].evidence_id == "EV-000066-stock_news-01"


def test_collect_keeps_failed_dimension_without_dropping_other_evidence() -> None:
    client = FakeTavilyClient(fail_risk=True)
    collector = TavilyEvidenceCollector(
        client,
        max_workers=5,
        max_retries=0,
        sleep_func=lambda _: None,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )

    bundle = collector.collect(
        Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026
    )

    assert bundle.statuses["stock_risk"] == "failed"
    assert bundle.statuses["stock_news"] == "success"
    assert bundle.dimensions["stock_risk"].evidence == ()


def test_collect_retries_failed_stock_news_every_minute_until_success() -> None:
    client = StockNewsFailsThenSucceedsClient()
    sleeps: list[float] = []
    collector = TavilyEvidenceCollector(
        client,
        max_workers=1,
        max_retries=0,
        sleep_func=sleeps.append,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )

    bundle = collector.collect(
        Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026
    )

    assert bundle.statuses["stock_news"] == "success"
    assert client.stock_news_attempts == 3
    assert sleeps == [60, 60]
    assert len(client.queries) == 7


def test_collect_reuses_same_day_successful_evidence_cache(tmp_path: Path) -> None:
    client = FakeTavilyClient()
    collector = TavilyEvidenceCollector(
        client,
        cache=CacheStore(tmp_path),
        analysis_date=date(2026, 8, 14),
        max_workers=5,
        max_retries=0,
        sleep_func=lambda _: None,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )

    first = collector.collect(
        Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026
    )
    second = collector.collect(
        Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026
    )

    assert first == second
    assert len(client.queries) == 5


def test_collect_does_not_reuse_cache_after_query_version_changes(tmp_path: Path) -> None:
    client = FakeTavilyClient()
    cache = CacheStore(tmp_path)
    first = TavilyEvidenceCollector(
        client,
        cache=cache,
        analysis_date=date(2026, 8, 14),
        query_version="search-v1",
        max_workers=5,
        max_retries=0,
        sleep_func=lambda _: None,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )
    second = TavilyEvidenceCollector(
        client,
        cache=cache,
        analysis_date=date(2026, 8, 14),
        query_version="search-v2",
        max_workers=5,
        max_retries=0,
        sleep_func=lambda _: None,
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc),
    )

    first.collect(Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026)
    second.collect(Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026)

    assert len(client.queries) == 10
