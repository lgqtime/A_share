from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd


RESULT_COLUMNS = [
    "股票代码",
    "股票名称",
    "个股得分",
    "个股理由",
    "板块得分",
    "板块理由",
    "综合得分",
    "AI结论",
    "关键风险",
    "缺失来源",
    "分析状态",
    "排名",
    "是否建议关注",
]


def composite_score(individual_score: int, sector_score: int) -> float:
    """Return the specified sector-heavy score for a completed analysis."""
    return round(sector_score * 0.6 + individual_score * 0.4, 2)


def merge_failure_maps(*failure_maps: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Combine source failures while preserving their first-seen order."""
    combined: dict[str, list[str]] = {}
    for failure_map in failure_maps:
        for stock_code, reasons in failure_map.items():
            target = combined.setdefault(str(stock_code), [])
            for reason in reasons:
                text = str(reason).strip()
                if text and text not in target:
                    target.append(text)
    return combined


def merge_analysis_results(
    candidates: Sequence[Mapping[str, object]],
    analysis_results: Sequence[Mapping[str, object]],
    failures: Mapping[str, Sequence[str]],
) -> pd.DataFrame:
    """Merge model results with every input candidate and assign final rankings."""
    results_by_code = {
        _normalized_code(result.get("stock_code")): result
        for result in analysis_results
        if _normalized_code(result.get("stock_code"))
    }
    rows: list[dict[str, Any]] = []

    for input_index, candidate in enumerate(candidates):
        stock_code = _normalized_code(candidate.get("stock_code"))
        stock_name = str(candidate.get("stock_name", "")).strip()
        missing_sources = list(failures.get(stock_code, []))
        result = results_by_code.get(stock_code)
        row = _base_row(stock_code, stock_name, missing_sources, input_index)

        if result is None:
            if not any(reason.startswith("AI ") for reason in row["_missing_sources"]):
                row["_missing_sources"].append("AI 未返回结果")
            rows.append(row)
            continue

        try:
            individual_score = _score(result.get("individual_score"))
            sector_score = _score(result.get("sector_score"))
        except (TypeError, ValueError):
            if "AI 分数无效" not in row["_missing_sources"]:
                row["_missing_sources"].append("AI 分数无效")
            rows.append(row)
            continue

        row.update(
            {
                "个股得分": individual_score,
                "个股理由": _text(result.get("individual_reason")),
                "板块得分": sector_score,
                "板块理由": _text(result.get("sector_reason")),
                "综合得分": composite_score(individual_score, sector_score),
                "AI结论": _text(result.get("final_verdict")),
                "关键风险": _text(result.get("key_risk")),
                "分析状态": "已评分",
            }
        )
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    scored_rows = [row for row in rows if row["分析状态"] == "已评分"]
    unscored_rows = [row for row in rows if row["分析状态"] != "已评分"]
    scored_rows.sort(
        key=lambda row: (
            -float(row["综合得分"]),
            -int(row["板块得分"]),
            -int(row["个股得分"]),
            str(row["股票代码"]),
        )
    )
    unscored_rows.sort(key=lambda row: int(row["_input_index"]))

    for rank, row in enumerate(scored_rows, start=1):
        row["排名"] = rank
        row["是否建议关注"] = rank <= 5

    for row in unscored_rows:
        row["排名"] = pd.NA
        row["是否建议关注"] = False

    output_rows = scored_rows + unscored_rows
    for row in output_rows:
        row["缺失来源"] = "；".join(row.pop("_missing_sources"))
        row.pop("_input_index")

    frame = pd.DataFrame(output_rows, columns=RESULT_COLUMNS)
    frame["排名"] = frame["排名"].astype("Int64")
    frame["是否建议关注"] = frame["是否建议关注"].astype(bool)
    return frame


def _base_row(
    stock_code: str,
    stock_name: str,
    missing_sources: Sequence[str],
    input_index: int,
) -> dict[str, Any]:
    return {
        "股票代码": stock_code,
        "股票名称": stock_name,
        "个股得分": pd.NA,
        "个股理由": "",
        "板块得分": pd.NA,
        "板块理由": "",
        "综合得分": pd.NA,
        "AI结论": "",
        "关键风险": "",
        "缺失来源": "",
        "分析状态": "未评分",
        "排名": pd.NA,
        "是否建议关注": False,
        "_missing_sources": _unique_texts(missing_sources),
        "_input_index": input_index,
    }


def _normalized_code(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _score(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not scores")
    score = int(value)
    if score < 1 or score > 10 or str(value).strip() not in {str(score), f"{score}.0"}:
        raise ValueError("Score must be an integer in the range 1..10")
    return score


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _unique_texts(values: Sequence[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return unique
