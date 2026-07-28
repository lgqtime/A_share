import unittest

import pandas as pd

import szse_quant_app as app


SHAPE_FIELDS = {
    "doji_3d": "近3日十字星",
    "inverted_t_doji_3d": "近3日倒T字星",
    "hanging_man_3d": "近3日吊颈线",
    "long_upper_shadow_bullish_3d": "近3日长上影阳线",
    "extreme_bullish_3d": "近3日极端大阳线",
}


def valid_history(rows: int = app.INDICATOR_WARMUP_BARS) -> pd.DataFrame:
    """生成不命中五种形态的完整离线日线。"""

    dates = pd.date_range(end="2024-06-14", periods=rows, freq="B")
    closes = pd.Series([100.0 + index * 0.1 for index in range(rows)], dtype="float64")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes - 0.8,
            "close": closes,
            "high": closes + 0.4,
            "low": closes - 1.0,
            "volume": [1_000_000.0] * rows,
            "amount": [100_000_000.0] * rows,
            "amplitude": [2.0] * rows,
            "pct_change": [0.0] * rows,
            "turnover": [3.0] * rows,
        }
    )


def set_ohlc(
    history: pd.DataFrame,
    days_from_end: int,
    *,
    open_price: float,
    close_price: float,
    high_price: float,
    low_price: float,
) -> None:
    index = history.index[-days_from_end]
    history.loc[index, ["open", "close", "high", "low"]] = (
        open_price,
        close_price,
        high_price,
        low_price,
    )


def previous_close(history: pd.DataFrame, days_from_end: int) -> float:
    index = history.index[-days_from_end]
    return float(history.loc[index - 1, "close"])


def set_doji(history: pd.DataFrame, days_from_end: int, *, body_ratio: float = 0.10) -> None:
    anchor = previous_close(history, days_from_end)
    price_range = 10.0
    set_ohlc(
        history,
        days_from_end,
        open_price=anchor,
        close_price=anchor + price_range * body_ratio,
        high_price=anchor + 5.0,
        low_price=anchor - 5.0,
    )


def set_inverted_t_doji(
    history: pd.DataFrame,
    days_from_end: int,
    *,
    lower_shadow: float = 1.0,
) -> None:
    anchor = previous_close(history, days_from_end)
    set_ohlc(
        history,
        days_from_end,
        open_price=anchor + lower_shadow,
        close_price=anchor + lower_shadow,
        high_price=anchor + 10.0,
        low_price=anchor,
    )


def set_hanging_man(
    history: pd.DataFrame,
    days_from_end: int,
    *,
    high_ratio: float = 0.95,
    body: float = 3.0,
) -> None:
    index = history.index[-days_from_end]
    prior_high = float(history.loc[: index - 1, "high"].tail(20).max())
    high_price = prior_high * high_ratio
    set_ohlc(
        history,
        days_from_end,
        open_price=high_price - 2.0 - body,
        close_price=high_price - 2.0,
        high_price=high_price,
        low_price=high_price - 10.0,
    )


def set_long_upper_shadow_bullish(
    history: pd.DataFrame,
    days_from_end: int,
    *,
    upper_shadow: float = 3.01,
) -> None:
    anchor = previous_close(history, days_from_end)
    set_ohlc(
        history,
        days_from_end,
        open_price=anchor,
        close_price=anchor + 3.0,
        high_price=anchor + 3.0 + upper_shadow,
        low_price=anchor - 3.0,
    )


def set_extreme_bullish(
    history: pd.DataFrame,
    days_from_end: int,
    *,
    gain_ratio: float = 0.0801,
) -> None:
    prior_close = previous_close(history, days_from_end)
    close_price = prior_close * (1.0 + gain_ratio)
    set_ohlc(
        history,
        days_from_end,
        open_price=close_price - 1.0,
        close_price=close_price,
        high_price=close_price,
        low_price=close_price - 2.0,
    )


SHAPE_BUILDERS = {
    "doji_3d": set_doji,
    "inverted_t_doji_3d": set_inverted_t_doji,
    "hanging_man_3d": set_hanging_man,
    "long_upper_shadow_bullish_3d": set_long_upper_shadow_bullish,
    "extreme_bullish_3d": set_extreme_bullish,
}


class CandlestickRiskFactorTests(unittest.TestCase):
    def test_recent_window_includes_current_and_third_bar_but_excludes_fourth(self) -> None:
        for key, builder in SHAPE_BUILDERS.items():
            for days_from_end, expected in ((1, True), (3, True), (4, False)):
                with self.subTest(shape=key, days_from_end=days_from_end):
                    history = valid_history()
                    builder(history, days_from_end)

                    factors = app.calculate_factors(history)

                    self.assertEqual(factors[SHAPE_FIELDS[key]], expected)

    def test_shape_threshold_boundaries_are_applied_as_specified(self) -> None:
        cases = (
            (
                "十字星实体恰为10%",
                "doji_3d",
                lambda history: set_doji(history, 1, body_ratio=0.10),
                True,
            ),
            (
                "十字星实体超过10%",
                "doji_3d",
                lambda history: set_doji(history, 1, body_ratio=0.1001),
                False,
            ),
            (
                "倒T字星下影线恰为10%",
                "inverted_t_doji_3d",
                lambda history: set_inverted_t_doji(history, 1, lower_shadow=1.0),
                True,
            ),
            (
                "倒T字星下影线超过10%",
                "inverted_t_doji_3d",
                lambda history: set_inverted_t_doji(history, 1, lower_shadow=1.001),
                False,
            ),
            (
                "吊颈线各比例和95%高点边界",
                "hanging_man_3d",
                lambda history: set_hanging_man(history, 1, high_ratio=0.95, body=3.0),
                True,
            ),
            (
                "吊颈线高点低于前20日最高价95%",
                "hanging_man_3d",
                lambda history: set_hanging_man(history, 1, high_ratio=0.9499, body=3.0),
                False,
            ),
            (
                "长上影阳线上影等于实体",
                "long_upper_shadow_bullish_3d",
                lambda history: set_long_upper_shadow_bullish(history, 1, upper_shadow=3.0),
                False,
            ),
            (
                "长上影阳线上影略大于实体",
                "long_upper_shadow_bullish_3d",
                lambda history: set_long_upper_shadow_bullish(history, 1, upper_shadow=3.01),
                True,
            ),
            (
                "极端大阳线涨幅恰为8%",
                "extreme_bullish_3d",
                lambda history: set_extreme_bullish(history, 1, gain_ratio=0.08),
                False,
            ),
            (
                "极端大阳线涨幅超过8%",
                "extreme_bullish_3d",
                lambda history: set_extreme_bullish(history, 1, gain_ratio=0.0801),
                True,
            ),
        )

        for name, key, configure, expected in cases:
            with self.subTest(name=name):
                history = valid_history()
                configure(history)

                factors = app.calculate_factors(history)

                self.assertEqual(factors[SHAPE_FIELDS[key]], expected)

    def test_long_upper_shadow_and_extreme_bullish_require_a_positive_candle(self) -> None:
        history = valid_history()
        anchor = previous_close(history, 1)
        set_ohlc(
            history,
            1,
            open_price=anchor + 3.0,
            close_price=anchor,
            high_price=anchor + 7.0,
            low_price=anchor - 3.0,
        )
        factors = app.calculate_factors(history)
        self.assertFalse(factors[SHAPE_FIELDS["long_upper_shadow_bullish_3d"]])

        history = valid_history()
        prior_close = previous_close(history, 1)
        close_price = prior_close * 1.0801
        set_ohlc(
            history,
            1,
            open_price=close_price,
            close_price=close_price,
            high_price=close_price,
            low_price=close_price - 2.0,
        )
        factors = app.calculate_factors(history)
        self.assertFalse(factors[SHAPE_FIELDS["extreme_bullish_3d"]])

    def test_missing_ohlc_and_zero_range_do_not_match_any_shape(self) -> None:
        missing_history = valid_history(app.INDICATOR_WARMUP_BARS + 1)
        missing_history.loc[missing_history.index[-3], "open"] = pd.NA
        missing_factors = app.calculate_factors(missing_history)

        zero_range_history = valid_history()
        anchor = previous_close(zero_range_history, 1)
        set_ohlc(
            zero_range_history,
            1,
            open_price=anchor,
            close_price=anchor,
            high_price=anchor,
            low_price=anchor,
        )
        zero_range_factors = app.calculate_factors(zero_range_history)

        for field in SHAPE_FIELDS.values():
            with self.subTest(field=field):
                self.assertFalse(missing_factors[field])
                self.assertFalse(zero_range_factors[field])


class CandlestickRiskSelectionTests(unittest.TestCase):
    def test_missing_shape_factors_are_not_risk_hits(self) -> None:
        factors = pd.DataFrame({field: [None] for field in SHAPE_FIELDS.values()})
        risk_matrix = app.risk_exclusion_matrix(
            factors,
            {key: True for key in SHAPE_FIELDS},
        )

        self.assertEqual(risk_matrix.shape, (1, len(SHAPE_FIELDS)))
        self.assertFalse(bool(risk_matrix.iloc[0].any()))

    def test_matching_shape_is_excluded_after_scoring(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2],
                "股票代码": ["000001", "000002"],
                "股票名称": ["甲", "乙"],
                "站上MA5": [True, True],
                "近3日十字星": [True, False],
                "近3日倒T字星": [False, False],
                "近3日吊颈线": [False, False],
                "近3日长上影阳线": [False, False],
                "近3日极端大阳线": [False, False],
                "数据来源": ["测试", "测试"],
            }
        )

        risk_matrix = app.risk_exclusion_matrix(factors, {"doji_3d": True})
        self.assertEqual(
            risk_matrix[app.CANDLESTICK_RISK_EXCLUSION_LABELS["doji_3d"]].tolist(),
            [True, False],
        )

        results, eligible_count, risk_excluded_count = app.score_and_select(
            factors,
            {"above_ma5": True},
            selected_risks={"doji_3d": True},
            top_n=10,
        )

        self.assertEqual(eligible_count, 1)
        self.assertEqual(risk_excluded_count, 1)
        self.assertEqual(results["股票代码"].tolist(), ["000002"])


class MacdScoringFactorTests(unittest.TestCase):
    def test_macd_red_blue_gap_is_a_range_scoring_factor_not_a_risk_rule(self) -> None:
        factors = pd.DataFrame(
            {
                "序号": [1, 2, 3, 4],
                "股票代码": ["000001", "000002", "000003", "000004"],
                "股票名称": ["甲", "乙", "丙", "丁"],
                "站上MA5": [True, True, True, True],
                # 两个边界都应被纳入，区间外与缺失值均不满足。
                "MACD_DEA": [1.10, 1.20, 1.21, None],
                "MACD_DIF": [1.00, 1.00, 1.00, 1.00],
                "数据来源": ["测试", "测试", "测试", "测试"],
            }
        )
        selected = {
            "above_ma5": True,
            "macd_dea_minus_dif_high": True,
        }

        conditions = app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            macd_dea_minus_dif_range=(0.1, 0.2),
        )
        self.assertEqual(
            conditions["MACD红线-蓝线区间[0.1, 0.2]（DEA-DIF）"].tolist(),
            [True, True, False, False],
        )
        self.assertEqual(
            app.risk_exclusion_matrix(
                factors, {"macd_dea_minus_dif_high": True}
            ).columns.tolist(),
            [],
        )

        results, eligible_count, risk_excluded_count = app.score_and_select(
            factors,
            selected,
            macd_dea_minus_dif_range=(0.1, 0.2),
            top_n=10,
        )
        self.assertEqual(eligible_count, 4)
        self.assertEqual(risk_excluded_count, 0)
        self.assertEqual(
            results["股票代码"].tolist(), ["000001", "000002", "000003", "000004"]
        )
        self.assertEqual(results["得分"].tolist(), [2.0, 2.0, 1.0, 1.0])

        strict_results, strict_count, strict_excluded_count = app.score_and_select(
            factors,
            selected,
            macd_dea_minus_dif_range=(0.1, 0.2),
            require_all=True,
            top_n=10,
        )
        self.assertEqual(strict_count, 2)
        self.assertEqual(strict_excluded_count, 0)
        self.assertEqual(strict_results["股票代码"].tolist(), ["000001", "000002"])

    def test_macd_red_blue_range_rejects_values_outside_configured_bounds(self) -> None:
        factors = pd.DataFrame({"MACD_DEA": [0.0], "MACD_DIF": [0.0]})
        selected = {"macd_dea_minus_dif_high": True}
        settings = {
            "turnover_range": (5.0, 10.0),
            "float_market_cap_range_yi": (50.0, 200.0),
            "pct_change_range": (3.0, 5.0),
            "amplitude_threshold": 3.0,
        }

        for value in ((-1.01, 0.2), (0.1, 1.01)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MACD红线-蓝线区间必须在-1至1之间"):
                    app.condition_matrix(
                        factors,
                        selected,
                        macd_dea_minus_dif_range=value,
                        **settings,
                    )

    def test_macd_range_conflicts_follow_the_selected_direction(self) -> None:
        bullish_selected = {
            "macd_bullish": True,
            "macd_dea_minus_dif_high": True,
        }
        app.validate_selected_conditions(
            bullish_selected,
            require_all=True,
            macd_dea_minus_dif_range=(-0.2, -0.1),
        )
        with self.assertRaisesRegex(ValueError, "MACD"):
            app.validate_selected_conditions(
                bullish_selected,
                require_all=True,
                macd_dea_minus_dif_range=(0.1, 0.2),
            )

        bearish_selected = {
            "macd_bearish": True,
            "macd_dea_minus_dif_high": True,
        }
        app.validate_selected_conditions(
            bearish_selected,
            require_all=True,
            macd_dea_minus_dif_range=(0.1, 0.2),
        )
        with self.assertRaisesRegex(ValueError, "MACD"):
            app.validate_selected_conditions(
                bearish_selected,
                require_all=True,
                macd_dea_minus_dif_range=(-0.2, -0.1),
            )


if __name__ == "__main__":
    unittest.main()
