import unittest
from unittest.mock import patch

import pandas as pd

import szse_quant_app as app


def valid_history() -> pd.DataFrame:
    """生成满足全部基础技术指标预热要求的模拟日线。"""

    dates = pd.date_range(end="2024-06-14", periods=app.INDICATOR_WARMUP_BARS, freq="B")
    closes = pd.Series(
        [10.0 + index * 0.05 for index in range(len(dates))], dtype="float64"
    )
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes - 0.02,
            "close": closes,
            "high": closes + 0.10,
            "low": closes - 0.10,
            "volume": [1_000_000.0] * len(dates),
            "amount": [100_000_000.0] * len(dates),
            "amplitude": [2.0] * len(dates),
            "pct_change": [0.5] * len(dates),
            "turnover": [3.0] * len(dates),
        }
    )


def patched_kdj_series(
    k_values: list[float], d_values: list[float]
):
    """构造供完整因子流程使用的确定性 KDJ 序列。"""

    if len(k_values) != len(d_values):
        raise ValueError("K、D 序列长度必须一致。")
    j_values = [3.0 * k_value - 2.0 * d_value for k_value, d_value in zip(k_values, d_values)]

    def calculate(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        if len(bars) != len(k_values):
            raise AssertionError("模拟 KDJ 序列长度必须与输入日线一致。")
        index = bars.index
        return (
            pd.Series([10.0] * len(bars), index=index, dtype="float64"),
            pd.Series(k_values, index=index, dtype="float64"),
            pd.Series(d_values, index=index, dtype="float64"),
            pd.Series(j_values, index=index, dtype="float64"),
        )

    return calculate


def calculate_with_patched_kdj(k_values: list[float], d_values: list[float]) -> dict[str, object]:
    """隔离 KDJ 信号窗口逻辑，避免其他因子走势影响断言。"""

    with (
        patch.object(
            app,
            "_calculate_kdj_series",
            side_effect=patched_kdj_series(k_values, d_values),
        ),
        patch.object(app, "_has_kdj_top_divergence", return_value=False),
    ):
        return app.calculate_factors(valid_history())


class KdjCalculationTests(unittest.TestCase):
    def test_kdj_requires_complete_warmup_window(self) -> None:
        with self.assertRaisesRegex(ValueError, f"不足 {app.INDICATOR_WARMUP_BARS} 根"):
            app.calculate_factors(valid_history().iloc[1:].reset_index(drop=True))

    def test_kdj_uses_50_seed_and_standard_3_3_smoothing(self) -> None:
        bars = pd.DataFrame(
            {
                "low": [0.0] * (app.KDJ_RSV_PERIOD + 2),
                "high": [100.0] * (app.KDJ_RSV_PERIOD + 2),
                "close": [0.0] * (app.KDJ_RSV_PERIOD + 2),
            }
        )

        rsv, k_line, d_line, j_line = app._calculate_kdj_series(bars)
        first_valid = app.KDJ_RSV_PERIOD - 1

        self.assertTrue(rsv.iloc[:first_valid].isna().all())
        self.assertEqual(rsv.iloc[first_valid], 0.0)
        self.assertAlmostEqual(k_line.iloc[first_valid], 100.0 / 3.0)
        self.assertAlmostEqual(d_line.iloc[first_valid], 400.0 / 9.0)
        self.assertAlmostEqual(j_line.iloc[first_valid], 100.0 / 9.0)
        self.assertAlmostEqual(k_line.iloc[first_valid + 1], 200.0 / 9.0)
        self.assertAlmostEqual(d_line.iloc[first_valid + 1], 1000.0 / 27.0)

    def test_healthy_cross_records_its_date_and_zero_based_trading_day_age(self) -> None:
        count = app.INDICATOR_WARMUP_BARS
        golden_cross_index = count - 3
        k_values = [10.0] * count
        d_values = [10.0] * count
        k_values[golden_cross_index] = 12.0
        d_values[golden_cross_index] = 11.0
        for index in range(golden_cross_index + 1, count):
            k_values[index] = 13.0
            d_values[index] = 11.0

        factors = calculate_with_patched_kdj(k_values, d_values)

        self.assertTrue(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertEqual(
            factors[app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN],
            valid_history()["date"].iloc[golden_cross_index].date().isoformat(),
        )
        self.assertEqual(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN], 2)

    def test_current_day_healthy_golden_cross_is_a_valid_signal_with_zero_age(self) -> None:
        count = app.INDICATOR_WARMUP_BARS
        k_values = [10.0] * count
        d_values = [10.0] * count
        k_values[-1] = 12.0
        d_values[-1] = 11.0

        factors = calculate_with_patched_kdj(k_values, d_values)

        self.assertTrue(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertEqual(
            factors[app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN],
            valid_history()["date"].iloc[-1].date().isoformat(),
        )
        self.assertEqual(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN], 0)

    def test_dead_cross_after_a_healthy_cross_invalidates_the_signal(self) -> None:
        count = app.INDICATOR_WARMUP_BARS
        golden_cross_index = count - 5
        k_values = [10.0] * count
        d_values = [10.0] * count
        k_values[golden_cross_index] = 12.0
        d_values[golden_cross_index] = 11.0
        for index in range(golden_cross_index + 1, count - 3):
            k_values[index] = 13.0
            d_values[index] = 11.0
        k_values[-3] = 10.0
        d_values[-3] = 11.0

        factors = calculate_with_patched_kdj(k_values, d_values)

        self.assertFalse(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertTrue(pd.isna(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN]))
        self.assertTrue(pd.isna(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN]))

    def test_healthy_cross_requires_j_to_rise_at_the_cross_day(self) -> None:
        count = app.INDICATOR_WARMUP_BARS
        golden_cross_index = count - 4
        k_values = [10.0] * count
        d_values = [10.0] * count
        k_values[golden_cross_index - 1] = 18.0
        d_values[golden_cross_index - 1] = 18.0
        k_values[golden_cross_index] = 12.0
        d_values[golden_cross_index] = 11.0
        for index in range(golden_cross_index + 1, count):
            k_values[index] = 13.0
            d_values[index] = 11.0

        factors = calculate_with_patched_kdj(k_values, d_values)

        self.assertFalse(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertTrue(pd.isna(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN]))

    def test_top_divergence_at_the_cross_day_rejects_healthy_golden_cross(self) -> None:
        count = app.INDICATOR_WARMUP_BARS
        golden_cross_index = count - 4
        k_values = [10.0] * count
        d_values = [10.0] * count
        k_values[golden_cross_index] = 12.0
        d_values[golden_cross_index] = 11.0
        for index in range(golden_cross_index + 1, count):
            k_values[index] = 13.0
            d_values[index] = 11.0

        with (
            patch.object(
                app,
                "_calculate_kdj_series",
                side_effect=patched_kdj_series(k_values, d_values),
            ),
            patch.object(app, "_has_kdj_top_divergence", return_value=True) as divergence,
        ):
            factors = app.calculate_factors(valid_history())

        self.assertFalse(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertTrue(pd.isna(factors[app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN]))
        self.assertEqual(len(divergence.call_args.args[0]), golden_cross_index + 1)

    def test_top_divergence_uses_two_latest_peaks_in_60_trading_days(self) -> None:
        highs = [10.0] * app.KDJ_DIVERGENCE_LOOKBACK_BARS
        j_values = [50.0] * app.KDJ_DIVERGENCE_LOOKBACK_BARS
        highs[15] = 11.0
        highs[40] = 12.0
        j_values[15] = 80.0
        j_values[40] = 60.0
        bars = pd.DataFrame({"high": highs, "kdj_j": j_values})

        self.assertTrue(app._has_kdj_top_divergence(bars))

        bars.loc[40, "kdj_j"] = 85.0
        self.assertFalse(app._has_kdj_top_divergence(bars))


class KdjScoringTests(unittest.TestCase):
    def test_kdj_rule_scores_one_point_five_and_require_all_uses_recent_cross_age(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2, 3],
                "股票代码": ["000001", "000002", "000003"],
                "股票名称": ["甲", "乙", "丙"],
                "站上MA5": [True, False, True],
                app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: [True, True, False],
                app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: [2, 3, None],
                "数据来源": ["测试"] * 3,
            }
        )
        selected = {
            "above_ma5": True,
            "kdj_healthy_golden_cross_3d": True,
        }

        self.assertEqual(app.maximum_score(selected), 2.5)
        results, eligible_count, risk_excluded_count = app.score_and_select(
            factors,
            selected,
            require_all=False,
            top_n=10,
        )

        self.assertEqual(eligible_count, 3)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000001", "000002", "000003"])
        self.assertEqual(results["得分"].tolist(), [2.5, 1.5, 1.0])

        results, eligible_count, risk_excluded_count = app.score_and_select(
            factors,
            selected,
            require_all=True,
            top_n=10,
        )

        self.assertEqual(eligible_count, 1)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000001"])
        self.assertEqual(results["得分"].tolist(), [2.5])

    def test_kdj_age_range_accepts_zero_to_ten_and_includes_both_bounds(self) -> None:
        factors = pd.DataFrame(
            {
                app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: [
                    True,
                    True,
                    True,
                    True,
                    True,
                    True,
                    False,
                ],
                app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: [0, 1, 2, 3, 4, 10, None],
            }
        )
        selected = {"kdj_healthy_golden_cross_3d": True}

        current_day_matrix = app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            kdj_healthy_golden_cross_age_range=(0, 0),
        )
        default_range_matrix = app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            kdj_healthy_golden_cross_age_range=(2, 3),
        )
        full_range_matrix = app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            kdj_healthy_golden_cross_age_range=(0, 10),
        )

        self.assertEqual(
            list(default_range_matrix.columns),
            [app.kdj_healthy_golden_cross_condition_label((2, 3))],
        )
        self.assertEqual(
            current_day_matrix.iloc[:, 0].tolist(),
            [True, False, False, False, False, False, False],
        )
        self.assertEqual(
            default_range_matrix.iloc[:, 0].tolist(),
            [False, False, True, True, False, False, False],
        )
        self.assertEqual(
            full_range_matrix.iloc[:, 0].tolist(),
            [True, True, True, True, True, True, False],
        )

    def test_unmet_selected_conditions_are_listed_as_deductions(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2, 3, 4],
                "股票代码": ["000001", "000002", "000003", "000004"],
                "股票名称": ["甲", "乙", "丙", "丁"],
                "站上MA5": [True, True, True, False],
                "站上MA20": [True, False, True, False],
                app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: [True, True, False, True],
                app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: [2, 3, None, 2],
                # MACD 多头未勾选；即使不满足也不能进入扣分项。
                "MACD多头": [False, False, False, False],
                "数据来源": ["测试"] * 4,
            }
        )
        selected = {
            "above_ma5": True,
            "above_ma20": True,
            "kdj_healthy_golden_cross_3d": True,
            "macd_bullish": False,
        }

        results, eligible_count, risk_excluded_count = app.score_and_select(
            factors,
            selected,
            require_all=False,
            top_n=10,
        )

        self.assertEqual(app.maximum_score(selected), 3.5)
        self.assertEqual(eligible_count, 4)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(results["股票代码"].tolist(), ["000001", "000002", "000003", "000004"])
        self.assertEqual(results["得分"].tolist(), [3.5, 2.5, 2.0, 1.5])
        self.assertEqual(
            results["未满足条件（扣分项）"].tolist(),
            [
                "无",
                "站上20日线（-1分）",
                f"{app.KDJ_HEALTHY_GOLDEN_CROSS_LABEL}（-1.5分）",
                "站上5日线（-1分）；站上20日线（-1分）",
            ],
        )
        self.assertFalse(
            results["未满足条件（扣分项）"].str.contains("MACD多头", regex=False).any()
        )


if __name__ == "__main__":
    unittest.main()
