from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from ai_agent_models import Candidate, DimensionResult, Evidence, EvidenceBundle
from ai_agent_io import CacheStore


CHINA_TZ = ZoneInfo("Asia/Shanghai")
DIMENSIONS = (
    "board_strength",
    "stock_funds",
    "stock_news",
    "market_analysis",
    "stock_risk",
)
INCLUDE_DOMAINS = (
    "cninfo.com.cn",
    "eastmoney.com",
    "stcn.com",
    "cs.com.cn",
    "finance.sina.com.cn",
    "cls.cn",
    "jrj.com.cn",
)
EXCLUDE_DOMAINS = ("guba.eastmoney.com",)
QUERY_VERSION = "search-v1"


class TavilyClient(Protocol):
    def search(self, **kwargs: object) -> dict[str, object]: ...


class TavilyEvidenceCollector:
    def __init__(
        self,
        client: TavilyClient,
        *,
        cache: CacheStore | None = None,
        analysis_date: date | None = None,
        query_version: str = QUERY_VERSION,
        max_workers: int = 5,
        max_retries: int = 2,
        sleep_func: Callable[[float], None],
        now_func: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.cache = cache
        self.analysis_date = analysis_date
        self.query_version = query_version
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.sleep = sleep_func
        self.now = now_func

    def collect(
        self,
        candidate: Candidate,
        *,
        primary_concept: str | None,
        analysis_year: int,
    ) -> EvidenceBundle:
        dimensions = [
            dimension
            for dimension in DIMENSIONS
            if dimension != "board_strength" or primary_concept is not None
        ]
        results: dict[str, DimensionResult] = {}
        pending_dimensions: list[str] = []
        cache_concept = primary_concept or "__no_primary_concept__"
        for dimension in dimensions:
            cached = self._read_cached_dimension(candidate, cache_concept, dimension)
            if cached is None:
                pending_dimensions.append(dimension)
            else:
                results[dimension] = cached

        with ThreadPoolExecutor(
            max_workers=max(1, min(self.max_workers, len(pending_dimensions)))
        ) as executor:
            futures = {
                dimension: executor.submit(
                    self._search_dimension,
                    dimension,
                    candidate,
                    primary_concept,
                    analysis_year,
                )
                for dimension in pending_dimensions
            }
            for dimension, future in futures.items():
                result = future.result()
                results[dimension] = result
                self._write_cached_dimension(candidate, cache_concept, dimension, result)
        self._retry_failed_stock_news(
            results,
            candidate,
            primary_concept,
            analysis_year,
            cache_concept,
        )
        if primary_concept is None:
            results["board_strength"] = DimensionResult.skipped()
        return EvidenceBundle(candidate.stock_code, results)

    def _retry_failed_stock_news(
        self,
        results: dict[str, DimensionResult],
        candidate: Candidate,
        primary_concept: str | None,
        analysis_year: int,
        cache_concept: str,
    ) -> None:
        while results["stock_news"].status == "failed":
            self.sleep(60)
            result = self._search_dimension(
                "stock_news", candidate, primary_concept, analysis_year
            )
            results["stock_news"] = result
            self._write_cached_dimension(candidate, cache_concept, "stock_news", result)

    def _search_dimension(
        self,
        dimension: str,
        candidate: Candidate,
        primary_concept: str | None,
        analysis_year: int,
    ) -> DimensionResult:
        query = build_query(dimension, candidate, primary_concept, analysis_year)
        try:
            response = self._search_with_retry(query)
        except Exception as error:
            return DimensionResult(status="failed", error=str(error))

        raw_results = response.get("results", [])
        if not isinstance(raw_results, list) or not raw_results:
            return DimensionResult(status="empty", raw_response=response)
        retrieved_at = self.now().astimezone(CHINA_TZ).isoformat()
        evidence: list[Evidence] = []
        for index, item in enumerate(raw_results[:5], start=1):
            if not isinstance(item, dict):
                continue
            evidence.append(
                Evidence(
                    evidence_id=f"EV-{candidate.stock_code}-{dimension}-{index:02d}",
                    stock_code=candidate.stock_code,
                    dimension=dimension,
                    query=query,
                    title=_text(item.get("title")),
                    excerpt=_text(item.get("content"))[:500],
                    url=_text(item.get("url")),
                    published_at=_optional_text(item.get("published_date")),
                    retrieved_at=retrieved_at,
                )
            )
        return DimensionResult(
            status="success" if evidence else "empty",
            evidence=tuple(evidence),
            raw_response=response,
        )

    def _read_cached_dimension(
        self, candidate: Candidate, primary_concept: str, dimension: str
    ) -> DimensionResult | None:
        if self.cache is None or self.analysis_date is None:
            return None
        return self.cache.read_evidence_dimension(
            self.analysis_date,
            candidate.stock_code,
            primary_concept,
            dimension,
            self.query_version,
        )

    def _write_cached_dimension(
        self,
        candidate: Candidate,
        primary_concept: str,
        dimension: str,
        result: DimensionResult,
    ) -> None:
        if self.cache is None or self.analysis_date is None:
            return
        self.cache.write_evidence_dimension(
            self.analysis_date,
            candidate.stock_code,
            primary_concept,
            dimension,
            self.query_version,
            result,
        )

    def _search_with_retry(self, query: str) -> dict[str, object]:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.search(
                    query=query,
                    topic="finance",
                    search_depth="advanced",
                    time_range="week",
                    include_domains=list(INCLUDE_DOMAINS),
                    exclude_domains=list(EXCLUDE_DOMAINS),
                    max_results=5,
                )
                if not isinstance(response, dict):
                    raise TypeError("Tavily 返回了非对象响应")
                return response
            except Exception as error:
                if attempt == self.max_retries or not _is_transient_error(error):
                    raise
                self.sleep(2.0 * (2**attempt))
        raise RuntimeError("unreachable retry state")


def build_query(
    dimension: str,
    candidate: Candidate,
    primary_concept: str | None,
    analysis_year: int,
) -> str:
    if dimension == "board_strength":
        if primary_concept is None:
            raise ValueError("板块检索需要主概念")
        return f"{primary_concept} 板块 最新政策 主力资金 龙头股 {analysis_year}"
    if dimension == "stock_funds":
        return f"{candidate.stock_name} {candidate.stock_code} 主力资金 北向资金 净流入"
    if dimension == "stock_news":
        return f"{candidate.stock_name} {candidate.stock_code} 最新公告 新闻 动态"
    if dimension == "market_analysis":
        return f"{candidate.stock_name} {candidate.stock_code} 券商 研报 评级 目标价"
    if dimension == "stock_risk":
        return f"{candidate.stock_name} {candidate.stock_code} 利空 减持 监管 警示"
    raise ValueError(f"未知检索维度: {dimension}")


def _is_transient_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    return status == 429 or isinstance(status, int) and 500 <= status <= 599


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _optional_text(value: object) -> str | None:
    text = _text(value)
    return text or None
