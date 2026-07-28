from __future__ import annotations

import unittest

import pandas as pd

import szse_quant_app as app


def factor_history() -> pd.DataFrame:
    count = app.INDICATOR_WARMUP_BARS
    positions = pd.Series(range(count), dtype="float64")
    close = 10.0 + positions * 0.01
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-02", periods=count),
            "open": close - 0.02,
            "close": close,
            "high": close + 0.20,
            "low": close - 0.20,
            "volume": 100.0,
            "amount": 100_000_000.0,
            "amplitude": 2.0,
            "pct_change": 0.5,
            "turnover": 5.0,
        }
    )


class CorrectedFactorFormulaTests(unittest.TestCase):
    def test_warmup_preserves_sixty_valid_kdj_values_through_yesterday(self) -> None:
        history = factor_history()

        _, _, _, j_line = app._calculate_kdj_series(history)

        self.assertEqual(app.INDICATOR_WARMUP_BARS, 150)
        self.assertGreaterEqual(int(j_line.iloc[:-1].notna().sum()), 60)

    def test_volume_ratio_uses_current_volume_over_previous_five_days(self) -> None:
        history = factor_history()
        history.loc[history.index[-6:-1], "volume"] = 100.0
        history.loc[history.index[-1], "volume"] = 200.0

        factors = app.calculate_factors(history)

        self.assertEqual(factors["5日均量"], 100.0)
        self.assertEqual(factors["量比"], 2.0)

    def test_upper_shadow_excludes_bearish_candle_body(self) -> None:
        history = factor_history()
        last = history.index[-1]
        history.loc[last, ["open", "close", "high", "low"]] = [12.0, 11.0, 12.2, 10.0]

        factors = app.calculate_factors(history)

        self.assertAlmostEqual(factors["上影线比例"], 0.2 / 2.2 * 100.0)

    def test_resistance_uses_previous_sixty_highs_without_current_high(self) -> None:
        history = factor_history()
        previous_high = float(history.iloc[-61:-1]["high"].max())
        last = history.index[-1]
        close = previous_high * 1.01
        history.loc[last, ["open", "close", "high", "low"]] = [
            close - 0.05,
            close,
            previous_high * 1.20,
            close - 0.10,
        ]

        factors = app.calculate_factors(history)

        self.assertAlmostEqual(factors["60日最高价"], previous_high)
        self.assertTrue(factors["触及60日高点压力"])


class CorrectedSelectionTests(unittest.TestCase):
    def test_zero_change_is_not_counted_as_positive(self) -> None:
        factors = pd.DataFrame({"当日涨跌幅": [-0.1, 0.0, 0.1]})

        matrix = app.condition_matrix(
            factors,
            {"positive_change": True},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
        )

        self.assertEqual(matrix["当日上涨"].tolist(), [False, False, True])

    def test_rsi_range_includes_boundaries_and_rejects_out_of_bounds_values(self) -> None:
        factors = pd.DataFrame(
            {"RSI14": [49.9, 50.0, 75.0, 100.0, 100.1, None]}
        )
        settings = {
            "turnover_range": (5.0, 10.0),
            "float_market_cap_range_yi": (50.0, 200.0),
            "pct_change_range": (3.0, 5.0),
            "amplitude_threshold": 3.0,
        }

        matrix = app.condition_matrix(
            factors,
            {"rsi_in_range": True},
            rsi_range=(50.0, 100.0),
            **settings,
        )
        self.assertEqual(
            matrix["RSI区间[50, 100]"].tolist(),
            [False, True, True, True, False, False],
        )

        for value in ((-0.1, 100.0), (50.0, 100.1)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "RSI区间必须在0至100之间"):
                    app.condition_matrix(
                        factors,
                        {"rsi_in_range": True},
                        rsi_range=value,
                        **settings,
                    )

    def test_require_all_rejects_mutually_exclusive_conditions(self) -> None:
        with self.assertRaisesRegex(ValueError, "互斥条件"):
            app.score_and_select(
                pd.DataFrame(),
                {"macd_bullish": True, "macd_bearish": True},
                require_all=True,
            )

    def test_tied_scores_use_existing_signal_strength_instead_of_sequence(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2],
                "股票代码": ["000001", "000002"],
                "股票名称": ["甲", "乙"],
                "当日成交额": [100_000_000.0, 100_000_000.0],
                "量比": [1.1, 1.8],
                "收盘日内位置（%）": [80.0, 70.0],
                "数据来源": ["测试", "测试"],
            }
        )

        results, _, _ = app.score_and_select(
            factors,
            {"amount_at_least_100m": True},
            require_all=True,
        )

        self.assertEqual(results.iloc[0]["股票代码"], "000002")


if __name__ == "__main__":
    unittest.main()
