from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from stock_analysis import DEFAULT_KDJ_PARAMETERS, KdjParameters


APP_PATH = Path(__file__).resolve().parents[1] / "stock_analysis_app.py"


def synthetic_history() -> pd.DataFrame:
    """Return enough valid rows to warm up every chart indicator offline."""

    rows = 260
    close = pd.Series([10.0 + index * 0.05 for index in range(rows)], dtype="float64")
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=rows),
            "open": close - 0.03,
            "close": close,
            "high": close + 0.12,
            "low": close - 0.12,
            "volume": [1_000_000.0] * rows,
            "amount": [100_000_000.0] * rows,
            "amplitude": [2.0] * rows,
            "pct_change": [0.5] * rows,
            "turnover": [3.0] * rows,
        }
    )


class StockAnalysisAppTests(unittest.TestCase):
    def test_submitting_the_form_renders_summary_metrics_and_chart_offline(self) -> None:
        stock_code = "000123"
        display_bars = 120

        with patch(
            "stock_analysis.fetch_adjusted_daily_history",
            return_value=synthetic_history(),
        ) as fetch_history:
            app = AppTest.from_file(APP_PATH)
            app.run(timeout=10)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)
            self.assertEqual(app.number_input("stock_analysis_kdj_n").value, 89)
            self.assertEqual(app.number_input("stock_analysis_kdj_m1").value, 3)
            self.assertEqual(app.number_input("stock_analysis_kdj_m2").value, 3)

            app.text_input[0].set_value(stock_code)
            app.number_input[0].set_value(display_bars)
            app.button[0].click()
            app.run(timeout=10)

        fetch_history.assert_called_once_with(
            stock_code,
            display_bars=display_bars,
            kdj_parameters=DEFAULT_KDJ_PARAMETERS,
        )
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        self.assertEqual(len(app.metric), 4)
        chart_count = len(app.get("vega_lite_chart")) + len(
            app.get("arrow_vega_lite_chart")
        )
        self.assertEqual(chart_count, 1)

    def test_non_default_kdj_form_parameters_reach_loader_and_render_chart(self) -> None:
        stock_code = "600000"
        display_bars = 100
        parameters = KdjParameters(
            rsv_period=55,
            k_smoothing_period=4,
            d_smoothing_period=5,
        )

        with patch(
            "stock_analysis.fetch_adjusted_daily_history",
            return_value=synthetic_history(),
        ) as fetch_history:
            app = AppTest.from_file(APP_PATH)
            app.run(timeout=10)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.error), 0)

            app.text_input[0].set_value(stock_code)
            app.number_input[0].set_value(display_bars)
            app.number_input("stock_analysis_kdj_n").set_value(
                parameters.rsv_period
            )
            app.number_input("stock_analysis_kdj_m1").set_value(
                parameters.k_smoothing_period
            )
            app.number_input("stock_analysis_kdj_m2").set_value(
                parameters.d_smoothing_period
            )
            app.button[0].click()
            app.run(timeout=10)

        fetch_history.assert_called_once_with(
            stock_code,
            display_bars=display_bars,
            kdj_parameters=parameters,
        )
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        chart_count = len(app.get("vega_lite_chart")) + len(
            app.get("arrow_vega_lite_chart")
        )
        self.assertEqual(chart_count, 1)


if __name__ == "__main__":
    unittest.main()
