from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import run_optimizer_lookback_sweep as sweep


class OptimizerLookbackSweepTests(unittest.TestCase):
    def test_select_as_of_dates_returns_latest_dates_first(self) -> None:
        next_trade_dates = {
            date(2026, 8, 3): date(2026, 8, 4),
            date(2026, 8, 4): date(2026, 8, 5),
            date(2026, 8, 5): date(2026, 8, 6),
        }

        selected = sweep.select_as_of_dates(next_trade_dates, count=2)

        self.assertEqual(selected, (date(2026, 8, 6), date(2026, 8, 5)))

    def test_run_matrix_writes_return_table_by_window_and_end_date(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            lookback_days = int(command[command.index("--lookback-days") + 1])
            as_of_date = command[command.index("--as-of-date") + 1]
            total_return = lookback_days + int(as_of_date[-2:]) / 100
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"结果：{lookback_days}/{lookback_days}，总收益率"
                    f"{total_return:.2f}%，正确率100.00%\n"
                ),
                stderr="",
            )

        with TemporaryDirectory() as temporary_directory:
            matrix_path, detail_path, rows = sweep.run_matrix(
                start=8,
                end=9,
                as_of_dates=(date(2026, 8, 6), date(2026, 8, 5)),
                output_dir=Path(temporary_directory),
                runner=fake_runner,
                now=datetime(2026, 8, 10, 17, 0, 0),
            )

            self.assertEqual(len(calls), 4)
            self.assertEqual(len(rows), 4)
            self.assertTrue(detail_path.is_file())
            with matrix_path.open(encoding="utf-8-sig", newline="") as file:
                matrix_rows = list(csv.reader(file))

        self.assertEqual(matrix_rows[0], ["交易窗口（天）", "2026-08-06", "2026-08-05"])
        self.assertEqual(matrix_rows[1], ["8", "8.06", "8.05"])
        self.assertEqual(matrix_rows[2], ["9", "9.06", "9.05"])

    def test_run_command_decodes_gb18030_optimizer_output(self) -> None:
        child_code = (
            "import sys; "
            "text = ''.join(map(chr, [32467, 26524, 65306, 55, 47, 55, 65292, "
            "24635, 25910, 30410, 29575, 55, 46, 50, 53, 37, 65292, 27491, 30830, "
            "29575, 49, 48, 48, 46, 48, 48, 37])); "
            "sys.stdout.buffer.write(text.encode('gb18030'))"
        )

        completed = sweep.run_command(
            [sys.executable, "-c", child_code],
            cwd=sweep.PROJECT_DIR,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stdout,
            "结果：7/7，总收益率7.25%，正确率100.00%",
        )
        self.assertEqual(
            sweep.parse_result_line(completed.stdout),
            {
                "correct_days": 7,
                "prediction_days": 7,
                "total_return_pct": 7.25,
                "accuracy_pct": 100.0,
                "result_line": "结果：7/7，总收益率7.25%，正确率100.00%",
            },
        )

    def test_parse_result_line_extracts_optimizer_metrics(self) -> None:
        output = "最优参数：...\n结果：7/7，总收益率36.0283%，正确率100.00%\n参数文件：..."

        result = sweep.parse_result_line(output)

        self.assertEqual(
            result,
            {
                "correct_days": 7,
                "prediction_days": 7,
                "total_return_pct": 36.0283,
                "accuracy_pct": 100.0,
                "result_line": "结果：7/7，总收益率36.0283%，正确率100.00%",
            },
        )

    def test_run_sweep_writes_each_log_and_a_summary_csv(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            lookback_days = int(command[-1])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"执行 {lookback_days} 日窗口\n"
                    f"结果：{lookback_days}/{lookback_days}，总收益率"
                    f"{lookback_days}.25%，正确率100.00%\n"
                ),
                stderr="",
            )

        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            summary_path, rows = sweep.run_sweep(
                start=7,
                end=8,
                output_dir=output_dir,
                runner=fake_runner,
                now=datetime(2026, 8, 10, 16, 45, 0),
            )

            self.assertEqual([command[-1] for command in calls], ["7", "8"])
            self.assertEqual(len(rows), 2)
            self.assertTrue((summary_path.parent / "lookback_07.log").is_file())
            self.assertTrue((summary_path.parent / "lookback_08.log").is_file())
            with summary_path.open(encoding="utf-8-sig", newline="") as file:
                summary_rows = list(csv.DictReader(file))

        self.assertEqual(summary_rows[0]["total_return_pct"], "7.25")
        self.assertEqual(summary_rows[1]["prediction_days"], "8")
        self.assertEqual(summary_rows[1]["exit_code"], "0")


if __name__ == "__main__":
    unittest.main()
