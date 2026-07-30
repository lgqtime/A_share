"""行业共识选股和回测接入的回归测试。"""

from __future__ import annotations

from datetime import date
from unittest import TestCase

import pandas as pd

from strategy_backtest import backtest_core as core
from strategy_backtest.industry_consensus import (
    describe_industry_consensus,
    select_industry_consensus_candidate,
)


def ranked_row(
    code: str,
    score: float,
    industry: object,
) -> dict[str, object]:
    return {
        "股票代码": code,
        "股票名称": f"测试{code}",
        "得分": score,
        "所属行业": industry,
    }


def amount_factor_row(
    code: str,
    industry: str | None,
    signal_day: date,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "序号": int(code),
        "股票代码": code,
        "股票名称": f"测试{code}",
        "所属行业": industry,
        "数据来源": "测试来源",
        "数据日期": signal_day.isoformat(),
        "当日成交额": 100_000_000.0,
        "BIAS20": 0.0,
    }
    row.update(overrides)
    return row


class IndustryConsensusSelectionTests(TestCase):
    def test_only_the_first_fifty_ranked_candidates_are_counted(self) -> None:
        rows = [ranked_row(f"{index:06d}", 100.0 - index, "行业A") for index in range(25)]
        rows.extend(
            ranked_row(f"{index:06d}", 75.0 - (index - 25), "行业B")
            for index in range(25, 51)
        )

        candidate = select_industry_consensus_candidate(pd.DataFrame(rows))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["所属行业"], "行业A")
        self.assertEqual(candidate["股票代码"], "000000")

    def test_tied_industries_and_scores_keep_the_existing_rank_order(self) -> None:
        ranked = pd.DataFrame(
            [
                ranked_row("000002", 10.0, "行业A"),
                ranked_row("000001", 10.0, "行业B"),
                ranked_row("000003", 9.0, "行业A"),
                ranked_row("000004", 8.0, "行业B"),
            ]
        )

        details = describe_industry_consensus(ranked)

        self.assertEqual(details.leading_industries, ("行业A", "行业B"))
        self.assertEqual(details.leading_industry_count, 2)
        self.assertIsNotNone(details.candidate)
        assert details.candidate is not None
        self.assertEqual(details.candidate["股票代码"], "000002")

    def test_blank_industries_do_not_form_a_shared_industry(self) -> None:
        ranked = pd.DataFrame(
            [
                ranked_row("000001", 10.0, ""),
                ranked_row("000002", 9.0, None),
                ranked_row("000003", 8.0, "未分类"),
                ranked_row("000004", 7.0, "行业A"),
                ranked_row("000005", 6.0, "行业B"),
                ranked_row("000006", 5.0, "行业B"),
            ]
        )

        details = describe_industry_consensus(ranked)

        self.assertEqual(details.top_candidate_count, 6)
        self.assertEqual(details.valid_industry_candidate_count, 3)
        self.assertEqual(details.leading_industries, ("行业B",))
        self.assertIsNotNone(details.candidate)
        assert details.candidate is not None
        self.assertEqual(details.candidate["股票代码"], "000005")

    def test_all_missing_industries_return_no_candidate(self) -> None:
        ranked = pd.DataFrame(
            [
                ranked_row("000001", 10.0, pd.NA),
                ranked_row("000002", 9.0, " "),
            ]
        )

        details = describe_industry_consensus(ranked)

        self.assertIsNone(details.candidate)
        self.assertEqual(details.valid_industry_candidate_count, 0)


class IndustryConsensusBacktestTests(TestCase):
    def test_backtest_uses_the_highest_scored_stock_not_the_majority_industry(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={
                (signal_day, "000001"): 99.0,
                (signal_day, "000002"): 1.25,
                (signal_day, "000003"): 2.0,
            },
        )
        factors = {
            signal_day: [
                amount_factor_row(
                    "000001", "电力设备", signal_day, **{"站上MA5": True}
                ),
                amount_factor_row(
                    "000002", "医药生物", signal_day, **{"站上MA5": False}
                ),
                amount_factor_row(
                    "000003", "医药生物", signal_day, **{"站上MA5": False}
                ),
            ]
        }

        daily, summary = core.evaluate_strategy(
            return_data,
            factors,
            {signal_day: {"精算因子行数": 3}},
            selected={"amount_at_least_100m": True, "above_ma5": True},
            selected_risks={},
            turnover_range=(5.0, 10.0),
            float_market_cap_range_yi=(50.0, 200.0),
            pct_change_range=(3.0, 5.0),
            amplitude_threshold=3.0,
            require_all=False,
        )

        self.assertEqual(daily.loc[0, "最终候选数"], 3)
        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "选中所属行业"], "电力设备")
        self.assertAlmostEqual(daily.loc[0, "当日组合收益率（%）"], 99.0)
        self.assertAlmostEqual(summary["总收益率（%）"], 99.0)

    def test_backtest_keeps_a_candidate_without_industry(self) -> None:
        signal_day = date(2026, 5, 6)
        next_day = date(2026, 5, 7)
        return_data = core.ReturnData(
            signal_dates=(signal_day,),
            next_trade_dates={signal_day: next_day},
            strict_returns={(signal_day, "000001"): 3.0},
        )
        factors = {
            signal_day: [amount_factor_row("000001", None, signal_day)]
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

        self.assertEqual(daily.loc[0, "选中股票代码"], "000001")
        self.assertEqual(daily.loc[0, "选中所属行业"], None)
        self.assertEqual(daily.loc[0, "当日组合收益率（%）"], 3.0)
        self.assertEqual(summary["预测天数"], 1)
