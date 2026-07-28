from __future__ import annotations

import unittest

import pandas as pd

import szse_quant_app as app
from strategy_backtest import backtest_core as core


class RecentMacdGoldenCrossTests(unittest.TestCase):
    @staticmethod
    def _cross_bars(
        *,
        age: int | None = None,
        dead_cross_after: bool = False,
        current_day_cross: bool = False,
    ) -> pd.DataFrame:
        count = 25
        bars = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-02", periods=count),
                "macd_golden_cross": False,
                "macd_dead_cross": False,
            }
        )
        completed_position = count - 1 - app.MACD_GOLDEN_CROSS_OFFSET_BARS
        if age is not None:
            cross_position = completed_position - age + 1
            bars.loc[cross_position, "macd_golden_cross"] = True
            if dead_cross_after:
                bars.loc[cross_position + 1, "macd_dead_cross"] = True
        if current_day_cross:
            bars.loc[count - 1, "macd_golden_cross"] = True
        return bars

    @staticmethod
    def _history() -> pd.DataFrame:
        count = app.INDICATOR_WARMUP_BARS
        closes = pd.Series(
            [20.0 + index * 0.05 for index in range(count)], dtype="float64"
        )
        previous_close = closes.shift(1)
        return app._normalize_history_frame(
            pd.DataFrame(
                {
                    "date": pd.bdate_range("2025-01-02", periods=count),
                    "open": closes - 0.02,
                    "close": closes,
                    "high": closes + 0.10,
                    "low": closes - 0.10,
                    "volume": 1_000_000.0,
                    "amount": 100_000_000.0,
                    "amplitude": 2.0,
                    "pct_change": closes.div(previous_close)
                    .sub(1.0)
                    .mul(100.0)
                    .fillna(0.0),
                    "turnover": 3.0,
                }
            )
        )

    def test_recent_completed_cross_records_its_date_and_age(self) -> None:
        bars = self._cross_bars(age=3)

        position, age = app._latest_valid_macd_golden_cross(bars)

        self.assertEqual(position, len(bars) - 4)
        self.assertEqual(age, 3)

    def test_current_day_cross_is_excluded_and_completed_dead_cross_invalidates(self) -> None:
        position, age = app._latest_valid_macd_golden_cross(
            self._cross_bars(current_day_cross=True)
        )
        self.assertIsNone(position)
        self.assertIsNone(age)

        position, age = app._latest_valid_macd_golden_cross(
            self._cross_bars(age=3, dead_cross_after=True)
        )
        self.assertIsNone(position)
        self.assertIsNone(age)

    def test_condition_matrix_uses_the_selected_recent_cross_window(self) -> None:
        factors = pd.DataFrame(
            {app.MACD_GOLDEN_CROSS_AGE_COLUMN: [1, 3, 4, None]}
        )
        selected = {"macd_golden_cross": True}

        for target_app in (app, core.strategy_app):
            one_day = target_app.condition_matrix(
                factors,
                selected,
                turnover_range=(5.0, 10.0),
                float_market_cap_range_yi=(50.0, 200.0),
                pct_change_range=(3.0, 5.0),
                amplitude_threshold=3.0,
                macd_golden_cross_lookback_days=1,
            )
            three_days = target_app.condition_matrix(
                factors,
                selected,
                turnover_range=(5.0, 10.0),
                float_market_cap_range_yi=(50.0, 200.0),
                pct_change_range=(3.0, 5.0),
                amplitude_threshold=3.0,
                macd_golden_cross_lookback_days=3,
            )

            self.assertEqual(
                list(three_days.columns),
                [target_app.macd_golden_cross_condition_label(3)],
            )
            self.assertEqual(one_day.iloc[:, 0].tolist(), [True, False, False, False])
            self.assertEqual(
                three_days.iloc[:, 0].tolist(), [True, True, False, False]
            )

    def test_recent_cross_does_not_conflict_with_current_day_macd_state(self) -> None:
        app.validate_selected_conditions(
            {"macd_golden_cross": True, "macd_bearish": True},
            require_all=True,
        )

    def test_backtest_precomputed_path_records_recent_cross_age(self) -> None:
        history = self._history()
        bars = core._precompute_factor_bars(history)
        bars["macd_golden_cross"] = False
        bars["macd_dead_cross"] = False
        cross_position = len(bars) - 4
        bars.loc[cross_position, "macd_golden_cross"] = True

        factors = core._factor_values_from_precomputed_bars(bars, len(bars) - 1)

        self.assertEqual(factors[app.MACD_GOLDEN_CROSS_AGE_COLUMN], 3)
        self.assertEqual(
            factors[app.MACD_GOLDEN_CROSS_DATE_COLUMN],
            history["date"].iloc[cross_position].date().isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
