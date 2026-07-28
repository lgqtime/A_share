"""独立回测的近期 KDJ 健康金叉规则测试。"""

from __future__ import annotations

from datetime import date
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from strategy_backtest import backtest_core as core
from strategy_backtest import factor_batch


strategy_app = core.strategy_app


def valid_history() -> pd.DataFrame:
    """构造满足回测指标预热要求的完整日线。"""

    count = strategy_app.INDICATOR_WARMUP_BARS
    dates = pd.bdate_range("2025-01-02", periods=count)
    closes = pd.Series([10.0 + index * 0.05 for index in range(count)], dtype="float64")
    previous_close = closes.shift(1)
    return strategy_app._normalize_history_frame(
        pd.DataFrame(
            {
                "date": dates,
                "open": closes - 0.02,
                "close": closes,
                "high": closes + 0.10,
                "low": closes - 0.10,
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
                "amplitude": 2.0,
                "pct_change": closes.div(previous_close).sub(1.0).mul(100.0).fillna(0.0),
                "turnover": 3.0,
            }
        )
    )


def patched_kdj_series(
    k_values: list[float], d_values: list[float]
):
    """返回指定的 K、D 序列，J 线按标准公式推导。"""

    j_values = [3.0 * k_value - 2.0 * d_value for k_value, d_value in zip(k_values, d_values)]

    def calculate(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        if len(bars) != len(k_values):
            raise AssertionError("模拟 KDJ 序列长度必须与输入日线一致。")
        return (
            pd.Series(10.0, index=bars.index, dtype="float64"),
            pd.Series(k_values, index=bars.index, dtype="float64"),
            pd.Series(d_values, index=bars.index, dtype="float64"),
            pd.Series(j_values, index=bars.index, dtype="float64"),
        )

    return calculate


def recent_healthy_cross_values(
    *, trading_days_before_signal: int = 3, dead_cross_after: bool = False
) -> tuple[list[float], list[float]]:
    """构造发生在信号日前指定交易日数的低位金叉序列。"""

    count = strategy_app.INDICATOR_WARMUP_BARS
    cross_index = count - 1 - trading_days_before_signal
    k_values = [10.0] * count
    d_values = [10.0] * count
    k_values[cross_index] = 12.0
    d_values[cross_index] = 11.0
    for index in range(cross_index + 1, count):
        k_values[index] = 13.0
        d_values[index] = 11.0
    if dead_cross_after:
        # 将死叉放在当日，避免死叉后又因 K 重新上穿 D 而形成新的当日金叉。
        k_values[-1] = 10.0
        d_values[-1] = 11.0
    return k_values, d_values


class BacktestKdjRecentCrossTests(TestCase):
    def test_backtest_strategy_copy_uses_the_zero_to_ten_age_range_defaults(self) -> None:
        self.assertEqual(strategy_app.MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE, 0)
        self.assertEqual(strategy_app.MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE, 10)
        self.assertEqual(
            strategy_app.DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE,
            (1, 3),
        )

    def test_snapshot_records_a_current_day_healthy_cross_with_zero_age(self) -> None:
        history = valid_history()
        k_values, d_values = recent_healthy_cross_values(trading_days_before_signal=0)

        with (
            patch.object(
                strategy_app,
                "_calculate_kdj_series",
                side_effect=patched_kdj_series(k_values, d_values),
            ),
            patch.object(strategy_app, "_has_kdj_top_divergence", return_value=False),
        ):
            factors = strategy_app._calculate_factors_from_normalized_history(history)

        self.assertTrue(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertEqual(
            factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN],
            history["date"].iloc[-1].date().isoformat(),
        )
        self.assertEqual(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN], 0)

    def test_post_cross_dead_cross_invalidates_the_earlier_signal(self) -> None:
        k_values, d_values = recent_healthy_cross_values(
            trading_days_before_signal=3,
            dead_cross_after=True,
        )

        with (
            patch.object(
                strategy_app,
                "_calculate_kdj_series",
                side_effect=patched_kdj_series(k_values, d_values),
            ),
            patch.object(strategy_app, "_has_kdj_top_divergence", return_value=False),
        ):
            factors = strategy_app._calculate_factors_from_normalized_history(valid_history())

        self.assertFalse(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertTrue(
            pd.isna(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN])
        )
        self.assertTrue(
            pd.isna(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN])
        )

    def test_precomputed_fallback_keeps_a_current_day_cross_with_zero_age(self) -> None:
        history = valid_history()
        k_values, d_values = recent_healthy_cross_values(trading_days_before_signal=0)

        with (
            patch.object(
                strategy_app,
                "_calculate_kdj_series",
                side_effect=patched_kdj_series(k_values, d_values),
            ),
            patch.object(strategy_app, "_has_kdj_top_divergence", return_value=False),
        ):
            bars = core._precompute_factor_bars(history)
            factors = core._factor_values_from_precomputed_bars(bars, len(bars) - 1)

        self.assertTrue(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertEqual(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN], 0)

    def test_condition_matrix_uses_the_adjustable_zero_based_age_range(self) -> None:
        factors = pd.DataFrame(
            {
                strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: [
                    True,
                    True,
                    True,
                    True,
                    True,
                    True,
                    False,
                ],
                strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: [0, 1, 2, 3, 4, 10, None],
            }
        )
        selected = {"kdj_healthy_golden_cross_3d": True}
        current_day_matrix = strategy_app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            kdj_healthy_golden_cross_age_range=(0, 0),
        )
        default_range_matrix = strategy_app.condition_matrix(
            factors,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            kdj_healthy_golden_cross_age_range=(2, 3),
        )
        full_range_matrix = strategy_app.condition_matrix(
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
            [strategy_app.kdj_healthy_golden_cross_condition_label((2, 3))],
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


class BatchBacktestKdjRecentCrossTests(TestCase):
    def test_batch_factor_records_a_current_day_cross_with_zero_age(self) -> None:
        history = valid_history()
        signal_day: date = history["date"].iloc[-1].date()

        def controlled_kdj(
            close: pd.DataFrame,
            high: pd.DataFrame,
            low: pd.DataFrame,
            **_: object,
        ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
            rsv = pd.DataFrame(10.0, index=close.index, columns=close.columns)
            k_line = pd.DataFrame(10.0, index=close.index, columns=close.columns)
            d_line = pd.DataFrame(10.0, index=close.index, columns=close.columns)
            k_line.iloc[-1, :] = 12.0
            d_line.iloc[-1, :] = 11.0
            j_line = 3.0 * k_line - 2.0 * d_line
            return rsv, k_line, d_line, j_line

        with (
            patch.object(factor_batch, "_kdj_lines_for_windows", side_effect=controlled_kdj),
            patch.object(factor_batch, "_has_kdj_top_divergence", return_value=False),
        ):
            by_date = factor_batch.calculate_factor_values_for_dates(
                history,
                (signal_day,),
                strategy_app=strategy_app,
            )

        factors = by_date[signal_day.isoformat()]
        self.assertTrue(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME])
        self.assertEqual(factors[strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN], 0)
