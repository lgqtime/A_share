from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from ai_agent import CHINA_TZ, DeepSeekChatClient, _build_tavily_client, run
from ai_agent_models import Candidate, ConceptResult, DimensionResult, EvidenceBundle, ScoreResult
from tavily_hub import TavilyHubClient


class FakeConceptDiscovery:
    def discover(self, candidate: Candidate) -> ConceptResult:
        return ConceptResult(candidate.stock_code, candidate.stock_name, ())


class ConceptDiscoveryWithFailure(FakeConceptDiscovery):
    def discover(self, candidate: Candidate) -> ConceptResult:
        if candidate.stock_code == "000002":
            raise RuntimeError("concept provider unavailable")
        return super().discover(candidate)


class FakeEvidenceCollector:
    def collect(
        self,
        candidate: Candidate,
        *,
        primary_concept: str | None,
        analysis_year: int,
    ) -> EvidenceBundle:
        return EvidenceBundle(
            candidate.stock_code,
            {
                "board_strength": DimensionResult.skipped(),
                "stock_funds": DimensionResult(status="empty"),
                "stock_news": DimensionResult(status="empty"),
                "market_analysis": DimensionResult(status="empty"),
                "stock_risk": DimensionResult(status="empty"),
            },
        )


class FakeEvaluator:
    def evaluate(
        self,
        candidate: Candidate,
        concepts: ConceptResult,
        bundle: EvidenceBundle,
    ) -> ScoreResult:
        return ScoreResult(
            stock_code=candidate.stock_code,
            stock_name=candidate.stock_name,
            individual_score=8,
            individual_reason="个股理由",
            individual_evidence_ids=(),
            sector_score=7,
            sector_reason="板块理由",
            sector_evidence_ids=(),
            final_verdict="看好",
            risk_level="无",
            key_risk="无",
            risk_evidence_ids=(),
            analysis_status="已评分",
        )


def write_candidates(path: Path, count: int) -> None:
    pd.DataFrame(
        {
            "股票代码": [f"{number:06d}" for number in range(1, count + 1)],
            "股票名称": [f"股票{number}" for number in range(1, count + 1)],
        }
    ).to_csv(path, index=False, encoding="utf-8-sig")


def test_build_tavily_client_reads_all_configured_hub_keys(tmp_path: Path, monkeypatch) -> None:
    for index in range(1, 7):
        monkeypatch.delenv(f"TAVILY_HUB_API_KEY{index}", raising=False)
    (tmp_path / ".env").write_text(
        "TAVILY_HUB_API_KEY1=hub-one\n"
        "TAVILY_HUB_API_KEY3=hub-three\n"
        "TAVILY_HUB_API_KEY6=hub-six\n",
        encoding="utf-8",
    )

    client = _build_tavily_client(tmp_path)

    assert isinstance(client, TavilyHubClient)
    assert client.api_keys == ("hub-one", "hub-three", "hub-six")


def test_run_processes_all_candidates_and_writes_top_ten(tmp_path: Path) -> None:
    write_candidates(tmp_path / "前 50 名（含所属行业）.csv", count=12)

    output = run(
        project_root=tmp_path,
        concept_factory=lambda root, analysis_date: FakeConceptDiscovery(),
        evidence_factory=lambda root, analysis_date: FakeEvidenceCollector(),
        evaluator_factory=lambda root: FakeEvaluator(),
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=CHINA_TZ),
    )

    top10 = pd.read_csv(output.top10_csv, dtype={"股票代码": "string"})
    rankings = pd.read_csv(output.all_rankings_csv, dtype={"股票代码": "string"})

    assert len(rankings) == 12
    assert len(top10) == 10
    assert output.top10_csv.name == "top10_recommendations.csv"
    assert not (output.root / "top5_recommendations.csv").exists()
    assert output.manifest.exists()
    manifest = json.loads(output.manifest.read_text(encoding="utf-8"))
    assert "top10_recommendations.csv" in manifest["output_files"]
    assert output.normalized_candidates_csv.exists()
    assert output.step1_jsonl.exists()
    assert output.step2_jsonl.exists()
    assert "stock_news_证据" in pd.read_csv(output.step2_csv).columns


def test_run_marks_candidate_shortfall_without_failing(tmp_path: Path) -> None:
    write_candidates(tmp_path / "前 50 名（含所属行业）.csv", count=2)

    output = run(
        project_root=tmp_path,
        concept_factory=lambda root, analysis_date: FakeConceptDiscovery(),
        evidence_factory=lambda root, analysis_date: FakeEvidenceCollector(),
        evaluator_factory=lambda root: FakeEvaluator(),
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=CHINA_TZ),
    )

    top10 = pd.read_csv(output.top10_csv, dtype={"股票代码": "string"})

    assert len(top10) == 2


def test_run_keeps_other_candidates_when_one_concept_call_fails(tmp_path: Path) -> None:
    write_candidates(tmp_path / "前 50 名（含所属行业）.csv", count=3)

    output = run(
        project_root=tmp_path,
        concept_factory=lambda root, analysis_date: ConceptDiscoveryWithFailure(),
        evidence_factory=lambda root, analysis_date: FakeEvidenceCollector(),
        evaluator_factory=lambda root: FakeEvaluator(),
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=CHINA_TZ),
    )

    rankings = pd.read_csv(output.all_rankings_csv, dtype={"股票代码": "string"})
    manifest = json.loads(output.manifest.read_text(encoding="utf-8"))

    assert len(rankings) == 3
    assert manifest["stage_failures"] == [
        {"stock_code": "000002", "stage": "concept", "error": "concept provider unavailable"}
    ]


def test_run_writes_concepts_and_versions_to_audit_outputs(tmp_path: Path) -> None:
    write_candidates(tmp_path / "前 50 名（含所属行业）.csv", count=1)

    output = run(
        project_root=tmp_path,
        concept_factory=lambda root, analysis_date: FakeConceptDiscovery(),
        evidence_factory=lambda root, analysis_date: FakeEvidenceCollector(),
        evaluator_factory=lambda root: FakeEvaluator(),
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=CHINA_TZ),
    )

    rankings = pd.read_csv(output.all_rankings_csv, dtype={"股票代码": "string"})
    manifest = json.loads(output.manifest.read_text(encoding="utf-8"))

    assert "主概念" in rankings.columns
    assert manifest["concept_prompt_version"] == "concept-v2"
    assert manifest["evidence_query_version"] == "search-v1"
    assert manifest["cache_enabled"] is False
    assert manifest["cache_hits"] == 0


def test_deepseek_client_uses_function_calling_for_web_search() -> None:
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="web_search",
            arguments='{"query":"中国长城 最新市场概念"}',
        ),
    )
    completions = FakeChatCompletions(
        [
            _completion(content=None, tool_calls=[tool_call]),
            _completion(
                content=(
                    '{"stock_code":"000066","stock_name":"中国长城","concepts":[]}'
                ),
                tool_calls=[],
            ),
        ]
    )
    client = object.__new__(DeepSeekChatClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    search = FakeWebSearchClient()
    client.web_search_client = search

    response = client.complete(
        system_prompt="system",
        user_prompt="user",
        tools=[{"type": "web_search"}],
    )

    assert response == '{"stock_code":"000066","stock_name":"中国长城","concepts":[]}'
    assert search.queries == ["中国长城 最新市场概念"]
    assert completions.requests[0]["tools"][0]["type"] == "function"
    assert completions.requests[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "web_search"},
    }
    assert completions.requests[1]["messages"][-1]["role"] == "tool"
    assert completions.requests[1]["response_format"] == {"type": "json_object"}


def test_deepseek_client_repairs_invalid_final_json_without_repeating_web_search() -> None:
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="web_search",
            arguments='{"query":"中国长城 最新市场概念"}',
        ),
    )
    valid_response = '{"stock_code":"000066","stock_name":"中国长城","concepts":[]}'
    completions = FakeChatCompletions(
        [
            _completion(content=None, tool_calls=[tool_call]),
            _completion(content="not valid json", tool_calls=[]),
            _completion(content=valid_response, tool_calls=[]),
        ]
    )
    client = object.__new__(DeepSeekChatClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    search = FakeWebSearchClient()
    client.web_search_client = search

    response = client.complete(
        system_prompt="system",
        user_prompt="user",
        tools=[{"type": "web_search"}],
    )

    assert response == valid_response
    assert search.queries == ["中国长城 最新市场概念"]
    assert len(completions.requests) == 3
    assert completions.requests[2]["response_format"] == {"type": "json_object"}
    assert completions.requests[2]["messages"][-2] == {
        "role": "assistant",
        "content": "not valid json",
    }
    assert completions.requests[2]["messages"][-1]["role"] == "user"


class FakeChatCompletions:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> SimpleNamespace:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeWebSearchClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, **kwargs: object) -> dict[str, object]:
        self.queries.append(str(kwargs["query"]))
        return {"results": [{"title": "财经报道", "content": "相关证据"}]}


def _completion(content: str | None, tool_calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )
