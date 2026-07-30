"""风险过滤后候选股的行业共识选股规则。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


DEFAULT_INDUSTRY_CONSENSUS_TOP_N = 50
MISSING_INDUSTRY_LABELS = frozenset({"nan", "none", "nat", "<na>", "未分类"})


@dataclass(frozen=True)
class IndustryConsensusDetails:
    """一次行业共识选择的候选和统计信息。"""

    candidate: pd.Series | None
    top_candidate_count: int
    valid_industry_candidate_count: int
    leading_industries: tuple[str, ...]
    leading_industry_count: int


def normalize_industry(value: object) -> str | None:
    """规范化有效行业；缺失、空白和“未分类”占位值不参与行业共识。"""

    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        # 行业字段应为标量；异常值按其文本表示处理，避免中断一次回测。
        pass
    text = str(value).strip()
    if not text or text.casefold() in MISSING_INDUSTRY_LABELS:
        return None
    return text


def describe_industry_consensus(
    ranked: pd.DataFrame,
    *,
    top_n: int = DEFAULT_INDUSTRY_CONSENSUS_TOP_N,
    industry_column: str = "所属行业",
) -> IndustryConsensusDetails:
    """返回已按既有规则排序的候选的行业共识详情。

    调用方负责先完成指标筛选、风险剔除和既有排序。函数仅查看前 ``top_n``
    个候选：先保留出现次数最多的行业，再从这些行业中按原有排名取首只股票。
    因原排名以得分为第一排序键，首只股票就是最高得分；得分相同时也自然沿用
    原有的量比、收盘位置、成交额和股票代码的排序规则。缺少行业的候选不会
    参与统计；若前 ``top_n`` 名都没有有效行业，返回的详情中 ``candidate`` 为
    ``None``。
    """

    try:
        limit = int(top_n)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_n 必须是正整数。") from exc
    if limit < 1:
        raise ValueError("top_n 必须是正整数。")
    if ranked.empty:
        return IndustryConsensusDetails(None, 0, 0, (), 0)

    top_candidates = ranked.head(limit).copy()
    if industry_column in top_candidates.columns:
        industries = top_candidates[industry_column].map(normalize_industry)
    else:
        industries = pd.Series(
            None,
            index=top_candidates.index,
            dtype="object",
        )
    valid_industries = industries.notna()
    if not bool(valid_industries.any()):
        return IndustryConsensusDetails(None, len(top_candidates), 0, (), 0)
    top_candidates = top_candidates.loc[valid_industries].copy()
    top_candidates[industry_column] = industries.loc[valid_industries].to_numpy()

    counts = top_candidates[industry_column].value_counts(sort=False)
    leading_count = int(counts.max())
    leading_industries = set(counts.loc[counts.eq(leading_count)].index)
    leading_candidates = top_candidates.loc[
        top_candidates[industry_column].isin(leading_industries)
    ]
    if leading_candidates.empty:  # pragma: no cover - 防御性兜底。
        return IndustryConsensusDetails(
            None,
            int(valid_industries.size),
            len(top_candidates),
            tuple(sorted(leading_industries)),
            leading_count,
        )
    return IndustryConsensusDetails(
        leading_candidates.iloc[0].copy(),
        int(valid_industries.size),
        len(top_candidates),
        tuple(sorted(leading_industries)),
        leading_count,
    )


def select_industry_consensus_candidate(
    ranked: pd.DataFrame,
    *,
    top_n: int = DEFAULT_INDUSTRY_CONSENSUS_TOP_N,
    industry_column: str = "所属行业",
) -> pd.Series | None:
    """从已排序候选中返回唯一的行业共识股票，或在无有效行业时返回空。"""

    return describe_industry_consensus(
        ranked,
        top_n=top_n,
        industry_column=industry_column,
    ).candidate
