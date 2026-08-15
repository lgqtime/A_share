from __future__ import annotations

import unittest

import pandas as pd

import result_merger as merger


class ResultMergerTests(unittest.TestCase):
    def test_ranked_results_use_scores_and_code_as_deterministic_tiebreakers(self) -> None:
        candidates = [
            {"stock_code": f"00000{number}", "stock_name": f"股票{number}"}
            for number in range(1, 7)
        ]
        analysis_results = [
            {
                "stock_code": candidate["stock_code"],
                "stock_name": candidate["stock_name"],
                "individual_score": 8,
                "individual_reason": "个股理由",
                "sector_score": 8,
                "sector_reason": "板块理由",
                "final_verdict": "看好",
                "key_risk": "风险",
            }
            for candidate in reversed(candidates)
        ]

        frame = merger.merge_analysis_results(candidates, analysis_results, {})

        self.assertEqual(frame["股票代码"].tolist(), [item["stock_code"] for item in candidates])
        self.assertEqual(frame["综合得分"].tolist(), [8.0] * 6)
        self.assertEqual(frame["排名"].tolist(), [1, 2, 3, 4, 5, 6])
        self.assertEqual(frame["是否建议关注"].tolist(), [True, True, True, True, True, False])

    def test_unscored_candidate_is_kept_with_missing_sources_and_no_rank(self) -> None:
        candidates = [{"stock_code": "000001", "stock_name": "测试股票"}]

        frame = merger.merge_analysis_results(
            candidates,
            [],
            {"000001": ["实时行情", "AI 分析失败"]},
        )

        self.assertEqual(frame.loc[0, "分析状态"], "未评分")
        self.assertEqual(frame.loc[0, "缺失来源"], "实时行情；AI 分析失败")
        self.assertTrue(pd.isna(frame.loc[0, "排名"]))
        self.assertFalse(frame.loc[0, "是否建议关注"])

    def test_composite_score_weights_sector_more_than_individual(self) -> None:
        self.assertEqual(merger.composite_score(5, 10), 8.0)


if __name__ == "__main__":
    unittest.main()
