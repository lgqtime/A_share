import unittest

import pandas as pd

import szse_quant_app as app
from strategy_backtest import backtest_core as core
from szse_quant_app import (
    DEFAULT_FLOAT_MARKET_CAP_RANGE_YI,
    FLOAT_MARKET_CAP_MAX_YI,
    FLOAT_MARKET_CAP_MIN_YI,
    condition_matrix,
    maximum_score,
    score_and_select,
)


class AmountFactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factors = pd.DataFrame(
            {
                "序号": [1, 2, 3],
                "股票代码": ["000001", "000002", "000003"],
                "股票名称": ["甲", "乙", "丙"],
                "当日成交额": [100_000_000, 99_999_999, None],
                "数据来源": ["测试"] * 3,
            }
        )
        self.selected = {"amount_at_least_100m": True}

    def test_daily_amount_threshold_includes_boundary_and_excludes_missing(self) -> None:
        matrix = condition_matrix(
            self.factors,
            self.selected,
            turnover_range=(2.0, 3.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
        )

        self.assertEqual(matrix["日成交额≥1亿元"].tolist(), [True, False, False])

    def test_daily_amount_threshold_scores_and_selects_matching_stock(self) -> None:
        results, eligible_count, risk_excluded_count = score_and_select(
            self.factors,
            self.selected,
            top_n=10,
        )

        self.assertEqual(eligible_count, 1)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000001"])
        self.assertEqual(results["满足条件"].tolist(), ["日成交额≥1亿元"])
        self.assertIn("当日成交额", results.columns)

    def test_maximum_score_counts_only_enabled_scoring_indicators(self) -> None:
        self.assertEqual(
            maximum_score(
                {
                    "above_ma5": True,
                    "amount_at_least_100m": True,
                    "turnover_in_range": True,
                    "float_market_cap_in_range": True,
                    "pct_change_in_range": True,
                    "bias_high": True,
                    "unknown_rule": True,
                }
            ),
            5,
        )


class RangeFactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factors = pd.DataFrame(
            {
                "序号": [1, 2, 3, 4, 5, 6],
                "股票代码": [f"00000{number}" for number in range(1, 7)],
                "股票名称": [f"测试{number}" for number in range(1, 7)],
                "换手率": [4.999, 5.0, 7.5, 10.0, 10.001, None],
                "估算流通市值（亿元）": [49.999, 50.0, 125.0, 200.0, 200.001, None],
                "当日涨跌幅": [2.999, 3.0, 4.0, 5.0, 5.001, None],
                "数据来源": ["测试"] * 6,
            }
        )
        self.selected = {
            "turnover_in_range": True,
            "float_market_cap_in_range": True,
            "pct_change_in_range": True,
        }

    def test_range_rules_include_both_boundaries_and_exclude_missing_values(self) -> None:
        matrix = condition_matrix(
            self.factors,
            self.selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
        )

        expected = [False, True, True, True, False, False]
        self.assertEqual(matrix["换手率5-10%"].tolist(), expected)
        self.assertEqual(matrix["流通市值50-200亿元"].tolist(), expected)
        self.assertEqual(matrix["涨幅3-5%"].tolist(), expected)

    def test_float_market_cap_range_uses_configured_limits_and_keeps_default(self) -> None:
        self.assertEqual(FLOAT_MARKET_CAP_MIN_YI, 1.0)
        self.assertEqual(FLOAT_MARKET_CAP_MAX_YI, 1_000.0)
        self.assertEqual(DEFAULT_FLOAT_MARKET_CAP_RANGE_YI, (50.0, 200.0))

        for invalid_range in ((0.0, 1_000.0), (1.0, 1_001.0)):
            with self.subTest(invalid_range=invalid_range):
                with self.assertRaisesRegex(ValueError, "流通市值区间必须在1至1000亿元之间"):
                    condition_matrix(
                        self.factors,
                        self.selected,
                        turnover_range=(5.0, 10.0),
                        float_market_cap_range_yi=invalid_range,
                        pct_change_range=(3.0, 5.0),
                        amplitude_threshold=3.0,
                    )

    def test_all_range_rules_score_one_point_each_and_respect_require_all(self) -> None:
        results, eligible_count, risk_excluded_count = score_and_select(
            self.factors,
            self.selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            require_all=True,
            top_n=10,
        )

        self.assertEqual(eligible_count, 3)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000002", "000003", "000004"])
        self.assertEqual(results["得分"].tolist(), [3, 3, 3])
        self.assertEqual(
            results["满足条件"].tolist(),
            [
                "换手率5-10%；流通市值50-200亿元；涨幅3-5%",
                "换手率5-10%；流通市值50-200亿元；涨幅3-5%",
                "换手率5-10%；流通市值50-200亿元；涨幅3-5%",
            ],
        )


class VolumeRatioFactorTests(unittest.TestCase):
    @staticmethod
    def _condition_matrix(
        target_app: object,
        factors: pd.DataFrame,
        volume_ratio_range: tuple[float, float],
    ) -> pd.DataFrame:
        return target_app.condition_matrix(  # type: ignore[attr-defined]
            factors,
            {"volume_breakout": True},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            volume_ratio_range=volume_ratio_range,
        )

    def test_volume_ratio_uses_an_inclusive_adjustable_range_in_both_paths(
        self,
    ) -> None:
        factors = pd.DataFrame({"量比": [1.5, 1.8, 3.8, 3.8001, None]})

        for target_app in (app, core.strategy_app):
            with self.subTest(target_app=target_app.__name__):
                default_matrix = self._condition_matrix(target_app, factors, (1.8, 3.8))
                custom_matrix = self._condition_matrix(target_app, factors, (1.5, 1.8))

                self.assertEqual(
                    default_matrix[
                        target_app.volume_ratio_range_condition_label((1.8, 3.8))
                    ].tolist(),
                    [False, True, True, False, False],
                )
                self.assertEqual(
                    custom_matrix[
                        target_app.volume_ratio_range_condition_label((1.5, 1.8))
                    ].tolist(),
                    [True, True, False, False, False],
                )

    def test_default_volume_ratio_range_drives_scoring(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2, 3, 4],
                "股票代码": ["000001", "000002", "000003", "000004"],
                "股票名称": ["甲", "乙", "丙", "丁"],
                "量比": [1.7, 1.8, 3.8, 3.9],
                "收盘日内位置（%）": [50.0, 50.0, 50.0, 50.0],
                "当日成交额": [100_000_000.0] * 4,
            }
        )

        results, eligible_count, risk_excluded_count = score_and_select(
            factors,
            {"volume_breakout": True},
            require_all=True,
            top_n=10,
        )

        self.assertEqual(eligible_count, 2)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000003", "000002"])
        self.assertEqual(
            results["满足条件"].tolist(),
            ["放量（量比1.8-3.8）"] * 2,
        )

    def test_invalid_volume_ratio_range_is_rejected(self) -> None:
        factors = pd.DataFrame({"量比": [2.0]})

        for value in ((-0.1, 3.8), (1.8, 15.1), (3.8, 1.8)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "量比区间"):
                    self._condition_matrix(app, factors, value)


if __name__ == "__main__":
    unittest.main()
