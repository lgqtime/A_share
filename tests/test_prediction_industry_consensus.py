from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

import szse_quant_app as root_app
from strategy_backtest import szse_quant_app as strategy_app


APPS = (root_app, strategy_app)
ROOT_APP_PATH = Path(__file__).resolve().parents[1] / "szse_quant_app.py"


class PredictionIndustryConsensusTests(unittest.TestCase):
    def test_company_loader_preserves_industry_and_rejects_legacy_workbook(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            current_workbook = directory / "current.xlsx"
            legacy_workbook = directory / "legacy.xlsx"
            pd.DataFrame(
                {
                    "公司代码": ["000001", "000002"],
                    "公司简称": ["甲公司", "乙公司"],
                    "所属行业": ["金融业", "制造业"],
                }
            ).to_excel(current_workbook, sheet_name="主板公司", index=False)
            pd.DataFrame(
                {
                    "公司代码": ["000003"],
                    "公司简称": ["丙公司"],
                }
            ).to_excel(legacy_workbook, sheet_name="主板公司", index=False)

            for app in APPS:
                current = app.load_mainboard_companies(current_workbook)

                self.assertEqual(current["所属行业"].tolist(), ["金融业", "制造业"])
                with self.assertRaisesRegex(ValueError, "所属行业"):
                    app.load_mainboard_companies(legacy_workbook)

    def test_factor_collection_keeps_company_industry(self) -> None:
        companies = pd.DataFrame(
            {
                "序号": [1],
                "股票代码": ["000001"],
                "股票名称": ["甲公司"],
                "所属行业": ["金融业"],
            }
        )
        for app in APPS:
            outcome = app.FetchOutcome(
                code="000001",
                history=pd.DataFrame(),
                source="测试来源",
                from_cache=True,
                factors={"数据日期": "2026-07-28"},
            )
            with patch.object(app, "fetch_stock_history", return_value=outcome):
                factors, errors, summary = app.collect_factor_frame(
                    companies,
                    max_companies=1,
                    cache_hours=1.0,
                    force_refresh=False,
                    workers=1,
                    request_interval_seconds=0.0,
                    as_of_date=date(2026, 7, 28),
                )

            self.assertTrue(errors.empty)
            self.assertEqual(summary["成功"], 1)
            self.assertEqual(factors["所属行业"].tolist(), ["金融业"])

    def test_review_candidates_include_rank_and_industry_for_manual_judgment(self) -> None:
        ranked_candidates = pd.DataFrame(
            {
                "股票代码": ["000001", "000003", "000002", "000004"],
                "股票名称": ["金融甲", "制造甲", "金融乙", "制造乙"],
                "得分": [9.5, 9.2, 9.0, 8.9],
            }
        )
        factors = pd.DataFrame(
            {
                "股票代码": ["000001", "000002", "000003", "000004"],
                "所属行业": ["金融业", "金融业", "制造业", "制造业"],
            }
        )
        for app in APPS:
            review_candidates = app.prepare_prediction_review_candidates(
                ranked_candidates,
                factors,
            )

            self.assertEqual(len(review_candidates), 4)
            self.assertEqual(
                review_candidates[app.PREDICTION_REVIEW_RANK_COLUMN].tolist(),
                [1, 2, 3, 4],
            )
            self.assertEqual(review_candidates.iloc[0]["股票代码"], "000001")
            self.assertEqual(review_candidates.iloc[0]["所属行业"], "金融业")
            self.assertEqual(
                review_candidates.columns[:5].tolist(),
                [
                    app.PREDICTION_REVIEW_RANK_COLUMN,
                    "股票代码",
                    "股票名称",
                    "所属行业",
                    "得分",
                ],
            )

    def test_review_candidates_mark_missing_industry_without_discarding_stock(self) -> None:
        ranked_candidates = pd.DataFrame(
            {
                "股票代码": ["000001", "000002"],
                "股票名称": ["甲公司", "乙公司"],
                "得分": [9.0, 8.0],
            }
        )
        for app in APPS:
            review_candidates = app.prepare_prediction_review_candidates(
                ranked_candidates,
                pd.DataFrame({"股票代码": ["000001", "000002"]}),
            )

            self.assertEqual(len(review_candidates), 2)
            self.assertEqual(
                review_candidates["所属行业"].tolist(),
                [app.UNKNOWN_INDUSTRY, app.UNKNOWN_INDUSTRY],
            )

    def test_review_candidates_stop_at_the_first_fifty_ranked_stocks(self) -> None:
        ranked_candidates = pd.DataFrame(
            {
                "股票代码": [f"{index:06d}" for index in range(1, 52)],
                "股票名称": [f"公司{index}" for index in range(1, 52)],
                "所属行业": ["制造业"] * 51,
                "得分": [100.0 - index for index in range(1, 52)],
            }
        )

        for app in APPS:
            review_candidates = app.prepare_prediction_review_candidates(
                ranked_candidates,
                pd.DataFrame(),
            )

            self.assertEqual(len(review_candidates), app.PREDICTION_REVIEW_TOP_N)
            self.assertEqual(
                review_candidates.iloc[-1][app.PREDICTION_REVIEW_RANK_COLUMN],
                app.PREDICTION_REVIEW_TOP_N,
            )
            self.assertEqual(review_candidates.iloc[-1]["股票代码"], "000050")

    def test_root_app_overwrites_the_fixed_top_fifty_export(self) -> None:
        first_export = pd.DataFrame(
            {
                root_app.PREDICTION_REVIEW_RANK_COLUMN: [1],
                "股票代码": ["000001"],
                "股票名称": ["甲公司"],
                "所属行业": ["金融业"],
            }
        )
        replacement_export = first_export.assign(股票代码="000002", 股票名称="乙公司")
        with TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory) / "前 50 名（含所属行业）.csv"
            root_app.save_prediction_review_candidates(first_export, export_path)
            root_app.save_prediction_review_candidates(replacement_export, export_path)
            saved = pd.read_csv(export_path, encoding="utf-8-sig", dtype={"股票代码": str})

        self.assertEqual(saved["股票代码"].tolist(), ["000002"])
        self.assertEqual(saved["股票名称"].tolist(), ["乙公司"])

    def test_review_industry_summary_counts_each_industry_and_keeps_unknown(self) -> None:
        review_candidates = pd.DataFrame(
            {
                "所属行业": ["Beta", "Alpha", "Beta", None, "Alpha", ""],
            }
        )
        expected = pd.DataFrame(
            {
                "所属行业": ["Alpha", "Beta", "未分类"],
                "入选数（前50）": [2, 2, 2],
            }
        )

        for app in APPS:
            industry_summary = app.summarize_prediction_review_industries(
                review_candidates
            )

            pd.testing.assert_frame_equal(industry_summary, expected)

    def test_review_industry_summary_only_counts_the_first_fifty(self) -> None:
        review_candidates = pd.DataFrame(
            {
                "所属行业": ["制造业"] * 50 + ["金融业"],
            }
        )

        for app in APPS:
            industry_summary = app.summarize_prediction_review_industries(
                review_candidates
            )

            self.assertEqual(industry_summary.to_dict("records"), [
                {
                    "所属行业": "制造业",
                    app.PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN: 50,
                }
            ])

    def test_review_industry_summary_marks_legacy_rows_without_industry_as_unknown(
        self,
    ) -> None:
        review_candidates = pd.DataFrame(
            {
                "股票代码": ["000001", "000002"],
            }
        )

        for app in APPS:
            industry_summary = app.summarize_prediction_review_industries(
                review_candidates
            )

            self.assertEqual(industry_summary.to_dict("records"), [
                {
                    "所属行业": app.UNKNOWN_INDUSTRY,
                    app.PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN: 2,
                }
            ])

    def test_review_candidates_do_not_include_risk_excluded_high_score_stock(self) -> None:
        factors = pd.DataFrame(
            {
                "股票代码": ["000001", "000002"],
                "股票名称": ["高分风险股", "可用股"],
                "所属行业": ["金融业", "制造业"],
                "站上MA5": [True, True],
                "量比": [2.0, 1.0],
                "收盘日内位置（%）": [80.0, 80.0],
                "当日成交额": [100_000_000.0, 100_000_000.0],
                "BIAS20": [11.0, 0.0],
            }
        )
        for app in APPS:
            selected = {
                key: key == "above_ma5" for key in app.SCORING_INDICATOR_KEYS
            }
            ranked_candidates, eligible_count, risk_excluded_count = app.score_and_select(
                factors,
                selected,
                selected_risks={"bias_high": True},
                top_n=app.PREDICTION_REVIEW_TOP_N,
            )
            review_candidates = app.prepare_prediction_review_candidates(
                ranked_candidates,
                factors,
            )

            self.assertEqual(eligible_count, 1)
            self.assertEqual(risk_excluded_count, 1)
            self.assertEqual(review_candidates["股票代码"].tolist(), ["000002"])
            self.assertEqual(
                review_candidates.iloc[0][app.PREDICTION_REVIEW_RANK_COLUMN],
                1,
            )

    def test_top_ten_view_remains_the_first_ten_of_risk_filtered_ranking(self) -> None:
        factors = pd.DataFrame(
            {
                "股票代码": [f"0000{index:02d}" for index in range(1, 13)],
                "股票名称": [f"公司{index}" for index in range(1, 13)],
                "站上MA5": [True] * 12,
                "量比": list(range(12, 0, -1)),
                "收盘日内位置（%）": [80.0] * 12,
                "当日成交额": [100_000_000.0] * 12,
            }
        )
        for app in APPS:
            selected = {
                key: key == "above_ma5" for key in app.SCORING_INDICATOR_KEYS
            }
            top_ten, _, _ = app.score_and_select(
                factors,
                selected,
                selected_risks={},
                top_n=10,
            )
            top_fifty, _, _ = app.score_and_select(
                factors,
                selected,
                selected_risks={},
                top_n=app.PREDICTION_REVIEW_TOP_N,
            )

            pd.testing.assert_frame_equal(
                top_ten,
                top_fifty.head(10).reset_index(drop=True),
            )

    def test_root_app_renders_first_rank_and_top_fifty_separately(self) -> None:
        app = AppTest.from_file(ROOT_APP_PATH)
        app.run(timeout=60)
        self.assertEqual(len(app.exception), 0)

        selected_day = date.today().isoformat()
        app.session_state["szse_quant_as_of_date"] = selected_day
        app.session_state["szse_quant_results_as_of_date"] = selected_day
        app.session_state["szse_quant_results_max_score"] = 9.5
        app.session_state["szse_quant_results"] = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "股票名称": ["甲公司"],
                "得分": [9.5],
                "满足条件": ["站上MA5"],
                "未满足条件（扣分项）": ["无"],
            }
        )
        app.session_state["szse_quant_ranked_top_50"] = pd.DataFrame(
            {
                root_app.PREDICTION_REVIEW_RANK_COLUMN: [1, 2],
                "股票代码": ["000001", "000002"],
                "股票名称": ["甲公司", "乙公司"],
                "所属行业": ["金融业", "制造业"],
                "得分": [9.5, 9.0],
                "满足条件": ["站上MA5", "站上MA5"],
                "未满足条件（扣分项）": ["无", "无"],
            }
        )
        app.run(timeout=60)

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any(
                subheader.value == "评分排名第一（含所属行业）"
                for subheader in app.subheader
            )
        )
        self.assertTrue(
            any(
                subheader.value == "前 2 名（含所属行业）"
                for subheader in app.subheader
            )
        )
        self.assertTrue(
            any(
                subheader.value == "当日前 2 名行业入选数量"
                for subheader in app.subheader
            )
        )
        review_tables = [
            table.value
            for table in app.dataframe
            if root_app.PREDICTION_REVIEW_RANK_COLUMN in table.value.columns
        ]
        self.assertEqual(len(review_tables), 2)
        self.assertEqual(len(review_tables[0]), 1)
        self.assertEqual(len(review_tables[1]), 2)
        self.assertEqual(review_tables[1].iloc[0]["所属行业"], "金融业")
        industry_summary_tables = [
            table.value
            for table in app.dataframe
            if root_app.PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN in table.value.columns
        ]
        self.assertEqual(len(industry_summary_tables), 1)
        self.assertEqual(
            industry_summary_tables[0].to_dict("records"),
            [
                {
                    "所属行业": "制造业",
                    root_app.PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN: 1,
                },
                {
                    "所属行业": "金融业",
                    root_app.PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN: 1,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
