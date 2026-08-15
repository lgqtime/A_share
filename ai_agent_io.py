from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ai_agent_models import Candidate, Concept, ConceptResult, DimensionResult, Evidence


CANDIDATE_FILE_NAME = "前 50 名（含所属行业）.csv"


class CandidateInputError(ValueError):
    """Raised when the fixed candidate CSV cannot provide usable candidates."""


@dataclass(frozen=True)
class RunPaths:
    root: Path
    manifest: Path
    normalized_candidates_csv: Path
    step1_jsonl: Path
    step1_summary_csv: Path
    step2_jsonl: Path
    step2_csv: Path
    all_rankings_csv: Path
    top10_csv: Path
    raw_step1_dir: Path
    raw_step2_dir: Path

    @classmethod
    def create(cls, project_root: Path, analysis_date: date, run_id: str) -> "RunPaths":
        date_root = project_root / "ai_agent_outputs" / analysis_date.strftime("%Y%m%d")
        root = date_root / run_id
        suffix = 1
        while root.exists():
            root = date_root / f"{run_id}_{suffix:02d}"
            suffix += 1
        root.mkdir(parents=True, exist_ok=False)
        raw_step1_dir = root / "raw" / "step1"
        raw_step2_dir = root / "raw" / "step2"
        raw_step1_dir.mkdir(parents=True)
        raw_step2_dir.mkdir(parents=True)
        return cls(
            root=root,
            manifest=root / "run_manifest.json",
            normalized_candidates_csv=root / "candidates_normalized.csv",
            step1_jsonl=root / "step1_concepts.jsonl",
            step1_summary_csv=root / "step1_concepts_summary.csv",
            step2_jsonl=root / "step2_search_results.jsonl",
            step2_csv=root / "step2_search_results.csv",
            all_rankings_csv=root / "all_rankings.csv",
            top10_csv=root / "top10_recommendations.csv",
            raw_step1_dir=raw_step1_dir,
            raw_step2_dir=raw_step2_dir,
        )


class CacheStore:
    def __init__(self, project_root: Path, *, enabled: bool = True) -> None:
        self.root = project_root / "ai_agent_cache"
        self.enabled = enabled
        self.hits = 0

    def read_concept(
        self,
        analysis_date: date,
        stock_code: str,
        model_version: str,
        prompt_version: str,
    ) -> ConceptResult | None:
        if not self.enabled:
            return None
        path = self._concept_path(analysis_date, stock_code, model_version, prompt_version)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            concepts = tuple(
                Concept(
                    concept_name=item["concept_name"],
                    concept_rank=item["concept_rank"],
                    is_core=item["is_core"],
                )
                for item in payload["result"]["concepts"]
            )
            result = ConceptResult(
                stock_code=payload["result"]["stock_code"],
                stock_name=payload["result"]["stock_name"],
                concepts=concepts,
                raw_response=payload.get("raw_response"),
                error=payload["result"].get("error"),
            )
            self.hits += 1
            return result
        except (KeyError, TypeError, json.JSONDecodeError):
            return None

    def write_concept(
        self,
        analysis_date: date,
        result: ConceptResult,
        model_version: str,
        prompt_version: str,
        raw_response: str,
    ) -> None:
        if not self.enabled:
            return
        path = self._concept_path(
            analysis_date, result.stock_code, model_version, prompt_version
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_version": model_version,
            "prompt_version": prompt_version,
            "raw_response": raw_response,
            "result": {
                "stock_code": result.stock_code,
                "stock_name": result.stock_name,
                "error": result.error,
                "concepts": [
                    {
                        "concept_name": concept.concept_name,
                        "concept_rank": concept.concept_rank,
                        "is_core": concept.is_core,
                    }
                    for concept in result.concepts
                ],
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _concept_path(
        self,
        analysis_date: date,
        stock_code: str,
        model_version: str,
        prompt_version: str,
    ) -> Path:
        key = f"{_safe_key(model_version)}_{_safe_key(prompt_version)}.json"
        return (
            self.root
            / analysis_date.strftime("%Y%m%d")
            / "concepts"
            / stock_code
            / key
        )

    def read_evidence_dimension(
        self,
        analysis_date: date,
        stock_code: str,
        primary_concept: str,
        dimension: str,
        query_version: str,
    ) -> DimensionResult | None:
        if not self.enabled:
            return None
        path = self._evidence_path(
            analysis_date, stock_code, primary_concept, dimension, query_version
        )
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["primary_concept"] != primary_concept:
                return None
            status = payload["status"]
            if status not in {"success", "empty"}:
                return None
            evidence = tuple(
                Evidence(
                    evidence_id=item["evidence_id"],
                    stock_code=item["stock_code"],
                    dimension=item["dimension"],
                    query=item["query"],
                    title=item["title"],
                    excerpt=item["excerpt"],
                    url=item["url"],
                    published_at=item["published_at"],
                    retrieved_at=item["retrieved_at"],
                )
                for item in payload["evidence"]
            )
            raw_response = payload.get("raw_response")
            result = DimensionResult(
                status=status,
                evidence=evidence,
                raw_response=raw_response if isinstance(raw_response, dict) else None,
            )
            self.hits += 1
            return result
        except (KeyError, TypeError, json.JSONDecodeError):
            return None

    def write_evidence_dimension(
        self,
        analysis_date: date,
        stock_code: str,
        primary_concept: str,
        dimension: str,
        query_version: str,
        result: DimensionResult,
    ) -> None:
        if not self.enabled:
            return
        if result.status not in {"success", "empty"}:
            return
        path = self._evidence_path(
            analysis_date, stock_code, primary_concept, dimension, query_version
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "primary_concept": primary_concept,
            "status": result.status,
            "raw_response": result.raw_response,
            "evidence": [
                {
                    "evidence_id": evidence.evidence_id,
                    "stock_code": evidence.stock_code,
                    "dimension": evidence.dimension,
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
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _evidence_path(
        self,
        analysis_date: date,
        stock_code: str,
        primary_concept: str,
        dimension: str,
        query_version: str,
    ) -> Path:
        return (
            self.root
            / analysis_date.strftime("%Y%m%d")
            / "evidence"
            / stock_code
            / _safe_key(primary_concept)
            / f"{dimension}_{_safe_key(query_version)}.json"
        )


def load_candidates(source: Path) -> tuple[list[Candidate], list[dict[str, object]]]:
    try:
        frame = pd.read_csv(source, dtype={"股票代码": "string"}, encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise CandidateInputError(f"候选文件不存在: {source}") from error
    except Exception as error:
        raise CandidateInputError(f"无法读取候选文件: {source}") from error

    required = ("股票代码", "股票名称")
    if any(column not in frame.columns for column in required):
        raise CandidateInputError("候选文件必须包含列: 股票代码、股票名称")

    candidates: list[Candidate] = []
    ignored: list[dict[str, object]] = []
    seen_codes: set[str] = set()
    for row_number, record in enumerate(frame.loc[:, required].to_dict("records"), start=2):
        stock_code = _normalize_stock_code(record["股票代码"])
        stock_name = _text(record["股票名称"])
        if stock_code is None:
            ignored.append({"row": row_number, "reason": "无效股票代码"})
            continue
        if not stock_name:
            ignored.append({"row": row_number, "reason": "无效股票名称"})
            continue
        if stock_code in seen_codes:
            ignored.append({"row": row_number, "reason": f"重复股票代码: {stock_code}"})
            continue
        candidates.append(Candidate(stock_code, stock_name))
        seen_codes.add(stock_code)

    if not candidates:
        raise CandidateInputError("候选文件没有有效的股票代码和名称")
    return candidates, ignored


def write_manifest(run: RunPaths, payload: dict[str, Any]) -> None:
    run.manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False, default=str))
            output.write("\n")


def _normalize_stock_code(value: object) -> str | None:
    text = _text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or len(text) > 6:
        return None
    return text.zfill(6)


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _safe_key(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
