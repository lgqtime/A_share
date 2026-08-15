from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_DIR / "parameter_window_analysis" / "analyze_windows.py"


class AnalyzeWindowsCliTests(unittest.TestCase):
    def test_dry_run_lists_every_requested_lookback_window(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--dry-run"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        planned_windows = [
            line for line in completed.stdout.splitlines() if line.startswith("lookback_days=")
        ]
        self.assertEqual(len(planned_windows), 26)
        self.assertEqual(planned_windows[0], "lookback_days=30")
        self.assertEqual(planned_windows[-1], "lookback_days=5")

    def test_summarize_only_reuses_a_completed_window(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            run_dir = output_dir / "optimization_runs" / "lookback_05_days"
            run_dir.mkdir(parents=True)
            (run_dir / "rolling_parameter_optimization_2026-08-06.json").write_text(
                json.dumps(
                    {
                        "best_settings": {
                            "szse_quant_filter_rsi_range": [45.0, 60.0]
                        },
                        "best_result": {
                            "prediction_days": 4,
                            "correct_days": 3,
                            "accuracy_pct": 75.0,
                            "total_return_pct": 10.0,
                        },
                        "data_window": {
                            "lookback_signal_days": 5,
                            "first_signal_date": "2026-07-30",
                            "last_signal_date": "2026-08-05",
                            "last_verification_date": "2026-08-06",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--lookback-start",
                    "5",
                    "--lookback-end",
                    "5",
                    "--output-dir",
                    str(output_dir),
                    "--summarize-only",
                ],
                cwd=PROJECT_DIR,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = (output_dir / "窗口收益汇总.csv").read_text(encoding="utf-8-sig")
            self.assertIn("5", summary)
            self.assertIn("10.0", summary)


if __name__ == "__main__":
    unittest.main()
