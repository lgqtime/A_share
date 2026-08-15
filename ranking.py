from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from ai_agent_models import EvidenceBundle, ScoreResult


RISK_ORDER = {"无": 0, "一般": 1, "重大": 2}


def rank_results(
    scores: Sequence[ScoreResult], bundles: Mapping[str, EvidenceBundle]
) -> pd.DataFrame:
    rows = [_rank_row(score, bundles.get(score.stock_code)) for score in scores]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = frame.sort_values(
        ["最终排序分", "证据门槛通过", "_风险排序", "板块得分", "个股得分", "股票代码"],
        ascending=[False, False, True, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    frame["排名"] = range(1, len(frame) + 1)
    return frame.drop(columns=["_风险排序"])


def top_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.head(10).copy()


def _rank_row(score: ScoreResult, bundle: EvidenceBundle | None) -> dict[str, object]:
    evidence_gate_passed = _evidence_gate_passed(bundle)
    base_score = round(score.sector_score * 0.6 + score.individual_score * 0.4, 2)
    final_score = _final_score(score, bundle, evidence_gate_passed, base_score)
    return {
        "股票代码": score.stock_code,
        "股票名称": score.stock_name,
        "个股得分": score.individual_score,
        "个股理由": score.individual_reason,
        "个股证据": ",".join(score.individual_evidence_ids),
        "板块得分": score.sector_score,
        "板块理由": score.sector_reason,
        "板块证据": ",".join(score.sector_evidence_ids),
        "基础分": base_score,
        "最终排序分": final_score,
        "证据门槛通过": evidence_gate_passed,
        "模型结论": score.final_verdict,
        "风险等级": score.risk_level,
        "关键风险": score.key_risk,
        "风险证据": ",".join(score.risk_evidence_ids),
        "推荐等级": _recommendation_level(score, evidence_gate_passed),
        "分析状态": score.analysis_status,
        "_风险排序": RISK_ORDER[score.risk_level],
    }


def _evidence_gate_passed(bundle: EvidenceBundle | None) -> bool:
    if bundle is None:
        return False
    statuses = bundle.statuses
    completed = sum(status in {"success", "empty"} for status in statuses.values())
    return completed >= 3 and statuses.get("stock_risk") in {"success", "empty"}


def _final_score(
    score: ScoreResult,
    bundle: EvidenceBundle | None,
    evidence_gate_passed: bool,
    base_score: float,
) -> float:
    if score.analysis_status == "分析失败":
        return 0.0
    risk_ids = bundle.evidence_ids if bundle is not None else set()
    if score.risk_level == "重大" and set(score.risk_evidence_ids).issubset(risk_ids) and score.risk_evidence_ids:
        return min(base_score, 4.0)
    if not evidence_gate_passed:
        return min(base_score, 6.0)
    return base_score


def _recommendation_level(score: ScoreResult, evidence_gate_passed: bool) -> str:
    if (
        score.analysis_status == "分析失败"
        or score.risk_level == "重大"
        or score.final_verdict == "回避"
    ):
        return "不建议"
    if not evidence_gate_passed or score.risk_level == "一般" or score.final_verdict == "中性":
        return "观察"
    return "建议关注"
