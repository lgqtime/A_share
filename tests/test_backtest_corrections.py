from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

from strategy_backtest import backtest_core as core


def short_history() -> pd.DataFrame:
    return core.strategy_app._normalize_history_frame(
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-06", "2026-05-07"]),
                "open": [10.0, 20.0],
                "close": [11.0, 21.0],
                "high": [11.2, 21.2],
                "low": [9.8, 19.8],
                "volume": [1_000_000.0, 1_000_000.0],
                "amount": [100_000_000.0, 100_000_000.0],
                "amplitude": [2.0, 2.0],
                "pct_change": [1.0, 90.0],
                "turnover": [5.0, 5.0],
            }
        )
    )


class BacktestReturnCorrectionTests(unittest.TestCase):
    def test_realized_return_uses_next_day_open_to_close(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 90.0},
        )
        outcomes = {
            "000001": core.HistoryOutcome(
                code="000001",
                history=short_history(),
                source="测试",
                from_cache=False,
                cache_token=None,
            )
        }

        realized = core.build_next_day_open_to_close_returns(return_data, outcomes)

        self.assertAlmostEqual(realized[(signal_day, "000001")], 5.0)

    def test_strategy_evaluation_uses_return_workbook_mapping_by_default(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 90.0},
        )
        factors = {
            signal_day: [
                {
                    "序号": 1,
                    "股票代码": "000001",
                    "股票名称": "测试股",
                    "所属行业": "测试行业",
                    "数据日期": signal_day.isoformat(),
                    "数据来源": "测试",
                    "当日成交额": 100_000_000.0,
                }
            ]
        }

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 1}},
            selected={"amount_at_least_100m": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            require_all=True,
        )

        self.assertAlmostEqual(daily.iloc[0]["次日真实涨跌幅（%）"], 90.0)
        self.assertAlmostEqual(summary["总收益率（%）"], 90.0)


class BacktestSourceCorrectionTests(unittest.TestCase):
    def test_history_fetch_uses_eastmoney_before_tencent(self) -> None:
        history = short_history()
        with (
            patch.object(core, "_fetch_eastmoney_full_history", return_value=history) as eastmoney,
            patch.object(core, "_fetch_tencent_full_history") as tencent,
        ):
            outcome = core._load_one_history(
                "000001",
                cache_key="test",
                first_signal_date=date(2026, 5, 6),
                end_date=date(2026, 5, 7),
                cache_hours=0.0,
                force_refresh=True,
                limiter=core.strategy_app.RequestRateLimiter(0.0),
                timeout_seconds=1.0,
            )

        eastmoney.assert_called_once()
        tencent.assert_not_called()
        self.assertIn("东方财富", outcome.source or "")


if __name__ == "__main__":
    unittest.main()
