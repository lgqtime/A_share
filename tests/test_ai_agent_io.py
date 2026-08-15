from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from ai_agent_models import Candidate, ConceptResult
from ai_agent_io import CacheStore, CandidateInputError, RunPaths, load_candidates, write_manifest


def test_load_candidates_reads_every_valid_unique_row_from_fixed_file(tmp_path: Path) -> None:
    source = tmp_path / "前 50 名（含所属行业）.csv"
    pd.DataFrame(
        {
            "股票代码": [66, "600519", 66, "bad"],
            "股票名称": ["甲", "乙", "重复", "无效"],
            "所属行业": ["制造", "消费", "制造", "其他"],
        }
    ).to_csv(source, index=False, encoding="utf-8-sig")

    candidates, ignored = load_candidates(source)

    assert candidates == [Candidate("000066", "甲"), Candidate("600519", "乙")]
    assert ignored == [
        {"row": 4, "reason": "重复股票代码: 000066"},
        {"row": 5, "reason": "无效股票代码"},
    ]


def test_load_candidates_rejects_file_without_required_chinese_columns(tmp_path: Path) -> None:
    source = tmp_path / "前 50 名（含所属行业）.csv"
    pd.DataFrame({"stock_code": ["000066"], "stock_name": ["中国长城"]}).to_csv(
        source, index=False, encoding="utf-8-sig"
    )

    with pytest.raises(CandidateInputError, match="股票代码、股票名称"):
        load_candidates(source)


def test_output_writer_creates_independent_run_directory_and_manifest(tmp_path: Path) -> None:
    run = RunPaths.create(tmp_path, date(2026, 8, 14), "153000")

    write_manifest(run, {"candidate_count": 2, "candidate_shortfall": True})

    assert run.root == tmp_path / "ai_agent_outputs" / "20260814" / "153000"
    assert run.manifest.exists()
    assert run.top10_csv == run.root / "top10_recommendations.csv"
    assert json.loads(run.manifest.read_text(encoding="utf-8")) == {
        "candidate_count": 2,
        "candidate_shortfall": True,
    }


def test_disabled_cache_store_does_not_read_or_write_concepts(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    cache.enabled = False
    result = ConceptResult("000066", "中国长城", ())

    cache.write_concept(
        date(2026, 8, 14),
        result,
        "deepseek-chat",
        "concept-v2",
        '{"stock_code":"000066","stock_name":"中国长城","concepts":[]}',
    )

    assert cache.read_concept(
        date(2026, 8, 14), "000066", "deepseek-chat", "concept-v2"
    ) is None
    assert not cache.root.exists()
