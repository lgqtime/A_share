from __future__ import annotations

import json

from ai_agent_models import Candidate, ConceptResult, DimensionResult, Evidence, EvidenceBundle
from evidence_evaluator import EvidenceEvaluator


class FakeEvaluatorClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def sample_candidate() -> Candidate:
    return Candidate("000066", "中国长城")


def sample_bundle() -> EvidenceBundle:
    candidate = sample_candidate()
    evidence = Evidence(
        evidence_id="EV-000066-stock_news-01",
        stock_code=candidate.stock_code,
        dimension="stock_news",
        query="query",
        title="标题",
        excerpt="证据摘要",
        url="https://example.test/evidence",
        published_at=None,
        retrieved_at="2026-08-14T15:30:00+08:00",
    )
    return EvidenceBundle(
        candidate.stock_code,
        {
            "board_strength": DimensionResult(status="empty"),
            "stock_funds": DimensionResult(status="empty"),
            "stock_news": DimensionResult(status="success", evidence=(evidence,)),
            "market_analysis": DimensionResult(status="empty"),
            "stock_risk": DimensionResult(status="empty"),
        },
    )


def valid_response(
    *, evidence_ids: list[str] | None = None, stock_name: str = "中国长城"
) -> str:
    evidence_ids = evidence_ids or ["EV-000066-stock_news-01"]
    return json.dumps(
        {
            "stock_code": "000066",
            "stock_name": stock_name,
            "individual_score": 8,
            "individual_reason": "个股证据充分",
            "individual_evidence_ids": evidence_ids,
            "sector_score": 7,
            "sector_reason": "板块中性",
            "sector_evidence_ids": [],
            "final_verdict": "看好",
            "risk_level": "无",
            "key_risk": "无重大风险证据",
            "risk_evidence_ids": [],
        },
        ensure_ascii=False,
    )


def test_evaluator_repairs_invalid_evidence_reference_once() -> None:
    client = FakeEvaluatorClient([valid_response(evidence_ids=["EV-unknown"]), valid_response()])
    evaluator = EvidenceEvaluator(client)

    result = evaluator.evaluate(sample_candidate(), ConceptResult("000066", "中国长城", ()), sample_bundle())

    assert result.analysis_status == "已评分"
    assert result.individual_evidence_ids == ("EV-000066-stock_news-01",)
    assert len(client.calls) == 2
    assert "修复" in str(client.calls[1]["user_prompt"])


def test_evaluator_marks_candidate_failed_after_second_invalid_response() -> None:
    client = FakeEvaluatorClient(["not json", "still not json"])
    evaluator = EvidenceEvaluator(client)

    result = evaluator.evaluate(sample_candidate(), ConceptResult("000066", "中国长城", ()), sample_bundle())

    assert result.analysis_status == "分析失败"
    assert result.individual_score == 0
    assert result.sector_score == 0


def test_evaluator_uses_input_name_when_model_returns_a_formal_company_name() -> None:
    client = FakeEvaluatorClient(
        [
            valid_response(stock_name="中国长城科技集团股份有限公司"),
            valid_response(stock_name="中国长城科技集团股份有限公司"),
        ]
    )
    evaluator = EvidenceEvaluator(client)

    result = evaluator.evaluate(
        sample_candidate(), ConceptResult("000066", "中国长城", ()), sample_bundle()
    )

    assert result.analysis_status == "已评分"
    assert result.stock_name == "中国长城"
