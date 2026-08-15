from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Candidate:
    stock_code: str
    stock_name: str


@dataclass(frozen=True)
class Concept:
    concept_name: str
    concept_rank: int
    is_core: bool


@dataclass(frozen=True)
class ConceptResult:
    stock_code: str
    stock_name: str
    concepts: tuple[Concept, ...]
    raw_response: str | None = None
    error: str | None = None

    @property
    def primary_concept(self) -> str | None:
        return self.concepts[0].concept_name if self.concepts else None


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    stock_code: str
    dimension: str
    query: str
    title: str
    excerpt: str
    url: str
    published_at: str | None
    retrieved_at: str


@dataclass(frozen=True)
class DimensionResult:
    status: str
    evidence: tuple[Evidence, ...] = ()
    error: str | None = None
    raw_response: Mapping[str, object] | None = None

    @classmethod
    def skipped(cls) -> "DimensionResult":
        return cls(status="skipped")


@dataclass(frozen=True)
class EvidenceBundle:
    stock_code: str
    dimensions: Mapping[str, DimensionResult]

    @property
    def statuses(self) -> dict[str, str]:
        return {name: result.status for name, result in self.dimensions.items()}

    @property
    def evidence_ids(self) -> set[str]:
        return {
            evidence.evidence_id
            for result in self.dimensions.values()
            for evidence in result.evidence
        }


@dataclass(frozen=True)
class ScoreResult:
    stock_code: str
    stock_name: str
    individual_score: int
    individual_reason: str
    individual_evidence_ids: tuple[str, ...]
    sector_score: int
    sector_reason: str
    sector_evidence_ids: tuple[str, ...]
    final_verdict: str
    risk_level: str
    key_risk: str
    risk_evidence_ids: tuple[str, ...]
    analysis_status: str
