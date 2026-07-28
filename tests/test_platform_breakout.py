import unittest

import pandas as pd

import szse_quant_app as app


def platform_history() -> pd.DataFrame:
    """构造一组在最后一日收盘突破此前20日平台的完整日线。"""

    count = app.INDICATOR_WARMUP_BARS
    dates = pd.date_range(end="2024-06-14", periods=count, freq="B")
    closes = pd.Series([10.0 + index * 0.02 for index in range(count)], dtype="float64")
    history = pd.DataFrame(
        {
            "date": dates,
            "open": closes - 0.02,
            "close": closes,
            "high": closes + 0.10,
            "low": closes - 0.10,
            "volume": [1_000_000.0] * count,
            "amount": [100_000_000.0] * count,
            "amplitude": [2.0] * count,
            "pct_change": [0.5] * count,
            "turnover": [5.0] * count,
        }
    )
    prior_high = history.loc[count - 21 : count - 2, "high"].max()
    last_index = history.index[-1]
    history.loc[last_index, "open"] = prior_high - 0.05
    history.loc[last_index, "close"] = prior_high + 0.05
    history.loc[last_index, "high"] = prior_high + 0.10
    history.loc[last_index, "low"] = prior_high - 0.10
    return history


class PlatformBreakoutFactorTests(unittest.TestCase):
    def test_breakout_uses_previous_twenty_highs_and_requires_close_confirmation(self) -> None:
        history = platform_history()
        prior_high = history.iloc[-21:-1]["high"].max()

        factors = app.calculate_factors(history)

        self.assertAlmostEqual(factors["前20日平台最高价"], prior_high)
        self.assertTrue(factors["收盘突破前20日平台"])
        self.assertAlmostEqual(
            factors["平台突破幅度（%）"],
            (history.iloc[-1]["close"] - prior_high) / prior_high * 100.0,
        )
        # 当日最高价高于收盘；若错误将当日高点纳入平台，此处会被误判为未突破。
        self.assertLess(history.iloc[-1]["close"], history.iloc[-1]["high"])

    def test_touch_or_intraday_pierce_without_close_confirmation_is_not_breakout(self) -> None:
        for close_offset in (0.0, -0.01):
            with self.subTest(close_offset=close_offset):
                history = platform_history()
                prior_high = history.iloc[-21:-1]["high"].max()
                last_index = history.index[-1]
                history.loc[last_index, "close"] = prior_high + close_offset
                history.loc[last_index, "high"] = prior_high + 0.30
                history.loc[last_index, "low"] = prior_high - 0.10

                factors = app.calculate_factors(history)

                self.assertFalse(factors["收盘突破前20日平台"])

    def test_ma5_and_close_position_conditions_cover_boundary_and_one_price_day(self) -> None:
        history = platform_history()
        prior_high = history.iloc[-21:-1]["high"].max()
        last_index = history.index[-1]
        history.loc[last_index, "low"] = prior_high - 1.0
        history.loc[last_index, "high"] = prior_high + 1.0
        history.loc[last_index, "close"] = prior_high + 0.40

        factors = app.calculate_factors(history)

        self.assertTrue(factors["MA5上行"])
        self.assertAlmostEqual(factors["收盘日内位置（%）"], 70.0)
        self.assertTrue(factors["收盘位于日内高位"])

        one_price_history = platform_history()
        one_price_prior_high = one_price_history.iloc[-21:-1]["high"].max()
        one_price_index = one_price_history.index[-1]
        one_price_history.loc[one_price_index, ["open", "close", "high", "low"]] = (
            one_price_prior_high + 0.10
        )
        one_price_factors = app.calculate_factors(one_price_history)

        self.assertEqual(one_price_factors["收盘日内位置（%）"], 100.0)
        self.assertTrue(one_price_factors["收盘位于日内高位"])

    def test_ma5_decline_is_not_treated_as_rising(self) -> None:
        history = platform_history()
        closing_prices = [16.0, 15.0, 14.0, 13.0, 12.0, 11.0]
        for index, close in zip(history.index[-6:], closing_prices):
            history.loc[index, "open"] = close
            history.loc[index, "close"] = close
            history.loc[index, "high"] = close + 0.10
            history.loc[index, "low"] = close - 0.10

        factors = app.calculate_factors(history)

        self.assertFalse(factors["MA5上行"])


class PlatformBreakoutSelectionTests(unittest.TestCase):
    def test_platform_conditions_score_and_require_all(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2, 3],
                "股票代码": ["000001", "000002", "000003"],
                "股票名称": ["甲", "乙", "丙"],
                "收盘突破前20日平台": [True, True, False],
                "MA5上行": [True, False, True],
                "收盘位于日内高位": [True, True, False],
                "数据来源": ["测试"] * 3,
            }
        )
        selected = {
            "platform_breakout_20d": True,
            "ma5_rising": True,
            "close_near_daily_high": True,
        }

        matrix = app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
        )
        results, eligible_count, _ = app.score_and_select(
            factors,
            selected,
            require_all=True,
        )

        self.assertEqual(app.maximum_score(selected), 3.0)
        self.assertEqual(matrix["收盘突破前20日平台"].tolist(), [True, True, False])
        self.assertEqual(matrix["MA5上行"].tolist(), [True, False, True])
        self.assertEqual(matrix["收盘位于日内高位（上30%）"].tolist(), [True, True, False])
        self.assertEqual(eligible_count, 1)
        self.assertEqual(results["股票代码"].tolist(), ["000001"])


if __name__ == "__main__":
    unittest.main()
