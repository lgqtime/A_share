from __future__ import annotations

from ai_agent_models import Candidate, DimensionResult, Evidence, EvidenceBundle, ScoreResult
from ranking import rank_results, top_rows


def bundle_for(code: str, *, risk_status: str = "empty", with_risk_evidence: bool = False) -> EvidenceBundle:
    risk_evidence = ()
    if with_risk_evidence:
        risk_evidence = (
            Evidence(
                evidence_id=f"EV-{code}-stock_risk-01",
                stock_code=code,
                dimension="stock_risk",
                query="风险查询",
                title="风险标题",
                excerpt="风险证据",
                url="https://example.test/risk",
                published_at=None,
                retrieved_at="2026-08-14T15:30:00+08:00",
            ),
        )
    return EvidenceBundle(
        code,
        {
            "board_strength": DimensionResult(status="empty"),
            "stock_funds": DimensionResult(status="empty"),
            "stock_news": DimensionResult(status="empty"),
            "market_analysis": DimensionResult(status="empty"),
            "stock_risk": DimensionResult(status=risk_status, evidence=risk_evidence),
        },
    )


def score_for(code: str, *, risk_level: str = "无", risk_evidence_ids: tuple[str, ...] = ()) -> ScoreResult:
    return ScoreResult(
        stock_code=code,
        stock_name=f"股票{code}",
        individual_score=9,
        individual_reason="个股理由",
        individual_evidence_ids=(),
        sector_score=9,
        sector_reason="板块理由",
        sector_evidence_ids=(),
        final_verdict="看好",
        risk_level=risk_level,
        key_risk="风险",
        risk_evidence_ids=risk_evidence_ids,
        analysis_status="已评分",
    )


def test_rank_caps_material_risk_and_still_returns_ten_rows() -> None:
    scores = [
        score_for("000001", risk_level="重大", risk_evidence_ids=("EV-000001-stock_risk-01",)),
        *[score_for(f"{number:06d}") for number in range(2, 12)],
    ]
    bundles = {
        "000001": bundle_for("000001", with_risk_evidence=True),
        **{f"{number:06d}": bundle_for(f"{number:06d}") for number in range(2, 12)},
    }

    frame = rank_results(scores, bundles)
    risk_row = frame.loc[frame["股票代码"] == "000001"].iloc[0]

    assert risk_row["最终排序分"] <= 4
    assert risk_row["推荐等级"] == "不建议"
    assert len(top_rows(frame)) == 10


def test_rank_caps_insufficient_evidence_at_six() -> None:
    score = score_for("000001")
    frame = rank_results([score], {"000001": bundle_for("000001", risk_status="failed")})

    assert frame.loc[0, "证据门槛通过"] == False
    assert frame.loc[0, "最终排序分"] == 6
    assert frame.loc[0, "推荐等级"] == "观察"
