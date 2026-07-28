from __future__ import annotations

import json
import unittest

import altair as alt
import pandas as pd

from stock_analysis import KdjParameters, build_analysis_frame
from stock_analysis_charts import _prepare_chart_data, build_stock_analysis_chart


REQUIRED_CHART_FIELDS = (
    "date",
    "open",
    "close",
    "high",
    "low",
    "ma5",
    "ma20",
    "macd_dif",
    "macd_dea",
    "macd_histogram",
    "kdj_k",
    "kdj_d",
    "kdj_j",
)


def sample_analysis_frame() -> pd.DataFrame:
    """Create fully warmed-up indicator data without fetching market data."""

    rows = 170
    close = pd.Series([10.0 + index * 0.1 for index in range(rows)], dtype="float64")
    history = pd.DataFrame(
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
    return build_analysis_frame(history, display_bars=30)


def crossing_analysis_frame() -> pd.DataFrame:
    """Create data with one distinct golden-cross date for each chart panel."""

    dates = pd.bdate_range("2024-01-02", periods=5)
    close = pd.Series([10.0, 10.2, 10.4, 10.6, 10.8], dtype="float64")
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.05,
            "close": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "ma5": [1.0, 1.0, 3.0, 3.0, 3.0],
            "ma20": [2.0, 2.0, 2.0, 2.0, 2.0],
            "macd_dif": [-1.0, -1.0, -1.0, 1.0, 1.0],
            "macd_dea": [0.0, 0.0, 0.0, 0.0, 0.0],
            "macd_histogram": [-1.0, -1.0, -1.0, 1.0, 1.0],
            "kdj_k": [10.0, 10.0, 10.0, 10.0, 30.0],
            "kdj_d": [20.0, 20.0, 20.0, 20.0, 20.0],
            "kdj_j": [0.0, 0.0, 0.0, 0.0, 50.0],
        }
    )
    return frame.sample(frac=1, random_state=7).reset_index(drop=True)


def collect_mark_types(value: object) -> list[str]:
    """Collect Vega-Lite mark types from a chart specification recursively."""

    marks: list[str] = []
    if isinstance(value, dict):
        mark = value.get("mark")
        if isinstance(mark, str):
            marks.append(mark)
        elif isinstance(mark, dict) and isinstance(mark.get("type"), str):
            marks.append(mark["type"])
        for nested in value.values():
            marks.extend(collect_mark_types(nested))
    elif isinstance(value, list):
        for nested in value:
            marks.extend(collect_mark_types(nested))
    return marks


def count_legends(value: object) -> int:
    """Count explicit legends so chart series remain distinguishable."""

    if isinstance(value, dict):
        return int(isinstance(value.get("legend"), dict)) + sum(
            count_legends(nested) for nested in value.values()
        )
    if isinstance(value, list):
        return sum(count_legends(nested) for nested in value)
    return 0


def collect_scale_domains(value: object) -> list[list[object]]:
    """Collect categorical scale domains that drive Altair's default legends."""

    domains: list[list[object]] = []
    if isinstance(value, dict):
        scale = value.get("scale")
        if isinstance(scale, dict) and isinstance(scale.get("domain"), list):
            domains.append(scale["domain"])
        for nested in value.values():
            domains.extend(collect_scale_domains(nested))
    elif isinstance(value, list):
        for nested in value:
            domains.extend(collect_scale_domains(nested))
    return domains


class StockAnalysisChartTests(unittest.TestCase):
    def test_chart_has_three_shared_x_axis_panels_with_all_required_fields(self) -> None:
        chart = build_stock_analysis_chart(sample_analysis_frame())
        specification = chart.to_dict()
        specification_text = json.dumps(specification, ensure_ascii=False)

        self.assertIsInstance(chart, alt.VConcatChart)
        self.assertEqual(len(specification["vconcat"]), 3)
        self.assertEqual(
            specification["resolve"]["scale"],
            {"x": "shared", "y": "independent"},
        )
        for field in REQUIRED_CHART_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, specification_text)

    def test_chart_contains_candles_indicator_lines_references_and_legends(self) -> None:
        specification = build_stock_analysis_chart(sample_analysis_frame()).to_dict()
        specification_text = json.dumps(specification, ensure_ascii=False)
        mark_types = collect_mark_types(specification)
        scale_domains = collect_scale_domains(specification)

        self.assertIn("rule", mark_types)
        self.assertIn("bar", mark_types)
        self.assertIn("line", mark_types)
        self.assertGreaterEqual(count_legends(specification), 1)
        self.assertIn(["MA5", "MA20"], scale_domains)
        self.assertIn(["DIF", "DEA"], scale_domains)
        self.assertIn(["K", "D", "J"], scale_domains)
        for label in ("MA5", "MA20", "DIF", "DEA", "KDJ"):
            with self.subTest(label=label):
                self.assertIn(label, specification_text)
        self.assertIn("20.0", specification_text)
        self.assertIn("80.0", specification_text)

    def test_chart_marks_the_trade_dates_of_each_golden_cross(self) -> None:
        prepared = _prepare_chart_data(crossing_analysis_frame())
        specification = build_stock_analysis_chart(crossing_analysis_frame()).to_dict()
        specification_text = json.dumps(specification, ensure_ascii=False)

        expected_signal_dates = {
            "ma5_golden_cross": "2024-01-04",
            "macd_golden_cross": "2024-01-05",
            "kdj_golden_cross": "2024-01-08",
        }
        for field, expected_date in expected_signal_dates.items():
            with self.subTest(field=field):
                actual_dates = prepared.loc[prepared[field], "date"].dt.strftime(
                    "%Y-%m-%d"
                )
                self.assertEqual(actual_dates.tolist(), [expected_date])

        self.assertGreaterEqual(collect_mark_types(specification).count("point"), 3)
        self.assertGreaterEqual(collect_mark_types(specification).count("text"), 3)
        self.assertIn("%m-%d", specification_text)
        for signal_name in ("MA5上穿MA20", "MACD金叉", "KDJ金叉", "发生日期"):
            with self.subTest(signal_name=signal_name):
                self.assertIn(signal_name, specification_text)

    def test_chart_title_reflects_non_default_kdj_parameters(self) -> None:
        parameters = KdjParameters(
            rsv_period=55,
            k_smoothing_period=4,
            d_smoothing_period=5,
        )
        specification = build_stock_analysis_chart(
            sample_analysis_frame(),
            kdj_parameters=parameters,
        ).to_dict()
        specification_text = json.dumps(specification, ensure_ascii=False)

        self.assertIn("KDJ（55, 4, 5）", specification_text)

    def test_chart_specification_is_json_serializable(self) -> None:
        specification = build_stock_analysis_chart(sample_analysis_frame()).to_dict()

        serialized = json.dumps(specification, ensure_ascii=False)

        self.assertIsInstance(serialized, str)
        self.assertGreater(len(serialized), 1_000)

    def test_chart_rejects_frame_missing_a_required_indicator(self) -> None:
        incomplete_frame = sample_analysis_frame().drop(columns=["kdj_j"])

        with self.assertRaisesRegex(ValueError, "kdj_j"):
            build_stock_analysis_chart(incomplete_frame)


if __name__ == "__main__":
    unittest.main()
