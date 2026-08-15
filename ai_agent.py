from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from ai_agent_io import CANDIDATE_FILE_NAME, CacheStore, RunPaths, load_candidates, write_jsonl, write_manifest
from ai_agent_models import Candidate, ConceptResult, DimensionResult, EvidenceBundle, ScoreResult
from concept_discovery import PROMPT_VERSION, ConceptDiscovery, verify_web_search_capability
from evidence_evaluator import EvidenceEvaluator, failed_score
from ranking import rank_results, top_rows
from tavily_evidence import QUERY_VERSION, TavilyEvidenceCollector
from tavily_hub import TavilyHubClient


CHINA_TZ = ZoneInfo("Asia/Shanghai")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索近期公开财经信息，并返回可核查的搜索结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用于公开网页检索的精确查询语句。",
                }
            },
            "required": ["query"],
        },
    },
}


class ConceptService(Protocol):
    def discover(self, candidate: Candidate) -> ConceptResult: ...


class EvidenceService(Protocol):
    def collect(
        self,
        candidate: Candidate,
        *,
        primary_concept: str | None,
        analysis_year: int,
    ) -> EvidenceBundle: ...


class EvaluationService(Protocol):
    def evaluate(
        self, candidate: Candidate, concepts: ConceptResult, bundle: EvidenceBundle
    ) -> ScoreResult: ...


class WebSearchClient(Protocol):
    def search(self, **kwargs: object) -> dict[str, object]: ...


class DeepSeekChatClient:
    def __init__(
        self, api_key: str, *, web_search_client: WebSearchClient | None = None
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("缺少 openai 依赖，请安装 requirements.txt") from error
        self.client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        self.web_search_client = web_search_client

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, str]] | None = None,
    ) -> str:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if tools is not None:
            return self._complete_with_web_search(messages)
        response = self.client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("DeepSeek 返回空响应")
        return content

    def _complete_with_web_search(self, messages: list[dict[str, object]]) -> str:
        if self.web_search_client is None:
            raise RuntimeError("DeepSeek 联网概念识别需要 Tavily 搜索客户端")

        first_response = self.client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            temperature=0.2,
            tools=[WEB_SEARCH_TOOL],
            tool_choice={"type": "function", "function": {"name": "web_search"}},
        )
        assistant_message = first_response.choices[0].message
        tool_calls = assistant_message.tool_calls or []
        if not tool_calls:
            raise RuntimeError("DeepSeek 联网搜索预检未返回 web_search 调用")

        serialized_calls: list[dict[str, object]] = []
        tool_messages: list[dict[str, str]] = []
        for tool_call in tool_calls:
            function = tool_call.function
            if function.name != "web_search":
                raise RuntimeError(f"DeepSeek 返回了不支持的工具: {function.name}")
            query = _web_search_query(function.arguments)
            result = self.web_search_client.search(
                query=query,
                topic="finance",
                search_depth="advanced",
                time_range="week",
                max_results=5,
            )
            if not isinstance(result, dict):
                raise RuntimeError("Tavily 联网搜索返回了非对象响应")
            serialized_calls.append(
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {"name": function.name, "arguments": function.arguments},
                }
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.content or "",
                "tool_calls": serialized_calls,
            }
        )
        messages.extend(tool_messages)
        return self._complete_final_json(messages)

    def _complete_final_json(self, messages: list[dict[str, object]]) -> str:
        request_messages = messages
        for attempt in range(2):
            final_response = self.client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=request_messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = final_response.choices[0].message.content
            if not content:
                raise RuntimeError("DeepSeek 在联网搜索后返回空响应")
            try:
                json.loads(content)
            except json.JSONDecodeError as error:
                if attempt == 1:
                    raise RuntimeError("DeepSeek 在联网搜索后返回无效 JSON") from error
                request_messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "上一条回答不是有效 JSON。请仅返回一个有效 JSON 对象，不要 Markdown 或解释。",
                    },
                ]
                continue
            return content
        raise RuntimeError("unreachable final JSON state")


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def run(
    project_root: Path | None = None,
    *,
    concept_factory: Callable[[Path, datetime], ConceptService] | None = None,
    evidence_factory: Callable[[Path, datetime], EvidenceService] | None = None,
    evaluator_factory: Callable[[Path], EvaluationService] | None = None,
    now_func: Callable[[], datetime] = china_now,
) -> RunPaths:
    root = project_root or Path(__file__).resolve().parent
    now = _as_china_time(now_func())
    candidates, ignored = load_candidates(root / CANDIDATE_FILE_NAME)
    cache = CacheStore(root, enabled=False)

    concepts_service = (
        concept_factory(root, now)
        if concept_factory is not None
        else _build_concept_service(root, now, cache)
    )
    evidence_service = (
        evidence_factory(root, now)
        if evidence_factory is not None
        else _build_evidence_service(root, now, cache)
    )
    evaluator = (
        evaluator_factory(root) if evaluator_factory is not None else _build_evaluator(root)
    )
    paths = RunPaths.create(root, now.date(), now.strftime("%H%M%S"))

    concepts: dict[str, ConceptResult] = {}
    bundles: dict[str, EvidenceBundle] = {}
    scores: list[ScoreResult] = []
    stage_failures: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            concept = concepts_service.discover(candidate)
        except Exception as error:
            stage_failures.append(_failure_record(candidate, "concept", error))
            concept = ConceptResult(candidate.stock_code, candidate.stock_name, (), error=str(error))
        try:
            bundle = evidence_service.collect(
                candidate,
                primary_concept=concept.primary_concept,
                analysis_year=now.year,
            )
        except Exception as error:
            stage_failures.append(_failure_record(candidate, "evidence", error))
            bundle = _failed_bundle(candidate, str(error))
        try:
            score = evaluator.evaluate(candidate, concept, bundle)
        except Exception as error:
            stage_failures.append(_failure_record(candidate, "evaluation", error))
            score = failed_score(candidate)
        concepts[candidate.stock_code] = concept
        bundles[candidate.stock_code] = bundle
        scores.append(score)

    rankings = rank_results(scores, bundles)
    _write_run_outputs(paths, candidates, concepts, bundles, rankings)
    write_manifest(
        paths,
        {
            "run_at": now.isoformat(),
            "input_file": CANDIDATE_FILE_NAME,
            "candidate_count": len(candidates),
            "candidate_shortfall": len(candidates) < 5,
            "ignored_rows": ignored,
            "stage_failures": stage_failures,
            "model_version": DEEPSEEK_MODEL,
            "concept_prompt_version": PROMPT_VERSION,
            "evidence_query_version": QUERY_VERSION,
            "cache_enabled": cache.enabled,
            "cache_hits": cache.hits,
            "output_files": [
                path.name
                for path in (
                    paths.normalized_candidates_csv,
                    paths.step1_jsonl,
                    paths.step1_summary_csv,
                    paths.step2_jsonl,
                    paths.step2_csv,
                    paths.all_rankings_csv,
                    paths.top10_csv,
                )
            ],
        },
    )
    return paths


def _write_run_outputs(
    paths: RunPaths,
    candidates: list[Candidate],
    concepts: dict[str, ConceptResult],
    bundles: dict[str, EvidenceBundle],
    rankings: pd.DataFrame,
) -> None:
    pd.DataFrame(
        [{"股票代码": candidate.stock_code, "股票名称": candidate.stock_name} for candidate in candidates]
    ).to_csv(paths.normalized_candidates_csv, index=False, encoding="utf-8-sig")

    concept_records = [_concept_record(concepts[candidate.stock_code]) for candidate in candidates]
    write_jsonl(paths.step1_jsonl, concept_records)
    pd.DataFrame(
        [
            {
                "股票代码": record["stock_code"],
                "股票名称": record["stock_name"],
                "主概念": record["primary_concept"],
                "概念列表": json.dumps(record["concepts"], ensure_ascii=False),
            }
            for record in concept_records
        ]
    ).to_csv(paths.step1_summary_csv, index=False, encoding="utf-8-sig")
    for record in concept_records:
        raw_response = record.pop("raw_response")
        if raw_response is not None:
            (paths.raw_step1_dir / f"{record['stock_code']}.json").write_text(
                raw_response, encoding="utf-8"
            )

    evidence_records = [_bundle_record(bundles[candidate.stock_code]) for candidate in candidates]
    write_jsonl(paths.step2_jsonl, evidence_records)
    pd.DataFrame(
        [
            {
                "股票代码": record["stock_code"],
                **{
                    f"{name}_状态": result["status"]
                    for name, result in record["dimensions"].items()
                },
                **{
                    f"{name}_证据": json.dumps(result["evidence"], ensure_ascii=False)
                    for name, result in record["dimensions"].items()
                },
            }
            for record in evidence_records
        ]
    ).to_csv(paths.step2_csv, index=False, encoding="utf-8-sig")
    for record in evidence_records:
        (paths.raw_step2_dir / f"{record['stock_code']}.json").write_text(
            json.dumps(record["raw_responses"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    rankings.insert(
        2,
        "主概念",
        rankings["股票代码"].map(
            lambda stock_code: concepts[str(stock_code)].primary_concept
        ),
    )
    rankings.to_csv(paths.all_rankings_csv, index=False, encoding="utf-8-sig")
    top_rows(rankings).to_csv(paths.top10_csv, index=False, encoding="utf-8-sig")


def _concept_record(result: ConceptResult) -> dict[str, object]:
    return {
        "stock_code": result.stock_code,
        "stock_name": result.stock_name,
        "primary_concept": result.primary_concept,
        "concepts": [
            {
                "concept_name": concept.concept_name,
                "concept_rank": concept.concept_rank,
                "is_core": concept.is_core,
            }
            for concept in result.concepts
        ],
        "raw_response": result.raw_response,
        "error": result.error,
    }


def _bundle_record(bundle: EvidenceBundle) -> dict[str, object]:
    dimensions: dict[str, object] = {}
    raw_responses: dict[str, object] = {}
    for name, result in bundle.dimensions.items():
        dimensions[name] = {
            "status": result.status,
            "error": result.error,
            "evidence": [
                {
                    "evidence_id": evidence.evidence_id,
                    "query": evidence.query,
                    "title": evidence.title,
                    "excerpt": evidence.excerpt,
                    "url": evidence.url,
                    "published_at": evidence.published_at,
                    "retrieved_at": evidence.retrieved_at,
                }
                for evidence in result.evidence
            ],
        }
        raw_responses[name] = result.raw_response
    return {
        "stock_code": bundle.stock_code,
        "dimensions": dimensions,
        "raw_responses": raw_responses,
    }


def _build_concept_service(
    project_root: Path, now: datetime, cache: CacheStore
) -> ConceptDiscovery:
    client = DeepSeekChatClient(
        _load_required_key(project_root, "DEEPSEEK_API_KEY"),
        web_search_client=_build_tavily_client(project_root),
    )
    verify_web_search_capability(client)
    return ConceptDiscovery(
        client,
        cache,
        analysis_date=now.date(),
        model_version=DEEPSEEK_MODEL,
    )


def _build_evidence_service(
    project_root: Path, now: datetime, cache: CacheStore
) -> TavilyEvidenceCollector:
    return TavilyEvidenceCollector(
        _build_tavily_client(project_root),
        cache=cache,
        analysis_date=now.date(),
        sleep_func=time.sleep,
        now_func=china_now,
    )


def _build_tavily_client(project_root: Path) -> TavilyHubClient:
    api_keys = tuple(
        key
        for index in range(1, 7)
        if (key := _load_optional_key(project_root, f"TAVILY_HUB_API_KEY{index}"))
    )
    if not api_keys:
        raise RuntimeError(
            ".env 中缺少 Tavily Hub Key；请配置 TAVILY_HUB_API_KEY1 至 TAVILY_HUB_API_KEY6 中至少一个。"
        )
    return TavilyHubClient(api_keys)


def _build_evaluator(project_root: Path) -> EvidenceEvaluator:
    return EvidenceEvaluator(DeepSeekChatClient(_load_required_key(project_root, "DEEPSEEK_API_KEY")))


def _load_required_key(project_root: Path, name: str) -> str:
    value = _load_optional_key(project_root, name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def _load_optional_key(project_root: Path, name: str) -> str | None:
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError("缺少 python-dotenv 依赖，请安装 requirements.txt") from error
    load_dotenv(project_root / ".env")
    value = os.environ.get(name, "").strip()
    return value or None


def _as_china_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ)


def _failed_bundle(candidate: Candidate, error: str) -> EvidenceBundle:
    return EvidenceBundle(
        candidate.stock_code,
        {
            name: DimensionResult(status="failed", error=error)
            for name in (
                "board_strength",
                "stock_funds",
                "stock_news",
                "market_analysis",
                "stock_risk",
            )
        },
    )


def _failure_record(candidate: Candidate, stage: str, error: Exception) -> dict[str, str]:
    return {"stock_code": candidate.stock_code, "stage": stage, "error": str(error)}


def _web_search_query(arguments: str) -> str:
    try:
        payload = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise RuntimeError("DeepSeek web_search 参数不是有效 JSON") from error
    query = payload.get("query") if isinstance(payload, dict) else None
    if not isinstance(query, str) or not query.strip():
        raise RuntimeError("DeepSeek web_search 缺少有效 query")
    return query.strip()[:300]


def main() -> int:
    if len(sys.argv) != 1:
        print("本工具不接受命令行参数。")
        return 2
    paths = run()
    print(f"分析完成: {paths.top10_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
