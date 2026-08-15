from __future__ import annotations

from ai_agent_models import Candidate


def test_candidate_preserves_normalized_stock_identity() -> None:
    candidate = Candidate(stock_code="000066", stock_name="中国长城")

    assert candidate.stock_code == "000066"
    assert candidate.stock_name == "中国长城"
