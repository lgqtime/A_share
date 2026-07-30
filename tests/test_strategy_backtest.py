from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from strategy_backtest import backtest_core as core
from strategy_backtest import run_backtest


def complete_factor_row(code: str, sequence: int, signal_day: date, **overrides: object) -> dict[str, object]:
    """构造恰好满足固定策略且不命中风险规则的一行因子。"""

    row: dict[str, object] = {
        "序号": sequence,
        "股票代码": code,
        "股票名称": f"测试{code}",
        "所属行业": "测试行业",
        "数据来源": "测试来源",
        "数据日期": signal_day.isoformat(),
        "站上MA5": True,
        "MA5高于MA20": True,
        "MACD多头": True,
        "MACD_DIF": 0.0,
        "MACD_DEA": 0.0,
        core.strategy_app.MACD_GOLDEN_CROSS_DATE_COLUMN: signal_day.isoformat(),
        core.strategy_app.MACD_GOLDEN_CROSS_AGE_COLUMN: 1,
        core.strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: True,
        core.strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN: signal_day.isoformat(),
        core.strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: 2,
        "当日成交额": 100_000_000.0,
        "估算流通市值（亿元）": 50.0,
        "BIAS20": 0.0,
        "上影线比例": 0.0,
        "当日涨跌幅": 0.0,
        "触及60日高点压力": False,
    }
    row.update(
        {
            column: False
            for column in core.strategy_app.CANDLESTICK_RISK_FACTOR_COLUMNS.values()
        }
    )
    row.update(overrides)
    return row


class StrictReturnTests(TestCase):
    def test_only_adjacent_market_day_return_is_retained(self) -> None:
        detail = pd.DataFrame(
            {
                "股票代码": ["000001", "000001", "000002", "000001"],
                "交易日期": ["2026-05-06", "2026-05-07", "2026-05-08", "2026-05-08"],
                "前一交易日": ["2026-05-05", "2026-05-06", "2026-05-06", "2026-05-07"],
                "当日涨跌幅（%）": [1.0, 2.0, 9.0, -1.0],
            }
        )
        with TemporaryDirectory() as temporary_directory:
            workbook = Path(temporary_directory) / "returns.xlsx"
            with pd.ExcelWriter(workbook) as writer:
                detail.to_excel(
                    writer,
                    sheet_name=core.RETURN_DETAIL_SHEET_NAME,
                    index=False,
                )
            return_data = core.load_strict_next_day_returns(workbook)

        self.assertEqual(
            return_data.signal_dates,
            (date(2026, 5, 6), date(2026, 5, 7)),
        )
        self.assertEqual(
            return_data.next_trade_dates[date(2026, 5, 6)], date(2026, 5, 7)
        )
        self.assertEqual(return_data.strict_returns[(date(2026, 5, 6), "000001")], 2.0)
        self.assertEqual(return_data.strict_returns[(date(2026, 5, 7), "000001")], -1.0)
        self.assertNotIn((date(2026, 5, 6), "000002"), return_data.strict_returns)
        self.assertEqual(return_data.failed_return_codes, frozenset())

    def test_failure_detail_codes_are_normalized_and_retained(self) -> None:
        detail = pd.DataFrame(
            {
                "股票代码": ["000001", "000001"],
                "交易日期": ["2026-05-06", "2026-05-07"],
                "前一交易日": ["2026-05-05", "2026-05-06"],
                "当日涨跌幅（%）": [1.0, 2.0],
            }
        )
        failures = pd.DataFrame(
            {
                "股票代码": [1237, "000002.0", "not-a-code", None],
                "失败原因": ["失败"] * 4,
            }
        )
        with TemporaryDirectory() as temporary_directory:
            workbook = Path(temporary_directory) / "returns.xlsx"
            with pd.ExcelWriter(workbook) as writer:
                detail.to_excel(
                    writer,
                    sheet_name=core.RETURN_DETAIL_SHEET_NAME,
                    index=False,
                )
                failures.to_excel(
                    writer,
                    sheet_name=core.RETURN_FAILURE_SHEET_NAME,
                    index=False,
                )
            return_data = core.load_strict_next_day_returns(workbook)

        self.assertEqual(
            return_data.failed_return_codes,
            frozenset({"001237", "000002"}),
        )


class StrategyEvaluationTests(TestCase):
    def test_fixed_settings_only_enable_requested_conditions(self) -> None:
        selected, risks = core.fixed_strategy_settings()

        self.assertEqual(core.strategy_app.maximum_score(selected), 6.5)
        self.assertEqual(
            {key for key, value in selected.items() if value},
            {
                "above_ma5",
                "ma5_above_ma20",
                "macd_bullish",
                "kdj_healthy_golden_cross_3d",
                "amount_at_least_100m",
                "float_market_cap_in_range",
            },
        )
        self.assertTrue(selected["kdj_healthy_golden_cross_3d"])
        self.assertFalse(selected.get("macd_dea_minus_dif_high", False))
        self.assertTrue(all(risks.values()))
        self.assertNotIn("macd_dea_minus_dif_high", risks)

    def test_no_signal_day_stays_in_denominator_and_tied_score_has_stable_fallback(self) -> None:
        first_day = date(2026, 5, 6)
        second_day = date(2026, 5, 7)
        third_day = date(2026, 5, 8)
        return_data = core.ReturnData(
            signal_dates=(first_day, second_day),
            next_trade_dates={first_day: second_day, second_day: third_day},
            strict_returns={(first_day, "000001"): 1.5},
        )
        factors = {
            first_day: [
                complete_factor_row("000002", 2, first_day),
                complete_factor_row("000001", 1, first_day),
            ],
            second_day: [],
        }
        day_stats = {
            first_day: {"精算因子行数": 2},
            second_day: {"精算因子行数": 0},
        }

        daily, summary = core.evaluate_fixed_strategy(return_data, factors, day_stats)

        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "是否预测正确"], "正确")
        self.assertEqual(daily.loc[1, "状态"], "无信号")
        self.assertEqual(daily.loc[1, "当日组合收益率（%）"], 0.0)
        self.assertEqual(summary["预测正确天数"], 1)
        self.assertEqual(summary["预测天数"], 1)
        self.assertEqual(summary["未预测天数"], 1)
        self.assertEqual(summary["总天数"], 2)
        self.assertAlmostEqual(float(summary["总收益率（%）"]), 1.5)
        self.assertEqual(summary["最终统计"], "1/1，1.50%")

    def test_risk_rules_exclude_candidates(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 5.0},
        )
        factors = {
            signal_day: [complete_factor_row("000001", 1, signal_day, BIAS20=10.1)]
        }

        daily, summary = core.evaluate_fixed_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 1}},
        )

        self.assertEqual(daily.loc[0, "完整满足指标数"], 1)
        self.assertEqual(daily.loc[0, "风险剔除数"], 1)
        self.assertEqual(daily.loc[0, "最终候选数"], 0)
        self.assertIsNone(daily.loc[0, "选中股票代码"])
        self.assertEqual(daily.loc[0, "状态"], "无信号")
        self.assertEqual(summary["预测正确天数"], 0)
        self.assertAlmostEqual(summary["总收益率（%）"], 0.0)

    def test_data_incomplete_day_is_unpredicted_and_keeps_zero_return(self) -> None:
        first_day = date(2026, 5, 6)
        second_day = date(2026, 5, 7)
        third_day = date(2026, 5, 8)
        return_data = core.ReturnData(
            signal_dates=(first_day, second_day),
            next_trade_dates={first_day: second_day, second_day: third_day},
            strict_returns={(first_day, "000001"): 2.0, (second_day, "000001"): -9.0},
        )
        factors = {
            first_day: [complete_factor_row("000001", 1, first_day)],
            second_day: [],
        }
        day_stats = {
            first_day: {"精算因子行数": 1},
            # 当天所有精算均失败时，不形成可评估预测。
            second_day: {"精算因子行数": 0, "因子计算失败数": 1},
        }

        daily, summary = core.evaluate_fixed_strategy(return_data, factors, day_stats)

        self.assertEqual(daily.loc[1, "状态"], "数据不完整")
        self.assertEqual(daily.loc[1, "是否预测正确"], "未预测")
        self.assertEqual(daily.loc[1, "当日组合收益率（%）"], 0.0)
        self.assertAlmostEqual(float(daily.loc[1, "累计收益率（%）"]), 2.0)
        self.assertEqual(summary["预测正确天数"], 1)
        self.assertEqual(summary["预测天数"], 1)
        self.assertEqual(summary["未预测天数"], 1)
        self.assertEqual(summary["总天数"], 2)
        self.assertEqual(summary["数据不完整天数"], 1)
        self.assertAlmostEqual(float(summary["总收益率（%）"]), 2.0)
        self.assertEqual(summary["最终统计"], "1/1，2.00%")

    def test_long_history_failure_does_not_turn_all_days_into_zero_over_zero(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 2.0},
        )

        daily, summary = core.evaluate_fixed_strategy(
            return_data,
            {signal_day: [complete_factor_row("000001", 1, signal_day)]},
            {signal_day: {"精算因子行数": 1, "长历史不可用股票数": 1}},
        )

        self.assertEqual(daily.loc[0, "状态"], "预测正确")
        self.assertEqual(summary["总天数"], 1)
        self.assertEqual(summary["最终统计"], "1/1，2.00%")

    def test_prediction_statistics_only_count_rows_with_strict_next_day_returns(self) -> None:
        days = tuple(date(2026, 5, 6 + offset) for offset in range(6))
        return_data = core.ReturnData(
            signal_dates=days[:5],
            next_trade_dates=dict(zip(days[:5], days[1:], strict=True)),
            strict_returns={
                (days[0], "000001"): 2.0,
                (days[1], "000001"): -1.0,
            },
        )
        factors = {
            days[0]: [complete_factor_row("000001", 1, days[0])],
            days[1]: [complete_factor_row("000001", 1, days[1])],
            days[2]: [],
            # 选中但缺少严格次日收益，不形成预测。
            days[3]: [complete_factor_row("000001", 1, days[3])],
            # 所有因子均失败时保留数据不完整状态。
            days[4]: [],
        }
        day_stats = {
            days[0]: {"精算因子行数": 1},
            days[1]: {"精算因子行数": 1},
            days[2]: {"精算因子行数": 0},
            days[3]: {"精算因子行数": 1},
            days[4]: {"精算因子行数": 0, "因子计算失败数": 1},
        }

        daily, summary = core.evaluate_fixed_strategy(return_data, factors, day_stats)

        self.assertEqual(summary["预测正确天数"], 1)
        self.assertEqual(summary["预测天数"], 2)
        self.assertEqual(summary["失败预测天数"], 1)
        self.assertEqual(summary["未预测天数"], 3)
        self.assertEqual(summary["总天数"], 5)
        self.assertAlmostEqual(float(summary["预测正确率（%）"]), 50.0)
        self.assertAlmostEqual(float(summary["总收益率（%）"]), 0.98)
        self.assertEqual(summary["最终统计"], "1/2，0.98%")
        self.assertEqual(
            daily["是否预测正确"].tolist(),
            ["正确", "错误", "未预测", "未预测", "未预测"],
        )
        self.assertEqual(daily.loc[3, "状态"], "次日收益缺失")
        self.assertEqual(daily.loc[4, "状态"], "数据不完整")
        self.assertEqual(daily["当日组合收益率（%）"].tolist(), [2.0, -1.0, 0.0, 0.0, 0.0])

    def test_custom_indicator_settings_drive_evaluation(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 1.0},
        )
        # 该行故意不满足固定策略的“站上MA5”，只满足自定义的 RSI 条件。
        factors = {
            signal_day: [
                complete_factor_row(
                    "000001", 1, signal_day, **{"站上MA5": False, "RSI14": 55.0}
                )
            ]
        }

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 1}},
            selected={"rsi_in_range": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            rsi_range=(50.0, 100.0),
            require_all=True,
        )

        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "选中得分"], 1.0)
        self.assertEqual(summary["策略满分"], 1.0)
        self.assertEqual(summary["最终统计"], "1/1，1.00%")

    def test_interactive_evaluation_keeps_available_stocks_after_factor_failure(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 2.0},
        )

        daily, summary = core.evaluate_strategy(
            return_data,
            {
                signal_day: [
                    complete_factor_row(
                        "000001", 1, signal_day, **{"RSI14": 55.0}
                    )
                ]
            },
            {signal_day: {"精算因子行数": 1, "因子计算失败数": 1}},
            selected={"rsi_in_range": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            rsi_range=(50.0, 100.0),
            require_all=True,
        )

        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "状态"], "预测正确")
        self.assertEqual(daily.loc[0, "数据覆盖状态"], "完整")
        self.assertEqual(summary["预测天数"], 1)
        self.assertEqual(summary["数据不完整天数"], 0)

    def test_custom_volume_ratio_threshold_drives_evaluation(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={
                (signal_day, "000001"): 5.0,
                (signal_day, "000002"): 1.0,
            },
        )
        factors = {
            signal_day: [
                complete_factor_row("000001", 1, signal_day, **{"量比": 1.5}),
                complete_factor_row("000002", 2, signal_day, **{"量比": 1.6}),
            ]
        }

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 2}},
            selected={"volume_breakout": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            volume_ratio_threshold=1.5,
            require_all=True,
        )

        self.assertEqual(daily.loc[0, "完整满足指标数"], 1)
        self.assertEqual(daily.loc[0, "选中股票代码"], "000002")
        self.assertEqual(daily.loc[0, "满足条件"], "放量（量比>1.5）")
        self.assertEqual(summary["最终统计"], "1/1，1.00%")

    def test_volume_ratio_range_is_inclusive_and_limits_the_upper_bound(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={
                (signal_day, "000001"): 5.0,
                (signal_day, "000002"): 1.0,
                (signal_day, "000003"): 9.0,
            },
        )
        factors = {
            signal_day: [
                complete_factor_row("000001", 1, signal_day, **{"量比": 1.5}),
                complete_factor_row("000002", 2, signal_day, **{"量比": 8.0}),
                complete_factor_row("000003", 3, signal_day, **{"量比": 8.1}),
            ]
        }

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 3}},
            selected={"volume_breakout": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            volume_ratio_range=(1.5, 8.0),
            require_all=True,
        )

        self.assertEqual(daily.loc[0, "完整满足指标数"], 2)
        self.assertEqual(daily.loc[0, "选中股票代码"], "000002")
        self.assertEqual(daily.loc[0, "满足条件"], "放量（量比1.5-8）")
        self.assertEqual(summary["最终统计"], "1/1，1.00%")

    def test_volume_ratio_range_rejects_values_outside_zero_to_fifteen(self) -> None:
        factors = pd.DataFrame({"量比": [1.5]})
        for volume_ratio_range in ((-0.1, 8.0), (1.5, 15.1), (8.0, 1.5)):
            with self.subTest(volume_ratio_range=volume_ratio_range):
                with self.assertRaisesRegex(ValueError, "量比区间"):
                    core.strategy_app.condition_matrix(
                        factors,
                        {"volume_breakout": True},
                        turnover_range=(5.0, 10.0),
                        float_market_cap_range_yi=(50.0, 200.0),
                        pct_change_range=(3.0, 5.0),
                        amplitude_threshold=3.0,
                        volume_ratio_range=volume_ratio_range,
                    )

    def test_require_all_changes_partial_match_eligibility(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 2.0},
        )
        factors = {
            signal_day: [
                complete_factor_row("000001", 1, signal_day, **{"MACD多头": False}),
                complete_factor_row("000002", 2, signal_day, **{"站上MA5": False}),
            ]
        }
        settings = {
            "selected": {"above_ma5": True, "macd_bullish": True},
            "selected_risks": {},
            "turnover_range": (5.0, 10.0),
            "float_market_cap_range_yi": (50.0, 200.0),
            "pct_change_range": (3.0, 5.0),
            "amplitude_threshold": 3.0,
        }

        loose_daily, loose_summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 2}},
            require_all=False,
            **settings,
        )
        strict_daily, strict_summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 2}},
            require_all=True,
            **settings,
        )

        self.assertEqual(loose_daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(loose_daily.loc[0, "最终候选数"], 2)
        self.assertEqual(
            loose_daily.loc[0, "未满足条件（扣分项）"],
            "MACD多头（-1分）",
        )
        self.assertEqual(loose_summary["最终统计"], "1/1，2.00%")
        self.assertEqual(strict_daily.loc[0, "完整满足指标数"], 0)
        self.assertEqual(strict_daily.loc[0, "状态"], "无信号")
        self.assertEqual(strict_summary["最终统计"], "0/0，0.00%")

    def test_macd_dea_dif_gap_scores_only_values_inside_selected_range(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={
                (signal_day, "000001"): 3.0,
                (signal_day, "000002"): 1.0,
            },
        )
        factors = {
            signal_day: [
                complete_factor_row(
                    "000001", 1, signal_day, MACD_DEA=0.11, MACD_DIF=0.0
                ),
                complete_factor_row(
                    "000002", 2, signal_day, MACD_DEA=0.21, MACD_DIF=0.0
                ),
            ]
        }
        factor_frame = pd.DataFrame(factors[signal_day])
        selected = {
            "above_ma5": True,
            "macd_dea_minus_dif_high": True,
        }

        condition_matrix = core.strategy_app.condition_matrix(
            factor_frame,
            selected,
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            macd_dea_minus_dif_range=(0.1, 0.2),
        )
        self.assertEqual(
            condition_matrix["MACD红线-蓝线区间[0.1, 0.2]（DEA-DIF）"].tolist(),
            [True, False],
        )

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 2}},
            selected=selected,
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            require_all=True,
            macd_dea_minus_dif_range=(0.1, 0.2),
        )

        self.assertEqual(daily.loc[0, "风险剔除数"], 0)
        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "状态"], "预测正确")
        self.assertEqual(summary["最终统计"], "1/1，3.00%")


class FactorCacheTests(TestCase):
    def test_old_strategy_signature_invalidates_factor_cache(self) -> None:
        signal_day = date(2026, 5, 6)
        code = "000001"
        cache_key = "signature-test"
        history_token = "history-token"
        cached_factors = {
            signal_day.isoformat(): complete_factor_row(code, 1, signal_day)
        }

        with TemporaryDirectory() as temporary_directory:
            with patch.object(core, "FACTOR_CACHE_ROOT", Path(temporary_directory)):
                core._write_factor_cache(
                    code,
                    cached_factors,
                    cache_key=cache_key,
                    history_token=history_token,
                )
                self.assertEqual(
                    core._read_factor_cache(
                        code,
                        cache_key=cache_key,
                        history_token=history_token,
                        cache_hours=1.0,
                    ),
                    cached_factors,
                )

                cache_path = core._factor_cache_path(cache_key, code)
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                payload["strategy_snapshot_signature"] = "obsolete-strategy-signature"
                cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )

                self.assertEqual(
                    core._read_factor_cache(
                        code,
                        cache_key=cache_key,
                        history_token=history_token,
                        cache_hours=1.0,
                    ),
                    {},
                )


class HistoryCacheTests(TestCase):
    def test_legacy_history_cache_requires_complete_bar_shape(self) -> None:
        code = "000001"
        cache_key = "legacy-history-test"
        end_day = date(2026, 5, 6)
        bar = {column: 1.0 for column in core.HISTORY_SCHEMA_COLUMNS}
        bar["date"] = end_day.isoformat()
        payload = {
            "version": core.HISTORY_CACHE_VERSION,
            "code": code,
            "end_date": end_day.isoformat(),
            "saved_at": "2026-05-06T00:00:00+00:00",
            "source": "测试",
            "bars": [bar],
        }

        with TemporaryDirectory() as temporary_directory:
            with patch.object(core, "HISTORY_CACHE_ROOT", Path(temporary_directory)):
                cache_path = core._history_cache_path(cache_key, code)
                cache_path.parent.mkdir(parents=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

                self.assertIsNotNone(
                    core._read_history_cache(
                        code,
                        cache_key=cache_key,
                        end_date=end_day,
                        cache_hours=1.0,
                    )
                )

                payload["bars"][0].pop("amount")
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                self.assertIsNone(
                    core._read_history_cache(
                        code,
                        cache_key=cache_key,
                        end_date=end_day,
                        cache_hours=1.0,
                    )
                )


class MergedHistoryCacheTests(TestCase):
    @staticmethod
    def _history(days: list[str], *, close_start: float = 10.0) -> pd.DataFrame:
        closes = [close_start + index for index in range(len(days))]
        return core.strategy_app._normalize_history_frame(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(days),
                    "open": closes,
                    "close": [value + 0.1 for value in closes],
                    "high": [value + 0.2 for value in closes],
                    "low": [value - 0.1 for value in closes],
                    "volume": [1_000_000.0] * len(days),
                    "amount": [100_000_000.0] * len(days),
                    "amplitude": [1.0] * len(days),
                    "pct_change": [1.0] * len(days),
                    "turnover": [5.0] * len(days),
                }
            )
        )

    @staticmethod
    def _write_legacy_cache(
        root: Path,
        *,
        cache_key: str,
        code: str,
        first_signal_date: date,
        end_date: date,
        history: pd.DataFrame,
    ) -> None:
        path = root / cache_key / f"{code}.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": core.LEGACY_HISTORY_CACHE_VERSION,
                    "code": code,
                    "first_signal_date": first_signal_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "saved_at": "2026-05-08T00:00:00+00:00",
                    "source": "旧缓存",
                    "history_columns": list(core.HISTORY_SCHEMA_COLUMNS),
                    "bars": core.strategy_app._history_to_records(history),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_only_missing_tail_is_downloaded_then_merged_into_shared_cache(self) -> None:
        code = "000001"
        first_signal_date = date(2026, 5, 6)
        cached_end_date = date(2026, 5, 7)
        requested_end_date = date(2026, 5, 8)
        legacy_key = core.history_cache_key(first_signal_date, cached_end_date)
        cached_history = self._history(["2026-05-06", "2026-05-07"])
        missing_tail = self._history(["2026-05-08"], close_start=30.0)

        with TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            self._write_legacy_cache(
                cache_root,
                cache_key=legacy_key,
                code=code,
                first_signal_date=first_signal_date,
                end_date=cached_end_date,
                history=cached_history,
            )
            with (
                patch.object(core, "HISTORY_CACHE_ROOT", cache_root),
                patch.object(
                    core,
                    "_fetch_eastmoney_full_history",
                    return_value=missing_tail,
                ) as fetch_history,
            ):
                limiter = core.strategy_app.RequestRateLimiter(0.0)
                outcome = core._load_one_history(
                    code,
                    cache_key=core.history_cache_key(
                        first_signal_date, requested_end_date
                    ),
                    first_signal_date=first_signal_date,
                    end_date=requested_end_date,
                    cache_hours=1.0,
                    force_refresh=False,
                    limiter=limiter,
                    timeout_seconds=1.0,
                    legacy_cache_keys=(legacy_key,),
                )

                fetch_history.assert_called_once_with(
                    code,
                    history_start=date(2026, 5, 8),
                    end_date=requested_end_date,
                    limiter=limiter,
                    timeout_seconds=1.0,
                )
                self.assertFalse(outcome.from_cache)
                self.assertEqual(
                    outcome.history["date"].dt.date.tolist(),
                    [date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8)],
                )

                merged_path = core._history_cache_path("ignored", code)
                merged_payload = json.loads(merged_path.read_text(encoding="utf-8"))
                self.assertEqual(merged_payload["version"], core.HISTORY_CACHE_VERSION)
                self.assertEqual(merged_payload["end_date"], requested_end_date.isoformat())
                self.assertEqual(len(merged_payload["bars"]), 3)

                with patch.object(core, "_fetch_eastmoney_full_history") as fetch_again:
                    cached_outcome = core._load_one_history(
                        code,
                        cache_key=core.history_cache_key(
                            first_signal_date, requested_end_date
                        ),
                        first_signal_date=first_signal_date,
                        end_date=requested_end_date,
                        cache_hours=1.0,
                        force_refresh=False,
                        limiter=core.strategy_app.RequestRateLimiter(0.0),
                        timeout_seconds=1.0,
                        legacy_cache_keys=(legacy_key,),
                    )
                fetch_again.assert_not_called()
                self.assertTrue(cached_outcome.from_cache)

    def test_only_missing_prefix_is_downloaded_when_selected_start_moves_earlier(self) -> None:
        code = "000001"
        requested_first_signal_date = date(2026, 5, 6)
        cached_first_signal_date = date(2026, 5, 7)
        end_date = date(2026, 5, 8)
        legacy_key = core.history_cache_key(cached_first_signal_date, end_date)
        cached_history = self._history(["2026-05-07", "2026-05-08"])
        missing_prefix = self._history(["2026-05-06"], close_start=30.0)
        requested_history_start = requested_first_signal_date - timedelta(
            days=core.FULL_HISTORY_LOOKBACK_DAYS
        )
        cached_history_start = cached_first_signal_date - timedelta(
            days=core.FULL_HISTORY_LOOKBACK_DAYS
        )

        with TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            self._write_legacy_cache(
                cache_root,
                cache_key=legacy_key,
                code=code,
                first_signal_date=cached_first_signal_date,
                end_date=end_date,
                history=cached_history,
            )
            with (
                patch.object(core, "HISTORY_CACHE_ROOT", cache_root),
                patch.object(
                    core,
                    "_fetch_eastmoney_full_history",
                    return_value=missing_prefix,
                ) as fetch_history,
            ):
                limiter = core.strategy_app.RequestRateLimiter(0.0)
                outcome = core._load_one_history(
                    code,
                    cache_key=core.history_cache_key(
                        requested_first_signal_date, end_date
                    ),
                    first_signal_date=requested_first_signal_date,
                    end_date=end_date,
                    cache_hours=1.0,
                    force_refresh=False,
                    limiter=limiter,
                    timeout_seconds=1.0,
                    legacy_cache_keys=(legacy_key,),
                )

        fetch_history.assert_called_once_with(
            code,
            history_start=requested_history_start,
            end_date=cached_history_start - timedelta(days=1),
            limiter=limiter,
            timeout_seconds=1.0,
        )
        self.assertEqual(
            outcome.history["date"].dt.date.tolist(),
            [date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8)],
        )

    def test_legacy_bars_before_metadata_avoid_repeating_a_prefix_download(self) -> None:
        code = "000001"
        requested_first_signal_date = date(2026, 5, 6)
        cached_first_signal_date = date(2026, 5, 7)
        end_date = date(2026, 5, 8)
        requested_history_start = requested_first_signal_date - timedelta(
            days=core.FULL_HISTORY_LOOKBACK_DAYS
        )
        legacy_key = core.history_cache_key(cached_first_signal_date, end_date)
        history = self._history(
            [
                requested_history_start.isoformat(),
                "2026-05-07",
                "2026-05-08",
            ]
        )

        with TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            self._write_legacy_cache(
                cache_root,
                cache_key=legacy_key,
                code=code,
                first_signal_date=cached_first_signal_date,
                end_date=end_date,
                history=history,
            )
            with (
                patch.object(core, "HISTORY_CACHE_ROOT", cache_root),
                patch.object(core, "_fetch_eastmoney_full_history") as fetch_history,
            ):
                outcome = core._load_one_history(
                    code,
                    cache_key=core.history_cache_key(
                        requested_first_signal_date, end_date
                    ),
                    first_signal_date=requested_first_signal_date,
                    end_date=end_date,
                    cache_hours=1.0,
                    force_refresh=False,
                    limiter=core.strategy_app.RequestRateLimiter(0.0),
                    timeout_seconds=1.0,
                    legacy_cache_keys=(legacy_key,),
                )

        fetch_history.assert_not_called()
        self.assertTrue(outcome.from_cache)

    def test_legacy_full_coverage_is_migrated_without_a_download(self) -> None:
        code = "000001"
        first_signal_date = date(2026, 5, 6)
        end_date = date(2026, 5, 8)
        legacy_key = core.history_cache_key(first_signal_date, end_date)
        history = self._history(["2026-05-06", "2026-05-07", "2026-05-08"])

        with TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            self._write_legacy_cache(
                cache_root,
                cache_key=legacy_key,
                code=code,
                first_signal_date=first_signal_date,
                end_date=end_date,
                history=history,
            )
            with (
                patch.object(core, "HISTORY_CACHE_ROOT", cache_root),
                patch.object(core, "_fetch_eastmoney_full_history") as fetch_history,
            ):
                outcome = core._load_one_history(
                    code,
                    cache_key=legacy_key,
                    first_signal_date=first_signal_date,
                    end_date=end_date,
                    cache_hours=1.0,
                    force_refresh=False,
                    limiter=core.strategy_app.RequestRateLimiter(0.0),
                    timeout_seconds=1.0,
                    legacy_cache_keys=(legacy_key,),
                )
                self.assertTrue(core._history_cache_path(legacy_key, code).is_file())

        fetch_history.assert_not_called()
        self.assertTrue(outcome.from_cache)

    def test_force_refresh_and_disabled_cache_do_not_reuse_cached_coverage(self) -> None:
        code = "000001"
        first_signal_date = date(2026, 5, 6)
        end_date = date(2026, 5, 8)
        fresh_history = self._history(["2026-05-06", "2026-05-07", "2026-05-08"])

        with TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            with patch.object(core, "HISTORY_CACHE_ROOT", cache_root):
                core._write_history_cache(
                    code,
                    fresh_history,
                    "测试",
                    cache_key="test",
                    first_signal_date=first_signal_date,
                    end_date=end_date,
                )
                with patch.object(
                    core,
                    "_fetch_eastmoney_full_history",
                    return_value=fresh_history,
                ) as force_fetch:
                    core._load_one_history(
                        code,
                        cache_key="test",
                        first_signal_date=first_signal_date,
                        end_date=end_date,
                        cache_hours=1.0,
                        force_refresh=True,
                        limiter=core.strategy_app.RequestRateLimiter(0.0),
                        timeout_seconds=1.0,
                    )
                self.assertEqual(
                    force_fetch.call_args.kwargs["history_start"],
                    first_signal_date
                    - timedelta(days=core.FULL_HISTORY_LOOKBACK_DAYS),
                )

            disabled_root = cache_root / "disabled"
            with (
                patch.object(core, "HISTORY_CACHE_ROOT", disabled_root),
                patch.object(
                    core,
                    "_fetch_eastmoney_full_history",
                    return_value=fresh_history,
                ),
            ):
                core._load_one_history(
                    code,
                    cache_key="disabled",
                    first_signal_date=first_signal_date,
                    end_date=end_date,
                    cache_hours=0.0,
                    force_refresh=False,
                    limiter=core.strategy_app.RequestRateLimiter(0.0),
                    timeout_seconds=1.0,
                )
                self.assertFalse(
                    core._history_cache_path("disabled", code).exists()
                )


class HistoryEligibilityTests(TestCase):
    @staticmethod
    def _short_history() -> pd.DataFrame:
        count = core.strategy_app.MIN_REQUIRED_BARS - 1
        close = pd.Series(range(10, 10 + count), dtype="float64")
        return core.strategy_app._normalize_history_frame(
            pd.DataFrame(
                {
                    "date": pd.bdate_range("2026-01-02", periods=count),
                    "open": close,
                    "close": close,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "volume": 1_000_000.0,
                    "amount": 100_000_000.0,
                    "amplitude": 1.0,
                    "pct_change": 1.0,
                    "turnover": 5.0,
                }
            )
        )

    def test_unavailable_and_short_histories_are_excluded_before_factor_calculation(self) -> None:
        signal_day = date(2026, 5, 6)
        companies = pd.DataFrame(
            [
                {"序号": 1, "股票代码": "000001", "股票名称": "获取失败"},
                {"序号": 2, "股票代码": "000002", "股票名称": "预热不足"},
            ]
        )
        short_history = self._short_history()

        def load_history(code: str, **_: object) -> core.HistoryOutcome:
            if code == "000001":
                return core.HistoryOutcome(
                    code=code,
                    history=None,
                    source=None,
                    from_cache=False,
                    cache_token=None,
                    error="测试网络失败",
                )
            return core.HistoryOutcome(
                code=code,
                history=short_history,
                source="测试",
                from_cache=False,
                cache_token="short-history",
            )

        with patch.object(core, "_load_one_history", side_effect=load_history):
            outcomes, history_errors, history_summary, cache_key = core.collect_full_histories(
                companies,
                first_signal_date=signal_day,
                end_date=date(2026, 7, 23),
                cache_hours=0.0,
                force_refresh=False,
                workers=1,
                request_interval_seconds=0.0,
                timeout_seconds=1.0,
            )

        self.assertIsNone(outcomes["000001"].history)
        self.assertIsNone(outcomes["000002"].history)
        self.assertEqual(history_summary["历史失败"], 1)
        self.assertEqual(history_summary["历史预热不足股票数"], 1)
        self.assertEqual(history_summary["历史自动剔除股票数"], 2)
        self.assertEqual(history_summary["历史有效股票数"], 0)
        self.assertEqual(
            set(history_errors["问题类型"]), {"长历史获取失败", "历史预热不足"}
        )

        factors, day_stats, factor_errors = core.collect_all_factor_rows_by_day(
            companies,
            outcomes,
            (signal_day,),
            cache_key=cache_key,
            cache_hours=0.0,
            factor_workers=1,
        )

        self.assertEqual(factors[signal_day], [])
        self.assertTrue(factor_errors.empty)
        self.assertEqual(day_stats[signal_day]["因子计算失败数"], 0)
        self.assertEqual(day_stats[signal_day]["因子预热不足股票数"], 0)
        self.assertEqual(day_stats[signal_day]["长历史不可用股票数"], 2)

    def test_collectors_defensively_skip_a_directly_supplied_short_history(self) -> None:
        signal_day = date(2026, 5, 6)
        companies = pd.DataFrame(
            [{"序号": 1, "股票代码": "000002", "股票名称": "预热不足"}]
        )
        outcomes = {
            "000002": core.HistoryOutcome(
                code="000002",
                history=self._short_history(),
                source="测试",
                from_cache=False,
                cache_token="short-history",
            )
        }

        fixed_factors, fixed_stats, fixed_errors = core.collect_factor_rows_by_day(
            companies,
            outcomes,
            (signal_day,),
            cache_key="test-cache",
            cache_hours=0.0,
        )
        all_factors, all_stats, all_errors = core.collect_all_factor_rows_by_day(
            companies,
            outcomes,
            (signal_day,),
            cache_key="test-cache",
            cache_hours=0.0,
            factor_workers=1,
        )

        self.assertEqual(fixed_factors[signal_day], [])
        self.assertTrue(fixed_errors.empty)
        self.assertEqual(fixed_stats[signal_day]["历史预热不足股票数"], 1)
        self.assertEqual(fixed_stats[signal_day]["策略预热不足候选数"], 0)
        self.assertEqual(all_factors[signal_day], [])
        self.assertTrue(all_errors.empty)
        self.assertEqual(all_stats[signal_day]["历史预热不足股票数"], 1)
        self.assertEqual(all_stats[signal_day]["因子预热不足股票数"], 0)
        self.assertEqual(all_stats[signal_day]["因子计算失败数"], 0)


class CliBacktestEntrypointTests(TestCase):
    def test_history_failures_and_warmup_shortages_continue_to_report(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={},
        )
        companies = pd.DataFrame(
            [
                {"序号": 1, "股票代码": "000001", "股票名称": "获取失败"},
                {"序号": 2, "股票代码": "000002", "股票名称": "预热不足"},
            ]
        )
        history_errors = pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "000001",
                    "股票名称": "获取失败",
                    "选股日期": None,
                    "问题类型": "长历史获取失败",
                    "失败原因": "测试网络失败",
                },
                {
                    "序号": 2,
                    "股票代码": "000002",
                    "股票名称": "预热不足",
                    "选股日期": None,
                    "问题类型": "历史预热不足",
                    "失败原因": "仅有119根日线",
                },
            ],
            columns=core.HISTORY_ERROR_COLUMNS,
        )
        history_summary = {
            "股票总数": 2,
            "历史成功": 1,
            "历史获取失败股票数": 1,
            "历史预热不足股票数": 1,
            "历史自动剔除股票数": 2,
            "历史有效股票数": 0,
            "历史缓存命中": 0,
        }
        histories = {
            "000001": core.HistoryOutcome(
                code="000001",
                history=None,
                source=None,
                from_cache=False,
                cache_token=None,
                error="测试网络失败",
            ),
            "000002": core.HistoryOutcome(
                code="000002",
                history=None,
                source="测试",
                from_cache=False,
                cache_token=None,
                error="历史预热不足",
            ),
        }
        factors_by_day = {signal_day: []}
        day_stats = {signal_day: {"策略预热不足候选数": 0}}
        factor_errors = pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS)
        daily_results = pd.DataFrame(
            {
                "选股日期": [signal_day],
                "下一市场交易日": [next_day],
                "选中股票代码": [None],
                "选中股票名称": [None],
                "状态": ["无信号"],
                "说明": ["该日无可用信号"],
            }
        )
        evaluation_summary = {"最终统计": "0/1，0.00%"}

        with (
            patch.object(
                core,
                "load_strict_next_day_returns",
                return_value=return_data,
            ),
            patch.object(
                run_backtest.strategy_app,
                "load_mainboard_companies",
                return_value=companies,
            ),
            patch.object(
                core,
                "collect_full_histories",
                return_value=(histories, history_errors, history_summary, "test-cache"),
            ),
            patch.object(
                core,
                "collect_factor_rows_by_day",
                return_value=(factors_by_day, day_stats, factor_errors),
            ) as collect_factors,
            patch.object(
                core,
                "evaluate_fixed_strategy",
                return_value=(daily_results, evaluation_summary),
            ) as evaluate,
            patch.object(core, "write_backtest_workbook") as write_report,
            patch("builtins.print") as print_mock,
        ):
            self.assertEqual(run_backtest.main([]), 0)

        collect_factors.assert_called_once()
        self.assertIs(collect_factors.call_args.args[0], companies)
        self.assertEqual(collect_factors.call_args.args[2], return_data.signal_dates)
        evaluate.assert_called_once_with(
            return_data,
            factors_by_day,
            day_stats,
        )
        write_report.assert_called_once()
        written_daily, written_summary, data_problems, _ = write_report.call_args.args
        self.assertIs(written_daily, daily_results)
        self.assertEqual(written_summary["历史失败股票数"], 1)
        self.assertEqual(written_summary["历史获取失败股票数"], 1)
        self.assertEqual(written_summary["历史预热不足股票数"], 1)
        self.assertEqual(written_summary["历史自动剔除股票数"], 2)
        self.assertEqual(written_summary["历史有效股票数"], 0)
        self.assertEqual(
            set(data_problems["问题类型"]),
            {"长历史获取失败", "历史预热不足"},
        )
        self.assertTrue(
            any(
                "真实失败 1" in str(call.args[0])
                and "自动剔除 2" in str(call.args[0])
                for call in print_mock.call_args_list
                if call.args
            )
        )


class BasicPrefilterTests(TestCase):
    def test_prefilter_keeps_boundary_amount_and_market_cap(self) -> None:
        dates = pd.bdate_range("2026-03-02", periods=25)
        close = pd.Series(range(100, 125), dtype="float64")
        history = pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "close": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
                "amplitude": 1.0,
                "pct_change": 1.0,
                # 100,000,000 * 100 / 2 / 100,000,000 = 50 亿元。
                "turnover": 2.0,
            }
        )
        selected_day = dates[-1].date()

        matched = core._basic_prefilter_dates(history, [selected_day])

        self.assertEqual(matched, {selected_day})


class FullFactorWindowEquivalenceTests(TestCase):
    @staticmethod
    def _history(*, seed: float) -> pd.DataFrame:
        """构造跨越多个预热窗口的确定性完整日线。"""

        count = core.strategy_app.INDICATOR_WARMUP_BARS + 30
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

    def test_multiple_stocks_and_dates_match_page_factor_function_field_by_field(self) -> None:
        for history in (self._history(seed=25.0), self._history(seed=80.0)):
            warmup = core.strategy_app.INDICATOR_WARMUP_BARS
            signal_dates = tuple(
                pd.Timestamp(history["date"].iloc[position]).date()
                for position in (
                    warmup - 1,
                    warmup + 7,
                    warmup + 19,
                    warmup + 29,
                )
            )
            actual_by_date = core._calculate_factor_values_for_dates(history, signal_dates)

            self.assertEqual(set(actual_by_date), {day.isoformat() for day in signal_dates})
            for signal_day in signal_dates:
                position = int(history["date"].searchsorted(pd.Timestamp(signal_day), side="right")) - 1
                expected_window = history.iloc[
                    max(0, position - core.strategy_app.INDICATOR_WARMUP_BARS + 1) : position + 1
                ].reset_index(drop=True)
                expected = core.strategy_app._calculate_factors_from_normalized_history(
                    expected_window,
                    as_of_date=signal_day,
                )
                actual = actual_by_date[signal_day.isoformat()]

                self.assertEqual(set(actual), set(expected))
                for field_name, expected_value in expected.items():
                    self.assertEqual(
                        actual[field_name],
                        expected_value,
                        f"{signal_day.isoformat()} 的 {field_name} 与页面因子不一致",
                    )


class FullFactorCacheIsolationTests(TestCase):
    def test_full_panel_does_not_read_legacy_fixed_strategy_factor_cache(self) -> None:
        first_history = FullFactorWindowEquivalenceTests._history(seed=25.0)
        second_history = FullFactorWindowEquivalenceTests._history(seed=80.0)
        warmup = core.strategy_app.INDICATOR_WARMUP_BARS
        signal_dates = tuple(
            pd.Timestamp(first_history["date"].iloc[position]).date()
            for position in (warmup - 1, warmup + 17)
        )
        companies = pd.DataFrame(
            [
                {"序号": 1, "股票代码": "000001", "股票名称": "测试一"},
                {"序号": 2, "股票代码": "000002", "股票名称": "测试二"},
            ]
        )
        outcomes = {
            "000001": core.HistoryOutcome(
                code="000001",
                history=first_history,
                source="测试",
                from_cache=True,
                cache_token="history-one",
            ),
            "000002": core.HistoryOutcome(
                code="000002",
                history=second_history,
                source="测试",
                from_cache=True,
                cache_token="history-two",
            ),
        }
        legacy_value = complete_factor_row("000001", 1, signal_dates[0], **{"RSI14": -1.0})

        with TemporaryDirectory() as temporary_directory:
            with patch.object(core, "FACTOR_CACHE_ROOT", Path(temporary_directory)):
                core._write_factor_cache(
                    "000001",
                    {signal_dates[0].isoformat(): legacy_value},
                    cache_key="test-history",
                    history_token="history-one",
                )
                rows_by_day, _, errors = core.collect_all_factor_rows_by_day(
                    companies,
                    outcomes,
                    signal_dates,
                    cache_key="test-history",
                    cache_hours=1.0,
                    factor_workers=1,
                )

                self.assertTrue(errors.empty)
                first_row = next(
                    row
                    for row in rows_by_day[signal_dates[0]]
                    if row["股票代码"] == "000001"
                )
                expected = core.strategy_app._calculate_factors_from_normalized_history(
                    first_history.iloc[: core.strategy_app.INDICATOR_WARMUP_BARS].reset_index(
                        drop=True
                    ),
                    as_of_date=signal_dates[0],
                )
                self.assertEqual(first_row["RSI14"], expected["RSI14"])
                self.assertNotEqual(first_row["RSI14"], -1.0)

                full_cache_path = core._factor_cache_path(
                    core._full_factor_cache_key("test-history"), "000001"
                )
                self.assertTrue(full_cache_path.is_file())
                full_payload = json.loads(full_cache_path.read_text(encoding="utf-8"))
                self.assertEqual(full_payload["version"], core.FULL_FACTOR_CACHE_VERSION)


class FullFactorParallelismTests(TestCase):
    def test_factor_collectors_preserve_company_industry(self) -> None:
        history = FullFactorWindowEquivalenceTests._history(seed=25.0)
        signal_day = pd.Timestamp(
            history["date"].iloc[core.strategy_app.INDICATOR_WARMUP_BARS - 1]
        ).date()
        companies = pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "000001",
                    "股票名称": "测试一",
                    "所属行业": "电子",
                }
            ]
        )
        outcomes = {
            "000001": core.HistoryOutcome(
                code="000001",
                history=history,
                source="测试",
                from_cache=True,
                cache_token="history-one",
            )
        }

        with patch.object(core, "_basic_prefilter_dates", return_value={signal_day}):
            fixed_rows, _, fixed_errors = core.collect_factor_rows_by_day(
                companies,
                outcomes,
                (signal_day,),
                cache_key="test-industry-fixed",
                cache_hours=0.0,
            )
        full_rows, _, full_errors = core.collect_all_factor_rows_by_day(
            companies,
            outcomes,
            (signal_day,),
            cache_key="test-industry-full",
            cache_hours=0.0,
            factor_workers=1,
        )

        self.assertTrue(fixed_errors.empty)
        self.assertTrue(full_errors.empty)
        self.assertEqual(fixed_rows[signal_day][0]["所属行业"], "电子")
        self.assertEqual(full_rows[signal_day][0]["所属行业"], "电子")

    def test_full_factor_panel_uses_thread_pool_for_parallel_calculation(self) -> None:
        first_history = FullFactorWindowEquivalenceTests._history(seed=25.0)
        second_history = FullFactorWindowEquivalenceTests._history(seed=80.0)
        signal_day = pd.Timestamp(
            first_history["date"].iloc[core.strategy_app.INDICATOR_WARMUP_BARS - 1]
        ).date()
        companies = pd.DataFrame(
            [
                {"序号": 1, "股票代码": "000001", "股票名称": "测试一"},
                {"序号": 2, "股票代码": "000002", "股票名称": "测试二"},
            ]
        )
        outcomes = {
            "000001": core.HistoryOutcome(
                code="000001",
                history=first_history,
                source="测试",
                from_cache=True,
                cache_token="history-one",
            ),
            "000002": core.HistoryOutcome(
                code="000002",
                history=second_history,
                source="测试",
                from_cache=True,
                cache_token="history-two",
            ),
        }
        actual_executor = core.ThreadPoolExecutor

        with patch.object(
            core, "ThreadPoolExecutor", wraps=actual_executor
        ) as thread_pool:
            rows_by_day, day_stats, errors = core.collect_all_factor_rows_by_day(
                companies,
                outcomes,
                (signal_day,),
                cache_key="test-thread-pool",
                cache_hours=0.0,
                factor_workers=2,
            )

        thread_pool.assert_called_once()
        self.assertTrue(errors.empty)
        self.assertEqual(len(rows_by_day[signal_day]), 2)
        self.assertEqual(day_stats[signal_day]["因子计算失败数"], 0)
