from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

import szse_quant_app as root_strategy_app
from strategy_backtest import backtest_core as core
from strategy_backtest import rolling_parameter_optimizer as optimizer
from strategy_backtest import szse_quant_app as strategy_app


DEFAULT_SETTINGS = {
    "szse_quant_filter_rsi_range": (49.1, 62.6),
    "szse_quant_filter_turnover_range": (5.4, 10.7),
    "szse_quant_filter_volume_ratio_range": (1.8, 3.8),
    "szse_quant_filter_pct_change_range": (-2.7, 10.1),
    "szse_quant_filter_macd_dea_minus_dif_range": (0.1, 0.2),
    "szse_quant_filter_kdj_healthy_golden_cross_age_range": (1, 3),
}


def _settings_with_rsi(lower: float, upper: float) -> dict[str, tuple[float, float]]:
    settings = dict(DEFAULT_SETTINGS)
    settings["szse_quant_filter_rsi_range"] = (lower, upper)
    return settings


class PreviousSettingsTests(unittest.TestCase):
    def _write_report(
        self,
        path: Path,
        settings: dict[str, tuple[float, float]],
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "run_date": "2026-07-22",
                    "best_settings": {
                        key: list(value) for key, value in settings.items()
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_explicit_starter_precedes_newest_prior_dated_report(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            self._write_report(
                output_dir / "rolling_parameter_optimization_2026-07-24.json",
                _settings_with_rsi(45.0, 70.0),
            )
            self._write_report(
                output_dir / "rolling_parameter_optimization_2026-07-26.json",
                _settings_with_rsi(46.0, 69.0),
            )
            # A future run must never leak into an earlier daily update.
            self._write_report(
                output_dir / "rolling_parameter_optimization_2026-07-28.json",
                _settings_with_rsi(47.0, 68.0),
            )
            starter_path = output_dir / "manual_starter.json"
            self._write_report(starter_path, _settings_with_rsi(48.0, 67.0))

            loaded_from_history = optimizer.load_previous_settings(
                output_dir,
                DEFAULT_SETTINGS,
                as_of_date=date(2026, 7, 27),
            )
            loaded_from_starter = optimizer.load_previous_settings(
                output_dir,
                DEFAULT_SETTINGS,
                starter_json=starter_path,
                as_of_date=date(2026, 7, 27),
            )

        self.assertEqual(
            loaded_from_history["szse_quant_filter_rsi_range"], (46.0, 69.0)
        )
        self.assertEqual(
            loaded_from_starter["szse_quant_filter_rsi_range"], (48.0, 67.0)
        )
        self.assertEqual(
            loaded_from_starter[
                "szse_quant_filter_kdj_healthy_golden_cross_age_range"
            ],
            (1, 3),
        )

    def test_defaults_are_returned_when_no_prior_report_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            loaded = optimizer.load_previous_settings(
                Path(temporary_directory),
                DEFAULT_SETTINGS,
                as_of_date=date(2026, 7, 27),
            )

        self.assertEqual(loaded, DEFAULT_SETTINGS)
        self.assertIsNot(loaded, DEFAULT_SETTINGS)

    def test_current_parameter_file_is_not_used_as_a_historical_starter(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            self._write_report(
                output_dir / optimizer.CURRENT_PARAMETER_FILENAME,
                _settings_with_rsi(45.0, 70.0),
            )
            loaded = optimizer.load_previous_settings(
                output_dir,
                DEFAULT_SETTINGS,
                as_of_date=date(2026, 7, 27),
            )

        self.assertEqual(loaded, DEFAULT_SETTINGS)


class RecentSignalDateTests(unittest.TestCase):
    def test_optimizer_and_backtest_core_use_the_production_strategy_module(self) -> None:
        root_path = Path(root_strategy_app.__file__).resolve()
        self.assertEqual(Path(optimizer.strategy_app.__file__).resolve(), root_path)
        self.assertEqual(Path(core.strategy_app.__file__).resolve(), root_path)
        self.assertIs(optimizer.strategy_app, core.strategy_app)

    def test_runtime_strategy_ignores_a_legacy_top_level_module_name_collision(
        self,
    ) -> None:
        project_dir = Path(__file__).resolve().parents[1]
        legacy_path = project_dir / "strategy_backtest" / "szse_quant_app.py"
        root_path = project_dir / "szse_quant_app.py"
        script = f"""
import importlib.util
from pathlib import Path
import sys

legacy_path = Path({str(legacy_path)!r})
root_path = Path({str(root_path)!r}).resolve()
legacy_spec = importlib.util.spec_from_file_location("szse_quant_app", legacy_path)
assert legacy_spec is not None and legacy_spec.loader is not None
legacy_module = importlib.util.module_from_spec(legacy_spec)
sys.modules["szse_quant_app"] = legacy_module
legacy_spec.loader.exec_module(legacy_module)

from strategy_backtest import runtime_strategy

resolved_path = Path(runtime_strategy.strategy_app.__file__).resolve()
assert resolved_path == root_path, (
    f"runtime strategy loaded {{resolved_path}}, expected {{root_path}}"
)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"subprocess failed:\\nstdout:\\n{completed.stdout}\\nstderr:\\n{completed.stderr}",
        )

    def test_default_lookback_is_thirty_trading_days(self) -> None:
        self.assertEqual(optimizer.DEFAULT_LOOKBACK_DAYS, 30)
        self.assertEqual(optimizer.parse_args([]).lookback_days, 30)

    def test_selects_only_the_requested_count_of_recent_verifiable_signal_days(self) -> None:
        first = date(2026, 7, 20)
        second = date(2026, 7, 21)
        third = date(2026, 7, 22)
        fourth = date(2026, 7, 23)
        fifth = date(2026, 7, 24)
        future = date(2026, 7, 28)
        return_data = core.ReturnData(
            signal_dates=(first, second, third, fourth, fifth, future),
            next_trade_dates={
                first: second,
                second: third,
                third: fourth,
                fourth: fifth,
                fifth: date(2026, 7, 27),
                future: date(2026, 7, 29),
            },
            strict_returns={
                (first, "000001"): 1.0,
                (second, "000001"): -1.0,
                (third, "000001"): 2.0,
                (fourth, "000001"): 0.5,
                (fifth, "000001"): 1.5,
                (future, "000001"): -2.0,
            },
            failed_return_codes=frozenset({"000002"}),
        )

        selected = optimizer.select_recent_signal_dates(
            return_data,
            as_of_date=date(2026, 7, 27),
            lookback_days=3,
        )

        self.assertEqual(selected.signal_dates, (third, fourth, fifth))
        self.assertEqual(
            selected.next_trade_dates,
            {third: fourth, fourth: fifth, fifth: date(2026, 7, 27)},
        )
        self.assertEqual(
            set(selected.strict_returns),
            {(third, "000001"), (fourth, "000001"), (fifth, "000001")},
        )
        self.assertEqual(selected.failed_return_codes, frozenset({"000002"}))


class BestResultTests(unittest.TestCase):
    def _result(
        self,
        settings: dict[str, tuple[float, float]],
    ) -> optimizer.OptimizationResult:
        return optimizer.OptimizationResult(
            settings=settings,
            total_return_pct=12.5,
            prediction_days=8,
            correct_days=5,
            accuracy_pct=62.5,
        )

    def test_exact_metric_ties_have_a_stable_settings_order(self) -> None:
        lexically_first = self._result(_settings_with_rsi(45.0, 70.0))
        lexically_second = self._result(_settings_with_rsi(48.0, 65.0))

        winner_in_forward_order = optimizer.best_result(
            [lexically_second, lexically_first]
        )
        winner_in_reverse_order = optimizer.best_result(
            [lexically_first, lexically_second]
        )

        self.assertEqual(winner_in_forward_order, lexically_first)
        self.assertEqual(winner_in_reverse_order, lexically_first)


class ResultPersistenceTests(unittest.TestCase):
    def test_persists_dated_parameter_and_full_backtest_json_reports(self) -> None:
        result = optimizer.OptimizationResult(
            settings=_settings_with_rsi(47.5, 61.6),
            total_return_pct=23.4,
            prediction_days=9,
            correct_days=6,
            accuracy_pct=66.6666666667,
        )
        daily_results = pd.DataFrame(
            {
                "signal_date": [date(2026, 7, 27)],
                "selected_code": ["000001"],
                "next_day_return_pct": [2.5],
            }
        )
        trace = [
            {
                "pass": 1,
                "parameter": "rsi",
                "before": (49.1, 62.6),
                "after": (47.5, 61.6),
                "confirmed_result": {"total_return_pct": result.total_return_pct},
            }
        ]
        data_problems = pd.DataFrame(
            {
                "股票代码": ["000002"],
                "选股日期": [date(2026, 7, 27)],
                "失败原因": ["测试数据问题"],
            }
        )

        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            json_path, report_path = optimizer.persist_optimization_result(
                result,
                output_dir,
                as_of_date=date(2026, 7, 27),
                daily_results=daily_results,
                trace=trace,
                metadata={"data_window": {"lookback_signal_days": 20}},
                data_problems=data_problems,
            )
            self.assertTrue(json_path.is_file())
            self.assertTrue(report_path.is_file())
            current_path = output_dir / optimizer.CURRENT_PARAMETER_FILENAME
            self.assertTrue(current_path.is_file())
            self.assertFalse(
                (output_dir / "rolling_parameter_backtest_2026-07-27.xlsx").exists()
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            current_payload = json.loads(current_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertIn("2026-07-27", json_path.name)
        self.assertIn("2026-07-27", report_path.name)
        self.assertEqual(report_path.suffix, ".json")
        self.assertEqual(payload["run_date"], "2026-07-27")
        self.assertEqual(current_payload, payload)
        self.assertEqual(
            payload["best_settings"]["szse_quant_filter_rsi_range"], [47.5, 61.6]
        )
        self.assertEqual(report["report_type"], optimizer.BACKTEST_REPORT_TYPE)
        self.assertEqual(
            report["worksheet_order"], list(optimizer.BACKTEST_WORKSHEET_NAMES)
        )
        self.assertEqual(
            set(report["worksheets"]), set(optimizer.BACKTEST_WORKSHEET_NAMES)
        )
        self.assertEqual(
            report["worksheets"]["每日回测"]["rows"][0]["signal_date"],
            "2026-07-27",
        )
        self.assertEqual(
            report["worksheets"]["优化路径"]["rows"][0]["before"], [49.1, 62.6]
        )
        self.assertEqual(
            report["worksheets"]["优化路径"]["rows"][0]["confirmed_result"][
                "total_return_pct"
            ],
            23.4,
        )
        self.assertEqual(
            report["worksheets"]["数据问题"]["rows"][0]["选股日期"],
            "2026-07-27",
        )
        summary_rows = report["worksheets"]["优化汇总"]["rows"]
        self.assertEqual(
            next(row["数值"] for row in summary_rows if row["项目"] == "data_window"),
            {"lookback_signal_days": 20},
        )

    def test_converts_legacy_xlsx_report_without_losing_worksheet_data(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            xlsx_path = output_dir / "rolling_parameter_backtest_2026-07-28.xlsx"
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [{"项目": "data_window", "数值": '{"lookback_signal_days": 20}'}]
                ).to_excel(writer, sheet_name="优化汇总", index=False)
                pd.DataFrame(
                    [{"参数来源": "最优参数", "参数键": "rsi", "最小值": 49.1, "最大值": 62.6}]
                ).to_excel(writer, sheet_name="参数对比", index=False)
                pd.DataFrame(
                    [{"选股日期": date(2026, 7, 27), "选中股票代码": "000001"}]
                ).to_excel(writer, sheet_name="每日回测", index=False)
                pd.DataFrame(
                    [
                        {
                            "before": "[1, 3]",
                            "after": "[2, 3]",
                            "confirmed_result": '{"total_return_pct": 2.5}',
                        }
                    ]
                ).to_excel(writer, sheet_name="优化路径", index=False)
                pd.DataFrame(
                    [{"股票代码": "000002", "失败原因": "测试数据问题"}]
                ).to_excel(writer, sheet_name="数据问题", index=False)

            report_path = optimizer.convert_legacy_backtest_report(xlsx_path)
            self.assertTrue(xlsx_path.is_file())
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report_path.name, "rolling_parameter_backtest_2026-07-28.json")
        self.assertEqual(report["run_date"], "2026-07-28")
        self.assertEqual(report["source_workbook"], xlsx_path.name)
        self.assertEqual(
            report["worksheets"]["优化汇总"]["rows"][0]["数值"],
            {"lookback_signal_days": 20},
        )
        self.assertEqual(
            report["worksheets"]["优化路径"]["rows"][0]["confirmed_result"],
            {"total_return_pct": 2.5},
        )
        self.assertEqual(
            report["worksheets"]["每日回测"]["rows"][0]["选中股票代码"],
            "000001",
        )


class CoordinateSearchTests(unittest.TestCase):
    def test_search_confirms_candidates_through_the_canonical_backtest_path(self) -> None:
        first = date(2026, 7, 20)
        second = date(2026, 7, 21)
        third = date(2026, 7, 22)
        return_data = core.ReturnData(
            signal_dates=(first, second, third),
            next_trade_dates={
                first: second,
                second: third,
                third: date(2026, 7, 23),
            },
            strict_returns={
                (first, "000001"): 1.0,
                (second, "000001"): 2.0,
                (third, "000001"): -0.5,
            },
        )
        settings = strategy_app.default_screening_settings()
        selected = {
            key: bool(settings[f"szse_quant_filter_{key}"])
            for key in strategy_app.SCORING_INDICATOR_KEYS
        }
        selected_risks = {
            "bias_high": True,
            "upper_shadow": False,
            "resistance_60_day": True,
            **{key: False for key in strategy_app.CANDLESTICK_RISK_PATTERN_KEYS},
        }

        def factor_row(signal_day: date, pct_change: float) -> dict[str, object]:
            return {
                "股票代码": "000001",
                "股票名称": "测试公司",
                "所属行业": "测试行业",
                "数据日期": signal_day.isoformat(),
                "数据来源": "测试",
                "站上MA5": True,
                "站上MA20": True,
                "RSI14": 50.0,
                "MACD多头": True,
                strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: 1,
                "量比": 2.0,
                "当日成交额": 120_000_000.0,
                "换手率": 6.0,
                "当日涨跌幅": pct_change,
                "BIAS20": 0.0,
                "触及60日高点压力": False,
                "收盘日内位置（%）": 80.0,
            }

        factors_by_day = {
            first: [factor_row(first, 0.0)],
            second: [factor_row(second, 0.5)],
            third: [factor_row(third, 1.0)],
        }
        day_stats = {day: {} for day in return_data.signal_dates}
        (
            _,
            baseline,
            final,
            daily_results,
            _,
            trace,
        ) = optimizer._coordinate_search(
            return_data,
            factors_by_day,
            day_stats,
            settings,
            selected,
            selected_risks,
            max_passes=1,
            confirm_top=2,
            batch_size=64,
            minimum_prediction_days=1,
        )

        self.assertEqual(baseline.prediction_days, 3)
        self.assertGreaterEqual(final.prediction_days, 1)
        self.assertEqual(len(daily_results), 3)
        self.assertTrue(trace)
        self.assertEqual(
            final.settings["szse_quant_filter_kdj_healthy_golden_cross_age_range"],
            (1, 3),
        )
