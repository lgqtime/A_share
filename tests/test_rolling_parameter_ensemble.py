from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from strategy_backtest import backtest_core as core
from strategy_backtest import rolling_parameter_ensemble as ensemble


def _settings(
    *,
    pct_change: tuple[float, float] = (-2.0, 8.0),
    rsi: tuple[float, float] = (41.0, 50.1),
    turnover: tuple[float, float] = (3.0, 10.0),
    volume_ratio: tuple[float, float] = (1.0, 3.0),
    kdj: tuple[int, int] = (0, 3),
) -> dict[str, tuple[float | int, float | int]]:
    return {
        "szse_quant_filter_rsi_range": rsi,
        "szse_quant_filter_turnover_range": turnover,
        "szse_quant_filter_volume_ratio_range": volume_ratio,
        "szse_quant_filter_pct_change_range": pct_change,
        "szse_quant_filter_kdj_healthy_golden_cross_age_range": kdj,
        "szse_quant_filter_macd_dea_minus_dif_range": (0.1, 0.2),
    }


class RollingWeightTests(unittest.TestCase):
    def test_uses_raw_five_day_returns_sample_stddev_and_coverage_adjusted_weights(self) -> None:
        scores = ensemble.calculate_rolling_weights(
            {
                14: (53.6579, 53.6579, 47.6147, 57.7045, 55.9239),
                30: (150.1096, 43.6762, 202.6511, 51.6165, 93.7568),
                45: (200.1867, 161.7248, 149.3643, 143.5565, 140.5386),
            }
        )

        self.assertAlmostEqual(scores[14].mean, 53.71178)
        self.assertAlmostEqual(scores[14].sample_stddev, 3.80839463, places=8)
        self.assertAlmostEqual(scores[14].raw_sharpe, 14.10352268, places=8)
        self.assertAlmostEqual(scores[14].weight, 0.4992181451, places=10)
        self.assertAlmostEqual(scores[30].weight, 0.0742562824, places=10)
        self.assertAlmostEqual(scores[45].weight, 0.4265255725, places=10)
        self.assertAlmostEqual(sum(score.weight for score in scores.values()), 1.0)

    def test_relu_excludes_negative_sharpe_before_linear_normalization(self) -> None:
        scores = ensemble.calculate_rolling_weights(
            {
                14: (-5.0, -4.0, -3.0, -2.0, -1.0),
                30: (1.0, 2.0, 3.0, 4.0, 5.0),
                45: (1.0, 2.0, 3.0, 4.0, 5.0),
            }
        )

        self.assertLess(scores[14].raw_sharpe, 0.0)
        self.assertEqual(scores[14].relu_sharpe, 0.0)
        self.assertEqual(scores[14].weight, 0.0)
        self.assertAlmostEqual(scores[30].weight, 0.4146341463, places=10)
        self.assertAlmostEqual(scores[45].weight, 0.5853658537, places=10)

    def test_uses_equal_weights_when_all_scores_are_zero(self) -> None:
        scores = ensemble.calculate_rolling_weights(
            {
                14: (2.0, 2.0, 2.0, 2.0, 2.0),
                30: (-2.0, -2.0, -2.0, -2.0, -2.0),
                45: (0.0, 0.0, 0.0, 0.0, 0.0),
            }
        )

        self.assertEqual(scores[14].raw_sharpe, 0.0)
        self.assertEqual(scores[30].raw_sharpe, 0.0)
        self.assertEqual(scores[45].raw_sharpe, 0.0)
        self.assertEqual(
            {score.weight for score in scores.values()}, {1.0 / 3.0}
        )

    def test_penalizes_any_guarded_parameter_range_outside_its_bounds(self) -> None:
        settings_by_violation = {
            "szse_quant_filter_pct_change_range": _settings(
                pct_change=(-5.6, 10.5)
            ),
            "szse_quant_filter_rsi_range": _settings(rsi=(19.9, 68.0)),
            "szse_quant_filter_turnover_range": _settings(turnover=(2.5, 10.6)),
            "szse_quant_filter_volume_ratio_range": _settings(
                volume_ratio=(0.7, 7.0)
            ),
        }
        returns_by_window = {
            14: (1.0, 2.0, 3.0, 4.0, 5.0),
            30: (1.0, 2.0, 3.0, 4.0, 5.0),
            45: (1.0, 2.0, 3.0, 4.0, 5.0),
        }

        for setting_key, violating_settings in settings_by_violation.items():
            with self.subTest(setting_key=setting_key):
                scores = ensemble.calculate_rolling_weights(
                    returns_by_window,
                    {
                        14: violating_settings,
                        30: _settings(),
                        45: _settings(),
                    },
                )

                self.assertEqual(scores[14].range_penalty_multiplier, 0.1)
                self.assertEqual(scores[14].out_of_range_setting_keys, (setting_key,))
                self.assertAlmostEqual(scores[14].weight, 0.0307328605, places=10)
                self.assertAlmostEqual(sum(score.weight for score in scores.values()), 1.0)

    def test_does_not_penalize_ranges_at_inclusive_guard_boundaries(self) -> None:
        scores = ensemble.calculate_rolling_weights(
            {
                14: (1.0, 2.0, 3.0, 4.0, 5.0),
                30: (1.0, 2.0, 3.0, 4.0, 5.0),
                45: (1.0, 2.0, 3.0, 4.0, 5.0),
            },
            {
                14: _settings(
                    pct_change=(-5.5, 10.5),
                    rsi=(20.0, 100.0),
                    turnover=(2.5, 10.5),
                    volume_ratio=(1.0, 7.0),
                ),
                30: _settings(),
                45: _settings(),
            },
        )

        self.assertEqual(scores[14].range_penalty_multiplier, 1.0)
        self.assertEqual(scores[14].out_of_range_setting_keys, ())
        self.assertAlmostEqual(scores[14].weight, 0.2407407407, places=10)

    def test_requires_exactly_five_finite_returns_per_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "5"):
            ensemble.calculate_rolling_weights(
                {
                    14: (1.0, 2.0, 3.0, 4.0),
                    30: (1.0, 2.0, 3.0, 4.0, 5.0),
                    45: (1.0, 2.0, 3.0, 4.0, 5.0),
                }
            )


class ParameterBlendingTests(unittest.TestCase):
    def test_blends_float_ranges_and_rounds_kdj_outward(self) -> None:
        blended = ensemble.blend_tunable_settings(
            {
                14: _settings(rsi=(40.1, 50.1), kdj=(0, 3)),
                30: _settings(rsi=(41.1, 51.1), kdj=(2, 4)),
                45: _settings(rsi=(42.1, 52.1), kdj=(5, 8)),
            },
            {14: 0.2, 30: 0.3, 45: 0.5},
        )

        self.assertEqual(
            blended["szse_quant_filter_kdj_healthy_golden_cross_age_range"],
            (3, 6),
        )
        self.assertAlmostEqual(
            blended["szse_quant_filter_rsi_range"][0], 41.4
        )
        self.assertAlmostEqual(
            blended["szse_quant_filter_rsi_range"][1], 51.4
        )


class VerificationDateTests(unittest.TestCase):
    def _return_data(self) -> core.ReturnData:
        signal_days = tuple(
            date(2026, 8, day_number)
            for day_number in (3, 4, 5, 6, 7)
        )
        return core.ReturnData(
            signal_dates=signal_days,
            next_trade_dates={
                date(2026, 8, 3): date(2026, 8, 4),
                date(2026, 8, 4): date(2026, 8, 5),
                date(2026, 8, 5): date(2026, 8, 6),
                date(2026, 8, 6): date(2026, 8, 7),
                date(2026, 8, 7): date(2026, 8, 10),
            },
            strict_returns={},
        )

    def test_selects_the_latest_five_completed_verification_days(self) -> None:
        selected = ensemble.select_completed_verification_dates(
            self._return_data(),
            as_of_date=date(2026, 8, 11),
        )

        self.assertEqual(
            selected,
            (
                date(2026, 8, 4),
                date(2026, 8, 5),
                date(2026, 8, 6),
                date(2026, 8, 7),
                date(2026, 8, 10),
            ),
        )

    def test_rejects_insufficient_completed_verification_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "5"):
            ensemble.select_completed_verification_dates(
                self._return_data(),
                as_of_date=date(2026, 8, 7),
            )


class SnapshotAndPublicationTests(unittest.TestCase):
    def _snapshot(
        self,
        day: date,
        window: int,
        total_return_pct: float,
    ) -> ensemble.WindowSnapshot:
        return ensemble.WindowSnapshot(
            as_of_date=day,
            verification_date=day,
            lookback_days=window,
            best_settings=_settings(),
            total_return_pct=total_return_pct,
            source_report=f"window_{window}_{day.isoformat()}.json",
            data_window={
                "last_verification_date": day.isoformat(),
                "lookback_signal_days": window,
            },
        )

    def test_reuses_existing_window_snapshot_without_running_optimizer(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            existing = self._snapshot(target_day, 14, 53.6579)
            ensemble.save_window_snapshot(output_dir, existing)
            calls: list[list[str]] = []

            def unexpected_optimizer(arguments: list[str]) -> int:
                calls.append(arguments)
                return 1

            loaded = ensemble.ensure_window_snapshot(
                output_dir=output_dir,
                as_of_date=target_day,
                lookback_days=14,
                optimizer_arguments=["--as-of-date", target_day.isoformat()],
                optimizer_main=unexpected_optimizer,
            )

        self.assertEqual(loaded, existing)
        self.assertEqual(calls, [])

    def test_reused_snapshot_repairs_a_missing_return_history_record(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            existing = self._snapshot(target_day, 14, 53.6579)
            ensemble.save_window_snapshot(output_dir, existing)

            ensemble.ensure_window_snapshot(
                output_dir=output_dir,
                as_of_date=target_day,
                lookback_days=14,
                optimizer_arguments=[],
                optimizer_main=lambda _arguments: 1,
            )

            history = ensemble.load_return_history(output_dir)

        self.assertEqual(history[target_day][14], 53.6579)

    def test_rejects_conflicting_window_snapshot_without_overwriting_history(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            original = self._snapshot(target_day, 14, 53.6579)
            conflicting = self._snapshot(target_day, 14, 99.0)
            path = ensemble.save_window_snapshot(output_dir, original)

            with self.assertRaisesRegex(ValueError, "覆盖"):
                ensemble.save_window_snapshot(output_dir, conflicting)

            persisted = ensemble.load_window_snapshot(output_dir, target_day, 14)

        self.assertEqual(persisted, original)
        self.assertTrue(path.name.endswith("lookback_14.json"))

    def test_preserves_distinct_input_versions_of_the_same_window_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            original = self._snapshot(target_day, 14, 53.6579)
            first = replace(original, input_fingerprint="first-input")
            revised = replace(
                original,
                total_return_pct=99.0,
                input_fingerprint="revised-input",
            )

            first_path = ensemble.save_window_snapshot(output_dir, first)
            revised_path = ensemble.save_window_snapshot(output_dir, revised)

            loaded_first = ensemble.load_window_snapshot(
                output_dir,
                target_day,
                14,
                input_fingerprint="first-input",
            )
            loaded_revised = ensemble.load_window_snapshot(
                output_dir,
                target_day,
                14,
                input_fingerprint="revised-input",
            )

        self.assertNotEqual(first_path, revised_path)
        self.assertEqual(loaded_first, first)
        self.assertEqual(loaded_revised, revised)

    def test_rejects_conflicting_snapshot_for_the_same_input_version(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            original = replace(
                self._snapshot(target_day, 14, 53.6579),
                input_fingerprint="same-input",
            )
            conflicting = replace(original, total_return_pct=99.0)
            ensemble.save_window_snapshot(output_dir, original)

            with self.assertRaisesRegex(ValueError, "覆盖"):
                ensemble.save_window_snapshot(output_dir, conflicting)

            persisted = ensemble.load_window_snapshot(
                output_dir,
                target_day,
                14,
                input_fingerprint="same-input",
            )

        self.assertEqual(persisted, original)

    def test_requires_every_expected_trading_day_before_publishing(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            expected_days = tuple(
                date(2026, 8, day_number)
                for day_number in (3, 4, 5, 6, 7)
            )
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            dated_path = output_dir / (
                f"{ensemble.optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            current_path.write_bytes(b"existing-current")
            dated_path.write_bytes(b"existing-dated")
            for day in expected_days:
                for window in ensemble.ENSEMBLE_WINDOWS:
                    if day == date(2026, 8, 6) and window == 30:
                        continue
                    snapshot = self._snapshot(day, window, float(window + day.day))
                    ensemble.save_window_snapshot(output_dir, snapshot)
                    ensemble.record_return(output_dir, snapshot)

            result = ensemble.publish_ensemble_if_ready(
                output_dir=output_dir,
                as_of_date=target_day,
                verification_dates=expected_days,
            )

            self.assertFalse(result.published)
            self.assertEqual(current_path.read_bytes(), b"existing-current")
            self.assertEqual(dated_path.read_bytes(), b"existing-dated")

    def test_publishes_canonical_files_only_after_full_history_is_available(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            expected_days = tuple(
                date(2026, 8, day_number)
                for day_number in (3, 4, 5, 6, 7)
            )
            returns = {
            14: (53.6579, 53.6579, 47.6147, 57.7045, 55.9239),
            30: (150.1096, 43.6762, 202.6511, 51.6165, 93.7568),
            45: (200.1867, 161.7248, 149.3643, 143.5565, 140.5386),
            }
            for position, day in enumerate(expected_days):
                for window in ensemble.ENSEMBLE_WINDOWS:
                    snapshot = self._snapshot(day, window, returns[window][position])
                    ensemble.save_window_snapshot(output_dir, snapshot)
                    ensemble.record_return(output_dir, snapshot)

            result = ensemble.publish_ensemble_if_ready(
                output_dir=output_dir,
                as_of_date=target_day,
                verification_dates=expected_days,
            )
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            dated_path = output_dir / (
                f"{ensemble.optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
            )
            payload = json.loads(current_path.read_text(encoding="utf-8"))
            dated_exists = dated_path.is_file()

        self.assertTrue(result.published)
        self.assertEqual(payload["run_date"], "2026-08-07")
        self.assertEqual(payload["parameter_source"], "rolling_parameter_ensemble")
        self.assertAlmostEqual(payload["ensemble"]["weights"]["14"], 0.4992181451)
        self.assertEqual(payload["ensemble"]["scores"]["14"]["coverage_factor"], 0.65)
        self.assertEqual(
            payload["ensemble"]["scores"]["14"]["out_of_range_setting_keys"],
            [],
        )
        self.assertEqual(
            payload["ensemble"]["scores"]["14"]["range_penalty_multiplier"],
            1.0,
        )
        self.assertGreater(
            payload["ensemble"]["scores"]["45"]["coverage_adjusted_score"],
            payload["ensemble"]["scores"]["30"]["coverage_adjusted_score"],
        )
        self.assertTrue(dated_exists)

    def test_does_not_replace_a_newer_current_parameter_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            expected_days = tuple(
                date(2026, 8, day_number)
                for day_number in (3, 4, 5, 6, 7)
            )
            for day in expected_days:
                for window in ensemble.ENSEMBLE_WINDOWS:
                    snapshot = self._snapshot(day, window, float(window + day.day))
                    ensemble.save_window_snapshot(output_dir, snapshot)
                    ensemble.record_return(output_dir, snapshot)
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            newer_current = b'{"run_date":"2026-08-10","best_settings":{}}'
            current_path.write_bytes(newer_current)

            result = ensemble.publish_ensemble_if_ready(
                output_dir=output_dir,
                as_of_date=target_day,
                verification_dates=expected_days,
            )

            persisted = current_path.read_bytes()

        self.assertFalse(result.published)
        self.assertIn("更新", result.reason or "")
        self.assertEqual(persisted, newer_current)

    def test_replaces_a_single_window_current_file_with_the_same_verification_day(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 7)
            expected_days = tuple(
                date(2026, 8, day_number)
                for day_number in (3, 4, 5, 6, 7)
            )
            for day in expected_days:
                for window in ensemble.ENSEMBLE_WINDOWS:
                    snapshot = self._snapshot(day, window, float(window + day.day))
                    ensemble.save_window_snapshot(output_dir, snapshot)
                    ensemble.record_return(output_dir, snapshot)
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            current_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-08-08",
                        "data_window": {
                            "last_verification_date": target_day.isoformat(),
                        },
                        "best_settings": _settings(),
                    }
                ),
                encoding="utf-8",
            )

            result = ensemble.publish_ensemble_if_ready(
                output_dir=output_dir,
                as_of_date=target_day,
                verification_dates=expected_days,
            )
            payload = json.loads(current_path.read_text(encoding="utf-8"))

        self.assertTrue(result.published)
        self.assertEqual(payload["parameter_source"], "rolling_parameter_ensemble")


class EnsembleCoordinatorTests(unittest.TestCase):
    def _return_data(self) -> core.ReturnData:
        return core.ReturnData(
            signal_dates=(
                date(2026, 8, 3),
                date(2026, 8, 4),
                date(2026, 8, 5),
                date(2026, 8, 6),
                date(2026, 8, 7),
            ),
            next_trade_dates={
                date(2026, 8, 3): date(2026, 8, 4),
                date(2026, 8, 4): date(2026, 8, 5),
                date(2026, 8, 5): date(2026, 8, 6),
                date(2026, 8, 6): date(2026, 8, 7),
                date(2026, 8, 7): date(2026, 8, 10),
            },
            strict_returns={},
        )

    def _record_history_before_target(self, output_dir: Path) -> None:
        returns = {
            14: (53.6579, 47.6147, 57.7045, 55.9239),
            30: (43.6762, 202.6511, 51.6165, 93.7568),
            45: (161.7248, 149.3643, 143.5565, 140.5386),
        }
        history_days = tuple(
            date(2026, 8, day_number) for day_number in (4, 5, 6, 7)
        )
        for position, day in enumerate(history_days):
            for window in ensemble.ENSEMBLE_WINDOWS:
                ensemble.record_return(
                    output_dir,
                    ensemble.WindowSnapshot(
                        as_of_date=day,
                        verification_date=day,
                        lookback_days=window,
                        best_settings=_settings(),
                        total_return_pct=returns[window][position],
                        source_report=f"history-{window}-{day.isoformat()}.json",
                        data_window={},
                    ),
                )

    @staticmethod
    def _argument(arguments: list[str], option: str) -> str:
        index = arguments.index(option)
        return arguments[index + 1]

    def _write_optimizer_report(
        self,
        arguments: list[str],
        total_return_pct: float,
    ) -> None:
        output_dir = Path(self._argument(arguments, "--output-dir"))
        target_day = date.fromisoformat(self._argument(arguments, "--as-of-date"))
        lookback_days = int(self._argument(arguments, "--lookback-days"))
        report_path = output_dir / (
            f"{ensemble.optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "run_date": target_day.isoformat(),
                    "best_settings": _settings(),
                    "best_result": {"total_return_pct": total_return_pct},
                    "data_window": {
                        "last_verification_date": target_day.isoformat(),
                        "lookback_signal_days": lookback_days,
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_runs_each_window_in_an_isolated_directory_and_publishes_latest_day(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            self._record_history_before_target(output_dir)
            returns_workbook = output_dir / "returns.xlsx"
            stock_pool = output_dir / "pool.csv"
            returns_workbook.write_bytes(b"returns")
            stock_pool.write_bytes(b"stock-pool")
            calls: list[list[str]] = []

            def fake_optimizer(arguments: list[str]) -> int:
                calls.append(arguments)
                lookback_days = int(self._argument(arguments, "--lookback-days"))
                self._write_optimizer_report(arguments, float(lookback_days))
                return 0

            with patch.object(
                ensemble.core,
                "load_strict_next_day_returns",
                return_value=self._return_data(),
            ):
                result = ensemble.run_ensemble_update(
                    returns_workbook=returns_workbook,
                    stock_pool=stock_pool,
                    output_dir=output_dir,
                    as_of_date=date(2026, 8, 11),
                    optimizer_main=fake_optimizer,
                )

            payload = json.loads(
                (
                    output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
                ).read_text(encoding="utf-8")
            )

        self.assertTrue(result.published)
        self.assertEqual(payload["run_date"], "2026-08-10")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            {self._argument(arguments, "--lookback-days") for arguments in calls},
            {"14", "30", "45"},
        )
        self.assertEqual(
            {self._argument(arguments, "--as-of-date") for arguments in calls},
            {"2026-08-10"},
        )
        self.assertEqual(
            {
                Path(self._argument(arguments, "--output-dir")).name
                for arguments in calls
            },
            {"lookback_14", "lookback_30", "lookback_45"},
        )

    def test_backfills_missing_history_for_all_required_verification_days(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            returns_workbook = output_dir / "returns.xlsx"
            stock_pool = output_dir / "pool.csv"
            returns_workbook.write_bytes(b"returns")
            stock_pool.write_bytes(b"stock-pool")
            calls: list[list[str]] = []

            def fake_optimizer(arguments: list[str]) -> int:
                calls.append(arguments)
                lookback_days = int(self._argument(arguments, "--lookback-days"))
                self._write_optimizer_report(arguments, float(lookback_days))
                return 0

            with patch.object(
                ensemble.core,
                "load_strict_next_day_returns",
                return_value=self._return_data(),
            ):
                result = ensemble.run_ensemble_update(
                    returns_workbook=returns_workbook,
                    stock_pool=stock_pool,
                    output_dir=output_dir,
                    as_of_date=date(2026, 8, 11),
                    optimizer_main=fake_optimizer,
                )

        self.assertTrue(result.published)
        self.assertEqual(len(calls), len(ensemble.ENSEMBLE_WINDOWS) * 5)
        self.assertEqual(
            {self._argument(arguments, "--as-of-date") for arguments in calls},
            {"2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"},
        )
        self.assertEqual(
            {int(self._argument(arguments, "--lookback-days")) for arguments in calls},
            set(ensemble.ENSEMBLE_WINDOWS),
        )

    def test_window_failure_leaves_canonical_parameter_files_unchanged(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            self._record_history_before_target(output_dir)
            returns_workbook = output_dir / "returns.xlsx"
            stock_pool = output_dir / "pool.csv"
            returns_workbook.write_bytes(b"returns")
            stock_pool.write_bytes(b"stock-pool")
            target_day = date(2026, 8, 10)
            output_dir.mkdir(parents=True, exist_ok=True)
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            dated_path = output_dir / (
                f"{ensemble.optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
            )
            current_path.write_bytes(b"previous-current")
            dated_path.write_bytes(b"previous-dated")

            def failing_optimizer(arguments: list[str]) -> int:
                lookback_days = int(self._argument(arguments, "--lookback-days"))
                if lookback_days == 30:
                    return 1
                self._write_optimizer_report(arguments, float(lookback_days))
                return 0

            with (
                patch.object(
                    ensemble.core,
                    "load_strict_next_day_returns",
                    return_value=self._return_data(),
                ),
                self.assertRaisesRegex(RuntimeError, "30"),
            ):
                ensemble.run_ensemble_update(
                    returns_workbook=returns_workbook,
                    stock_pool=stock_pool,
                    output_dir=output_dir,
                    as_of_date=target_day,
                    optimizer_main=failing_optimizer,
                )

            self.assertEqual(current_path.read_bytes(), b"previous-current")
            self.assertEqual(dated_path.read_bytes(), b"previous-dated")

    def test_archives_each_input_version_before_the_window_report_can_change(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            target_day = date(2026, 8, 10)
            arguments = [
                "--output-dir",
                str(output_dir),
                "--as-of-date",
                target_day.isoformat(),
                "--lookback-days",
                "14",
            ]
            returns = iter((1.0, 2.0))

            def fake_optimizer(current_arguments: list[str]) -> int:
                self._write_optimizer_report(
                    current_arguments,
                    next(returns),
                )
                return 0

            first = ensemble.ensure_window_snapshot(
                output_dir=output_dir,
                as_of_date=target_day,
                lookback_days=14,
                optimizer_arguments=arguments,
                optimizer_main=fake_optimizer,
                input_fingerprint="first-input",
            )
            revised = ensemble.ensure_window_snapshot(
                output_dir=output_dir,
                as_of_date=target_day,
                lookback_days=14,
                optimizer_arguments=arguments,
                optimizer_main=fake_optimizer,
                input_fingerprint="revised-input",
            )

            first_report = json.loads(
                Path(first.source_report).read_text(encoding="utf-8")
            )
            revised_report = json.loads(
                Path(revised.source_report).read_text(encoding="utf-8")
            )

        self.assertNotEqual(first.source_report, revised.source_report)
        self.assertEqual(first_report["best_result"]["total_return_pct"], 1.0)
        self.assertEqual(revised_report["best_result"]["total_return_pct"], 2.0)

    def test_import_mode_only_seeds_return_cache(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            matrix_path = output_dir / "returns.csv"
            matrix_path.write_text(
                "交易窗口（天）,2026-08-07\n"
                "14,53.6579\n"
                "30,150.1096\n"
                "45,200.1867\n",
                encoding="utf-8-sig",
            )

            exit_code = ensemble.main(
                [
                    "--output-dir",
                    str(output_dir),
                    "--import-return-matrix",
                    str(matrix_path),
                ]
            )

            history = ensemble.load_return_history(output_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(history[date(2026, 8, 7)][14], 53.6579)

    def test_main_returns_failure_when_fusion_is_not_published(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            current_path = output_dir / ensemble.optimizer.CURRENT_PARAMETER_FILENAME
            current_path.parent.mkdir(parents=True, exist_ok=True)
            current_path.write_text("{}", encoding="utf-8")
            with patch.object(
                ensemble,
                "run_ensemble_update",
                return_value=ensemble.EnsemblePublication(False, "历史不足"),
            ):
                exit_code = ensemble.main(
                    [
                        "--returns-workbook",
                        str(output_dir / "returns.xlsx"),
                        "--stock-pool",
                        str(output_dir / "pool.csv"),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(exit_code, 1)


class ReturnMatrixImportTests(unittest.TestCase):
    def test_imports_bom_encoded_raw_returns_for_all_windows(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            matrix_path = root / "returns.csv"
            matrix_path.write_text(
                "交易窗口（天）,2026-08-07,2026-08-06\n"
                "14,53.6579,53.6579\n"
                "30,150.1096,43.6762\n"
                "45,200.1867,161.7248\n",
                encoding="utf-8-sig",
            )
            imported_count = ensemble.import_return_matrix(matrix_path, root)
            history = ensemble.load_return_history(root)

        self.assertEqual(imported_count, 6)
        self.assertEqual(history[date(2026, 8, 7)][14], 53.6579)
        self.assertEqual(history[date(2026, 8, 6)][45], 161.7248)


if __name__ == "__main__":
    unittest.main()
