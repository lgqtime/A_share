from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import scheduled_ashare_workflow as workflow


class ScheduledAshareWorkflowTests(unittest.TestCase):
    def test_send_pushplus_with_retry_retries_a_transient_failure(self) -> None:
        logger = Mock()
        with patch.object(
            workflow,
            "send_pushplus",
            side_effect=[workflow.ScheduledWorkflowError("temporary outage"), None],
        ) as send:
            with patch.object(time, "sleep") as retry_sleep:
                workflow.send_pushplus_with_retry(
                    env_file=Path(".env"),
                    title="summary",
                    content="body",
                    logger=logger,
                )

        self.assertEqual(send.call_count, 2)
        retry_sleep.assert_called_once_with(5.0)
        self.assertEqual(logger.warning.call_count, 1)
        logger.error.assert_not_called()

    def test_send_pushplus_with_retry_logs_and_raises_after_all_failures(self) -> None:
        logger = Mock()
        failure = workflow.ScheduledWorkflowError("network unavailable")
        with patch.object(workflow, "send_pushplus", side_effect=failure) as send:
            with patch.object(time, "sleep") as retry_sleep:
                with self.assertRaisesRegex(workflow.ScheduledWorkflowError, "network unavailable"):
                    workflow.send_pushplus_with_retry(
                        env_file=Path(".env"),
                        title="summary",
                        content="body",
                        logger=logger,
                    )

        self.assertEqual(send.call_count, 3)
        self.assertEqual(retry_sleep.call_count, 2)
        self.assertEqual(logger.warning.call_count, 3)
        logger.error.assert_called_once()

    def test_send_pushplus_with_retry_persists_failures_to_workflow_log(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            logger = workflow.configure_logger(
                workflow.output_root(project),
                datetime(2026, 8, 5, tzinfo=workflow.CHINA_TIMEZONE),
            )
            try:
                with patch.object(
                    workflow,
                    "send_pushplus",
                    side_effect=workflow.ScheduledWorkflowError("network unavailable"),
                ):
                    with patch.object(time, "sleep"):
                        with self.assertRaises(workflow.ScheduledWorkflowError):
                            workflow.send_pushplus_with_retry(
                                env_file=project / ".env",
                                title="summary",
                                content="body",
                                logger=logger,
                            )
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()

            log_path = workflow.output_root(project) / "logs" / "scheduled_workflow_2026-08-05.log"
            log_content = log_path.read_text(encoding="utf-8")

        self.assertIn("PushPlus send attempt 3/3 failed: network unavailable", log_content)
        self.assertIn("PushPlus summary delivery failed after 3 attempts.", log_content)

    def test_previous_trading_day_skips_weekend_and_holiday(self) -> None:
        trade_dates = {
            date(2026, 7, 30),
            date(2026, 7, 31),
            date(2026, 8, 3),
            date(2026, 9, 30),
            date(2026, 10, 8),
        }

        self.assertEqual(
            workflow.previous_trading_day(trade_dates, date(2026, 8, 3)),
            date(2026, 7, 31),
        )
        self.assertEqual(
            workflow.previous_trading_day(trade_dates, date(2026, 10, 8)),
            date(2026, 9, 30),
        )

    def test_scheduled_target_skips_non_trading_day(self) -> None:
        logger = Mock()
        with TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            with patch.object(
                workflow,
                "load_trade_calendar",
                return_value={date(2026, 7, 30), date(2026, 7, 31)},
            ):
                result = workflow.scheduled_target_day(
                    project,
                    date(2026, 8, 1),
                    logger,
                )

        self.assertIsNone(result)
        logger.info.assert_called()

    def test_read_top_ten_uses_first_ten_rows_in_csv_order(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / workflow.TOP_FIFTY_FILE_NAME
            frame = pd.DataFrame(
                {
                    "评分排名": range(1, 12),
                    "股票代码": [f"{index:06d}" for index in range(1, 12)],
                    "股票名称": [f"股票{index}" for index in range(1, 12)],
                    "所属行业": ["测试行业"] * 11,
                    "得分": [10 - index / 10 for index in range(11)],
                }
            )
            frame.to_csv(source, index=False, encoding="utf-8-sig")
            stocks = workflow.read_top_ten(source)

        self.assertEqual(len(stocks), 10)
        self.assertEqual(stocks[0]["ticker"], "000001")
        self.assertEqual(stocks[-1]["ticker"], "000010")
        self.assertEqual(stocks[0]["screening"]["rank"], 1)

    def test_unconfigured_holding_is_not_reported_as_not_held(self) -> None:
        stocks = [{"ticker": "001221"}, {"ticker": "002090"}]
        workflow.apply_holding_context(stocks, {"positions": {"001221": {"shares": 500}}})

        self.assertEqual(stocks[0]["holding"]["status"], "已持有")
        self.assertEqual(stocks[0]["holding"]["shares"], 500)
        self.assertEqual(stocks[1]["holding"]["status"], "未配置")
        self.assertIsNone(stocks[1]["holding"]["shares"])

    def test_message_marks_prior_close_and_unconfigured_positions(self) -> None:
        content = workflow.build_pushplus_html(
            {"source_csv": "D:/source.csv"},
            {
                "trade_date": "2026-07-30",
                "summary": {"requested": 1, "completed": 1, "failed": 0},
                "stocks": [
                    {
                        "ticker": "001221",
                        "name": "测试股票",
                        "industry": "制造业",
                        "screening": {"rank": 1, "score": 9.5, "close": 41.67},
                        "holding": {"status": "未配置"},
                        "decision": {"action": "buy", "confidence": 0.8},
                        "risk": {"risk_score": 3},
                        "market_snapshot": {"quote_source": "historical"},
                    }
                ],
            },
        )

        self.assertIn("前一实际交易日收盘数据", content)
        self.assertIn("未配置", content)
        self.assertIn("买入候选", content)

    def test_read_risk_filtered_candidates_allows_empty_and_limits_to_ten(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "风险过滤后得分前10.csv"
            pd.DataFrame(
                columns=["股票代码", "股票名称", "未满足条件（扣分项）"]
            ).to_csv(source, index=False, encoding="utf-8-sig")
            self.assertEqual(workflow.read_risk_filtered_candidates(source), [])

            pd.DataFrame(
                {
                    "股票代码": [str(index) for index in range(1, 12)],
                    "股票名称": [f"股票{index}" for index in range(1, 12)],
                    "未满足条件（扣分项）": ["无"] * 11,
                }
            ).to_csv(source, index=False, encoding="utf-8-sig")
            candidates = workflow.read_risk_filtered_candidates(source)

        self.assertEqual(len(candidates), 10)
        self.assertEqual(candidates[0], {"ticker": "000001", "name": "股票1", "deduction": "无"})
        self.assertEqual(candidates[-1]["ticker"], "000010")

    def test_screening_message_uses_only_code_name_and_deduction(self) -> None:
        content = workflow.build_screening_pushplus_html(
            "2026-07-31",
            [{"ticker": "000001", "name": "测试股票", "deduction": "量比不在范围内"}],
        )

        self.assertIn("<th>股票代码</th><th>股票名称</th><th>未满足条件（扣分项）</th>", content)
        self.assertIn("000001", content)
        self.assertIn("测试股票", content)
        self.assertIn("量比不在范围内", content)
        self.assertNotIn("持仓", content)
        self.assertNotIn("买入候选", content)
        self.assertNotIn("所属行业", content)

    def test_screening_message_reports_no_candidate_after_risk_filtering(self) -> None:
        content = workflow.build_screening_pushplus_html("2026-07-31", [])

        self.assertIn("风险过滤后无候选", content)
        self.assertNotIn("前 50", content)

    def test_run_send_uses_saved_risk_filtered_candidates_without_ai_analysis(self) -> None:
        scheduled_day = date(2026, 8, 3)
        trade_day = date(2026, 7, 31)
        logger = Mock()
        with TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            output_dir = workflow.day_output_dir(project, trade_day)
            candidate_csv = output_dir / "风险过滤后得分前10.csv"
            candidate_csv.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                {
                    "股票代码": ["000001"],
                    "股票名称": ["测试股票"],
                    "未满足条件（扣分项）": ["无"],
                }
            ).to_csv(candidate_csv, index=False, encoding="utf-8-sig")
            workflow.write_json(
                output_dir / "data_preparation.json",
                {
                    "status": "completed",
                    "trade_date": trade_day.isoformat(),
                    "risk_filtered_top_ten_csv": str(candidate_csv),
                },
            )

            with patch.object(workflow, "scheduled_target_day", return_value=trade_day):
                with patch.object(workflow, "send_pushplus") as send:
                    result = workflow.run_send(
                        indicator_project=project,
                        agent_project=project,
                        scheduled_day=scheduled_day,
                        dry_run=False,
                        force=True,
                        logger=logger,
                    )

            message = (output_dir / "pushplus_message.html").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        send.assert_called_once()
        self.assertIn("000001", message)
        self.assertNotIn("买入候选", message)

    def test_run_collect_records_empty_risk_filtered_candidate_file(self) -> None:
        scheduled_day = date(2026, 8, 3)
        trade_day = date(2026, 7, 31)
        logger = Mock()
        with TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            archive = project / "daily_trading_outputs" / "archive" / trade_day.isoformat()
            archive.mkdir(parents=True)
            (archive / workflow.TOP_FIFTY_FILE_NAME).write_text("股票代码\\n", encoding="utf-8")
            pd.DataFrame(
                columns=["股票代码", "股票名称", "未满足条件（扣分项）"]
            ).to_csv(
                archive / workflow.RISK_FILTERED_TOP_TEN_FILE_NAME,
                index=False,
                encoding="utf-8-sig",
            )
            workflow.write_json(
                archive / workflow.STATE_FILE_NAME,
                {"stages": {"after_close": {"status": "completed"}}},
            )
            runner = project / "daily_trading_runner.py"
            runner.write_text("", encoding="utf-8")
            python = project / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")

            with patch.object(workflow, "scheduled_target_day", return_value=trade_day):
                with patch.object(workflow, "_run_process", return_value=0):
                    result = workflow.run_collect(
                        indicator_project=project,
                        agent_project=project,
                        scheduled_day=scheduled_day,
                        dry_run=False,
                        logger=logger,
                    )

            with patch.object(workflow, "scheduled_target_day", return_value=trade_day):
                with patch.object(workflow, "send_pushplus") as send:
                    send_result = workflow.run_send(
                        indicator_project=project,
                        agent_project=project,
                        scheduled_day=scheduled_day,
                        dry_run=False,
                        force=True,
                        logger=logger,
                    )
                    sent_content = send.call_args.kwargs["content"]

            state = workflow.read_json(
                workflow.day_output_dir(project, trade_day) / "data_preparation.json"
            )

        self.assertEqual(result, 0)
        self.assertEqual(send_result, 0)
        send.assert_called_once()
        self.assertIn("风险过滤后无候选", sent_content)
        self.assertEqual(state["source_csv"], str(archive / workflow.TOP_FIFTY_FILE_NAME))
        self.assertEqual(
            state["risk_filtered_top_ten_csv"],
            str(archive / workflow.RISK_FILTERED_TOP_TEN_FILE_NAME),
        )
        self.assertEqual(state["candidate_count"], 0)
        self.assertTrue(state["risk_filter_enabled"])
        self.assertFalse(state["require_all_selected_conditions"])

    def test_installer_retires_ai_schedule_and_keeps_monitor_out_of_definitions(self) -> None:
        installer = (
            Path(workflow.__file__).resolve().parent / "install_daily_runner_tasks.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$retiredAiTaskName = "A-Share Top 10 AI Analysis"', installer)
        self.assertIn("function Test-RetiredAiAnalysisTask", installer)
        self.assertIn("function Remove-RetiredAiAnalysisTask", installer)
        self.assertIn('-TaskPath "\\"', installer)
        self.assertIn("T04:00:", installer)
        self.assertIn("scheduled_ashare_workflow\\.py", installer)
        self.assertIn("--mode\\s+analyze", installer)
        self.assertNotIn('New-WorkflowAction "analyze"', installer)
        self.assertNotIn('-At "04:00"', installer)
        self.assertNotIn("Register-ScheduledTask -TaskName $monitorTaskName", installer)


if __name__ == "__main__":
    unittest.main()
