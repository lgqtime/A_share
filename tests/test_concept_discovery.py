from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from ai_agent_io import CacheStore
from ai_agent_models import Candidate
from concept_discovery import ConceptDiscovery, ConceptResponseError, verify_web_search_capability


class FakeConceptClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def response_for(candidate: Candidate, concepts: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "stock_code": candidate.stock_code,
            "stock_name": candidate.stock_name,
            "concepts": concepts,
        },
        ensure_ascii=False,
    )


def test_discover_returns_ranked_concepts_and_requests_web_search(tmp_path: Path) -> None:
    candidate = Candidate("000066", "中国长城")
    client = FakeConceptClient(
        [
            response_for(
                candidate,
                [
                    {"concept_name": "信创", "concept_rank": 1, "is_core": True},
                    {"concept_name": "国产芯片", "concept_rank": 2, "is_core": False},
                ],
            )
        ]
    )
    service = ConceptDiscovery(
        client,
        CacheStore(tmp_path),
        analysis_date=date(2026, 8, 14),
        model_version="deepseek-search",
    )

    result = service.discover(candidate)

    assert result.primary_concept == "信创"
    assert result.concepts[0].is_core is True
    assert client.calls[0]["tools"] == [{"type": "web_search"}]
    assert "排除行业" in str(client.calls[0]["system_prompt"])


def test_discover_uses_same_day_successful_cache(tmp_path: Path) -> None:
    candidate = Candidate("000066", "中国长城")
    client = FakeConceptClient([response_for(candidate, [])])
    service = ConceptDiscovery(
        client,
        CacheStore(tmp_path),
        analysis_date=date(2026, 8, 14),
        model_version="deepseek-search",
    )

    first = service.discover(candidate)
    second = service.discover(candidate)

    assert first == second
    assert first.primary_concept is None
    assert len(client.calls) == 1


def test_discover_rejects_marker_character_in_concept_name(tmp_path: Path) -> None:
    candidate = Candidate("000066", "中国长城")
    client = FakeConceptClient(
        [response_for(candidate, [{"concept_name": "信创*", "concept_rank": 1, "is_core": True}])]
    )
    service = ConceptDiscovery(
        client,
        CacheStore(tmp_path),
        analysis_date=date(2026, 8, 14),
        model_version="deepseek-search",
    )

    with pytest.raises(ConceptResponseError, match="概念名称不能包含"):
        service.discover(candidate)


def test_discover_uses_input_name_when_model_returns_a_formal_company_name(tmp_path: Path) -> None:
    candidate = Candidate("000066", "中国长城")
    client = FakeConceptClient(
        [
            json.dumps(
                {
                    "stock_code": candidate.stock_code,
                    "stock_name": "中国长城科技集团股份有限公司",
                    "concepts": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    service = ConceptDiscovery(
        client,
        CacheStore(tmp_path),
        analysis_date=date(2026, 8, 14),
        model_version="deepseek-search",
    )

    result = service.discover(candidate)

    assert result.stock_name == "中国长城"


def test_web_search_capability_preflight_uses_required_tool() -> None:
    client = FakeConceptClient(["capability confirmed"])

    verify_web_search_capability(client)

    assert client.calls[0]["tools"] == [{"type": "web_search"}]
    assert "json" in str(client.calls[0]["system_prompt"]).lower()
