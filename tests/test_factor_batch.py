"""批量因子计算与页面单窗口实现的等价性测试。"""

from __future__ import annotations

from datetime import date
from unittest import TestCase

import pandas as pd

from strategy_backtest import backtest_core as core
from strategy_backtest import factor_batch


class BatchFactorEquivalenceTests(TestCase):
    @staticmethod
    def _history(seed: float) -> pd.DataFrame:
        count = core.strategy_app.INDICATOR_WARMUP_BARS + 65
        positions = pd.Series(range(count), dtype="float64")
        close = (
            seed
            + positions * 0.13
            + (positions.mod(11) - 5.0) * 0.31
            + (positions.mod(7) - 3.0) * 0.17
        )
        opening = close * (1.0 + (positions.mod(5) - 2.0) * 0.002)
        high = pd.concat([opening, close], axis=1).max(axis=1) + 0.8 + positions.mod(3) * 0.1
        low = pd.concat([opening, close], axis=1).min(axis=1) - 0.7 - positions.mod(4) * 0.1
        previous_close = close.shift(1)
        return core.strategy_app._normalize_history_frame(
            pd.DataFrame(
                {
                    "date": pd.bdate_range("2025-10-01", periods=count),
                    "open": opening,
                    "close": close,
                    "high": high,
                    "low": low,
                    "volume": 1_000_000.0 + positions * 12_345.0,
                    "amount": 100_000_000.0 + positions * 2_500_000.0,
                    "amplitude": (high - low).div(previous_close).mul(100.0).fillna(0.0),
                    "pct_change": close.div(previous_close).sub(1.0).mul(100.0).fillna(0.0),
                    "turnover": 2.0 + positions.mod(13) * 0.4,
                }
            )
        )

    def test_fast_batch_matches_page_for_every_returned_field(self) -> None:
        for history in (self._history(25.0), self._history(80.0)):
            warmup = core.strategy_app.INDICATOR_WARMUP_BARS
            signal_dates: tuple[date, ...] = tuple(
                pd.Timestamp(history["date"].iloc[position]).date()
                for position in (
                    warmup - 1,
                    warmup,
                    warmup + 17,
                    warmup + 39,
                    warmup + 64,
                )
            )
            actual_by_date = factor_batch.calculate_factor_values_for_dates(
                history,
                signal_dates,
                strategy_app=core.strategy_app,
            )

            self.assertEqual(
                set(actual_by_date),
                {signal_day.isoformat() for signal_day in signal_dates},
            )
            for signal_day in signal_dates:
                position = int(
                    history["date"].searchsorted(pd.Timestamp(signal_day), side="right")
                ) - 1
                window = history.iloc[position - warmup + 1 : position + 1].reset_index(drop=True)
                expected = core.strategy_app._calculate_factors_from_normalized_history(
                    window,
                    as_of_date=signal_day,
                )
                self.assertEqual(actual_by_date[signal_day.isoformat()], expected)
