from __future__ import annotations

from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from strategy_backtest import backtest_core as core


APP_PATH = Path(__file__).resolve().parents[1] / "strategy_backtest" / "backtest_app.py"


class BacktestAppResultTests(unittest.TestCase):
    def test_legacy_default_ranges_are_migrated_without_overwriting_custom_ranges(
        self,
    ) -> None:
        app = AppTest.from_file(APP_PATH)
        app.run(timeout=60)

        app.session_state["backtest_filter_defaults_version"] = 0
        app.session_state["backtest_filter_rsi_range"] = (45.0, 75.0)
        app.session_state["backtest_filter_turnover_range"] = (5.2, 10.8)
        app.session_state["backtest_filter_volume_ratio_range"] = (1.5, 8.0)
        app.session_state["backtest_filter_pct_change_range"] = (-2.7, 10.1)
        app.session_state["backtest_filter_kdj_healthy_golden_cross_age_range"] = (2, 3)
        app.session_state["backtest_filter_positive_change"] = True
        app.session_state["backtest_filter_pct_change_in_range"] = False
        app.run(timeout=60)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            tuple(float(value) for value in app.slider(key="backtest_filter_rsi_range").value),
            (49.1, 62.6),
        )
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(key="backtest_filter_turnover_range").value
            ),
            (5.4, 10.7),
        )
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(
                    key="backtest_filter_volume_ratio_range"
                ).value
            ),
            (1.8, 3.8),
        )
        self.assertFalse(app.checkbox(key="backtest_filter_positive_change").value)
        self.assertTrue(app.checkbox(key="backtest_filter_pct_change_in_range").value)
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(key="backtest_filter_pct_change_range").value
            ),
            (-2.7, 10.1),
        )
        self.assertEqual(
            tuple(
                int(value)
                for value in app.slider(
                    key="backtest_filter_kdj_healthy_golden_cross_age_range"
                ).value
            ),
            (1, 3),
        )

        app.session_state["backtest_filter_defaults_version"] = 0
        app.session_state["backtest_filter_rsi_range"] = (52.0, 68.0)
        app.session_state["backtest_filter_turnover_range"] = (4.0, 12.0)
        app.run(timeout=60)

        self.assertEqual(
            tuple(float(value) for value in app.slider(key="backtest_filter_rsi_range").value),
            (52.0, 68.0),
        )
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(key="backtest_filter_turnover_range").value
            ),
            (4.0, 12.0),
        )

    def test_kdj_controls_expose_a_configurable_zero_based_age_range(self) -> None:
        app = AppTest.from_file(APP_PATH)
        app.run(timeout=60)

        self.assertEqual(len(app.exception), 0)
        kdj_checkbox = app.checkbox(
            key="backtest_filter_kdj_healthy_golden_cross_3d"
        )
        kdj_age_range = app.slider(
            key="backtest_filter_kdj_healthy_golden_cross_age_range"
        )
        macd_golden_cross = app.checkbox(key="backtest_filter_macd_golden_cross")
        macd_lookback = app.number_input(
            key="backtest_filter_macd_golden_cross_lookback_days"
        )
        self.assertIn("KDJ", kdj_checkbox.label)
        self.assertIn("状态良好", kdj_checkbox.label)
        self.assertTrue(kdj_checkbox.value)
        self.assertEqual(tuple(int(value) for value in kdj_age_range.value), (1, 3))
        self.assertIn("MACD", macd_golden_cross.label)
        self.assertFalse(macd_golden_cross.value)
        self.assertEqual(int(macd_lookback.value), 3)
        self.assertTrue(app.checkbox(key="backtest_filter_rsi_in_range").value)
        self.assertTrue(app.checkbox(key="backtest_filter_macd_bullish").value)
        self.assertTrue(app.checkbox(key="backtest_filter_volume_breakout").value)
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(
                    key="backtest_filter_volume_ratio_range"
                ).value
            ),
            (1.8, 3.8),
        )
        self.assertTrue(app.checkbox(key="backtest_filter_amount_at_least_100m").value)
        self.assertTrue(app.checkbox(key="backtest_filter_turnover_in_range").value)
        self.assertEqual(
            tuple(
                float(value)
                for value in app.slider(key="backtest_filter_turnover_range").value
            ),
            (5.4, 10.7),
        )
        self.assertFalse(app.checkbox(key="backtest_filter_positive_change").value)
        self.assertTrue(app.checkbox(key="backtest_filter_pct_change_in_range").value)
        self.assertTrue(app.checkbox(key="backtest_filter_require_all").value)
        self.assertTrue(app.checkbox(key="backtest_risk_bias_high").value)
        self.assertFalse(app.checkbox(key="backtest_risk_upper_shadow").value)
        self.assertTrue(app.checkbox(key="backtest_risk_resistance_60_day").value)
        self.assertEqual(
            app.multiselect(key="backtest_risk_candlestick_patterns").value,
            ["doji_3d", "inverted_t_doji_3d", "hanging_man_3d"],
        )

    def test_history_failures_do_not_abort_the_backtest(self) -> None:
        daily_results = pd.DataFrame(
            {
                "选股日期": [date(2026, 5, 6)],
                "下一市场交易日": [date(2026, 5, 7)],
                "选中股票代码": ["000001"],
                "选中股票名称": ["甲公司"],
                "是否预测正确": ["正确"],
                "次日真实涨跌幅（%）": [1.0],
                "累计收益率（%）": [1.0],
            }
        )
        history_errors = pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "000003",
                    "股票名称": "丙公司",
                    "选股日期": None,
                    "问题类型": "长历史获取失败",
                    "失败原因": "测试失败",
                }
            ],
            columns=core.HISTORY_ERROR_COLUMNS,
        )

        with (
            patch.object(
                core,
                "collect_full_histories",
                return_value=({}, history_errors, {"历史失败": 1}, "test-cache"),
            ),
            patch.object(
                core,
                "collect_all_factor_rows_by_day",
                return_value=({}, {}, pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS)),
            ) as collect_all_factor_rows_by_day,
            patch.object(
                core,
                "evaluate_strategy",
                return_value=(daily_results, {"总收益率（%）": 1.0}),
            ) as evaluate_strategy,
        ):
            app = AppTest.from_file(APP_PATH)
            app.run(timeout=60)
            macd_factor_checkbox = app.checkbox(
                key="backtest_filter_macd_dea_minus_dif_high"
            )
            macd_factor_range = app.slider(
                key="backtest_filter_macd_dea_minus_dif_range"
            )
            rsi_factor_checkbox = app.checkbox(key="backtest_filter_rsi_in_range")
            rsi_factor_range = app.slider(key="backtest_filter_rsi_range")
            kdj_age_range = app.slider(
                key="backtest_filter_kdj_healthy_golden_cross_age_range"
            )
            macd_lookback = app.number_input(
                key="backtest_filter_macd_golden_cross_lookback_days"
            )
            volume_ratio_range = app.slider(
                key="backtest_filter_volume_ratio_range"
            )
            self.assertFalse(macd_factor_checkbox.value)
            self.assertEqual(
                tuple(float(value) for value in macd_factor_range.value), (0.1, 0.2)
            )
            self.assertTrue(rsi_factor_checkbox.value)
            self.assertEqual(
                tuple(float(value) for value in rsi_factor_range.value), (49.1, 62.6)
            )
            self.assertEqual(
                tuple(int(value) for value in kdj_age_range.value), (1, 3)
            )
            self.assertEqual(int(macd_lookback.value), 3)
            self.assertEqual(
                tuple(float(value) for value in volume_ratio_range.value),
                (1.8, 3.8),
            )
            self.assertEqual(
                app.date_input(key="backtest_date_range").value,
                (date(2025, 10, 29), date(2026, 7, 27)),
            )
            macd_factor_checkbox.set_value(True)
            macd_factor_range.set_value((0.1, 0.25))
            rsi_factor_range.set_value((52.0, 68.0))
            kdj_age_range.set_value((0, 10))
            macd_lookback.set_value(5)
            volume_ratio_range.set_value((1.8, 7.5))
            app.button[3].click()
            app.run(timeout=60)

        self.assertEqual(
            tuple(
                float(value)
                for value in evaluate_strategy.call_args.kwargs[
                    "macd_dea_minus_dif_range"
                ]
            ),
            (0.1, 0.25),
        )
        self.assertEqual(
            tuple(
                float(value)
                for value in evaluate_strategy.call_args.kwargs["rsi_range"]
            ),
            (52.0, 68.0),
        )
        self.assertTrue(evaluate_strategy.call_args.kwargs["selected"]["rsi_in_range"])
        self.assertTrue(
            evaluate_strategy.call_args.kwargs["selected"]["macd_dea_minus_dif_high"]
        )
        self.assertNotIn(
            "macd_dea_minus_dif_high",
            evaluate_strategy.call_args.kwargs["selected_risks"],
        )
        self.assertEqual(
            tuple(
                int(value)
                for value in evaluate_strategy.call_args.kwargs[
                    "kdj_healthy_golden_cross_age_range"
                ]
            ),
            (0, 10),
        )
        self.assertEqual(
            int(evaluate_strategy.call_args.kwargs["macd_golden_cross_lookback_days"]),
            5,
        )
        self.assertEqual(
            tuple(
                float(value)
                for value in evaluate_strategy.call_args.kwargs["volume_ratio_range"]
            ),
            (1.8, 7.5),
        )
        self.assertNotIn("volume_ratio_threshold", evaluate_strategy.call_args.kwargs)
        self.assertNotIn("realized_returns", evaluate_strategy.call_args.kwargs)
        factor_signal_dates = collect_all_factor_rows_by_day.call_args.args[2]
        self.assertEqual(factor_signal_dates[0], date(2025, 10, 29))
        self.assertEqual(factor_signal_dates[-1], date(2026, 7, 24))
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        self.assertTrue(
            any("已自动剔除 1 只" in warning.value for warning in app.warning)
        )
        self.assertEqual(len(app.dataframe), 1)

    def test_return_failure_codes_are_excluded_before_backtest_collection(self) -> None:
        first_day = date(2026, 5, 6)
        second_day = date(2026, 5, 7)
        third_day = date(2026, 5, 8)
        return_data = core.ReturnData(
            signal_dates=(first_day, second_day),
            next_trade_dates={first_day: second_day, second_day: third_day},
            strict_returns={(first_day, "000002"): 1.0},
            failed_return_codes=frozenset({"000001"}),
        )
        companies = pd.DataFrame(
            [
                {"序号": 1, "股票代码": 1, "股票名称": "失败收益"},
                {"序号": 2, "股票代码": "000002", "股票名称": "可用收益"},
            ]
        )
        daily_results = pd.DataFrame(
            {
                "选股日期": [first_day],
                "下一市场交易日": [second_day],
                "选中股票代码": ["000002"],
                "选中股票名称": ["可用收益"],
                "是否预测正确": ["正确"],
                "次日真实涨跌幅（%）": [1.0],
                "累计收益率（%）": [1.0],
            }
        )

        st.cache_data.clear()
        with (
            patch.object(core, "load_strict_next_day_returns", return_value=return_data),
            patch.object(
                core.strategy_app,
                "load_mainboard_companies",
                return_value=companies,
            ),
            patch.object(
                core,
                "collect_full_histories",
                return_value=(
                    {},
                    pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS),
                    {},
                    "test-cache",
                ),
            ) as collect_full_histories,
            patch.object(
                core,
                "collect_all_factor_rows_by_day",
                return_value=(
                    {},
                    {},
                    pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS),
                ),
            ) as collect_all_factor_rows_by_day,
            patch.object(
                core,
                "evaluate_strategy",
                return_value=(daily_results, {"总收益率（%）": 1.0}),
            ) as evaluate_strategy,
        ):
            app = AppTest.from_file(APP_PATH)
            app.run(timeout=60)
            app.date_input(key="backtest_date_range").set_value(
                (date(2026, 5, 7), date(2026, 5, 8))
            )
            run_button = next(button for button in app.button if button.label == "运行回测")
            run_button.click()
            app.run(timeout=60)

        history_codes = collect_full_histories.call_args.args[0]["股票代码"].tolist()
        factor_codes = collect_all_factor_rows_by_day.call_args.args[0]["股票代码"].tolist()
        selected_return_data = evaluate_strategy.call_args.args[0]
        self.assertEqual(history_codes, ["000002"])
        self.assertEqual(factor_codes, ["000002"])
        self.assertEqual(selected_return_data.signal_dates, (second_day,))
        self.assertEqual(selected_return_data.failed_return_codes, frozenset({"000001"}))
        self.assertEqual(
            app.date_input(key="backtest_date_range").value,
            (date(2026, 5, 7), date(2026, 5, 8)),
        )
        self.assertEqual(len(app.exception), 0)

    def test_result_view_only_shows_actual_predictions(self) -> None:
        daily_results = pd.DataFrame(
            {
                "选股日期": [
                    date(2026, 5, 6),
                    pd.Timestamp("2026-05-08"),
                    date(2026, 5, 8),
                    date(2026, 5, 9),
                ],
                "下一市场交易日": [
                    date(2026, 5, 7),
                    date(2026, 5, 11),
                    date(2026, 5, 11),
                    date(2026, 5, 11),
                ],
                "选中股票代码": ["000001", "000002", None, "000004"],
                "选中股票名称": ["甲公司", "乙公司", None, "丁公司"],
                "是否预测正确": ["正确", "错误", "未预测", "不可评估"],
                "次日真实涨跌幅（%）": [1.2, -0.2, None, 0.5],
                "累计收益率（%）": [1.2, 1.0, 1.0, 1.5],
            }
        )
        payload = {
            "summary": {"总收益率（%）": 1.0},
            "history_summary": {"历史失败": 2},
            "daily_results": daily_results,
            "data_problems": pd.DataFrame(),
            "start_date": None,
            "end_date": None,
        }

        app = AppTest.from_file(APP_PATH)
        app.run(timeout=60)
        self.assertEqual(len(app.exception), 0)

        app.session_state["backtest_results"] = payload
        app.run(timeout=60)

        self.assertEqual(len(app.exception), 0)
        metrics = {metric.label: str(metric.value) for metric in app.metric}
        self.assertEqual(metrics["正确率"], "1 / 2")
        self.assertEqual(metrics["未预测天数"], "2")
        self.assertTrue(
            any("已自动剔除 2 只" in warning.value for warning in app.warning)
        )

        self.assertEqual(len(app.dataframe), 1)
        displayed = app.dataframe[0].value
        self.assertEqual(
            displayed.columns.tolist(),
            [
                "预测日期",
                "预测结果",
                "涨跌幅（%）",
                "总收益率（%）",
                "公司名称",
                "股票代码",
            ],
        )
        self.assertEqual(displayed["预测日期"].tolist(), ["2026-05-07", "2026-05-11"])
        self.assertEqual(displayed["预测结果"].tolist(), ["正确", "失败"])
        self.assertEqual(displayed["股票代码"].tolist(), ["000001", "000002"])
        exported_csv = displayed.to_csv(index=False)
        self.assertTrue(exported_csv.startswith("预测日期,预测结果,涨跌幅（%）"))
        self.assertIn("2026-05-07,正确", exported_csv)
        self.assertEqual(len(app.get("download_button")), 1)

        # 页面展示使用副本，内部完整日表仍保留原始状态和未预测记录。
        self.assertEqual(daily_results["是否预测正确"].tolist(), ["正确", "错误", "未预测", "不可评估"])


if __name__ == "__main__":
    unittest.main()
