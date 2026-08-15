from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

import pandas as pd

import daily_trading_runner as runner


class DailyTradingRunnerTests(unittest.TestCase):
    @contextmanager
    def managed_logger(self, paths: runner.PipelinePaths):
        logger = runner.configure_logger(paths, runner.china_now())
        try:
            yield logger
        finally:
            runner.close_logger(logger)

    def test_after_close_recovery_uses_the_previous_weekday_before_close(self) -> None:
        before_close = datetime(2026, 7, 28, 9, 0, tzinfo=runner.CHINA_TIMEZONE)
        after_close = datetime(2026, 7, 28, 17, 0, tzinfo=runner.CHINA_TIMEZONE)

        self.assertEqual(runner.resolve_after_close_day(before_close, None), date(2026, 7, 27))
        self.assertEqual(runner.resolve_after_close_day(after_close, None), date(2026, 7, 28))

    def test_atomic_workbook_write_preserves_interrupt_when_cleanup_is_locked(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "returns.xlsx"
            target.write_bytes(b"current")

            def interrupted_writer(temporary_path: Path) -> None:
                temporary_path.write_bytes(b"partial")
                raise KeyboardInterrupt

            with patch.object(Path, "unlink", side_effect=PermissionError("busy")):
                with self.assertRaises(KeyboardInterrupt):
                    runner.atomic_write_workbook(target, interrupted_writer)

            self.assertEqual(target.read_bytes(), b"current")

    def test_atomic_workbook_write_uses_a_unique_temporary_name(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "returns.xlsx"
            legacy_temporary = Path(temporary_directory) / "returns.tmp.xlsx"
            legacy_temporary.write_bytes(b"legacy")
            seen_paths: list[Path] = []

            def writer(temporary_path: Path) -> None:
                seen_paths.append(temporary_path)
                temporary_path.write_bytes(b"replacement")

            runner.atomic_write_workbook(target, writer)

            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(legacy_temporary.read_bytes(), b"legacy")
            self.assertEqual(len(seen_paths), 1)
            self.assertNotEqual(seen_paths[0], legacy_temporary)
            self.assertEqual(seen_paths[0].suffix, ".xlsx")

    def test_atomic_json_write_normalizes_non_finite_numbers(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "state.json"

            runner.atomic_write_json(
                target,
                {
                    "nan": float("nan"),
                    "nested": {"positive": float("inf"), "negative": float("-inf")},
                    "values": [1, float("nan")],
                },
            )

            content = target.read_text(encoding="utf-8")
            self.assertNotIn("NaN", content)
            self.assertNotIn("Infinity", content)
            self.assertEqual(
                json.loads(content),
                {
                    "nan": None,
                    "nested": {"positive": None, "negative": None},
                    "values": [1, None],
                },
            )

    def test_return_failure_companies_are_excluded_from_daily_ranking(self) -> None:
        companies = pd.DataFrame(
            {
                "股票代码": ["000001", "000002", "000003"],
                "股票名称": ["甲", "乙", "丙"],
            }
        )
        failures = pd.DataFrame({"股票代码": ["2", "000003", "invalid"]})

        codes = runner._return_failure_codes(failures)
        remaining = runner._exclude_return_failure_companies(companies, codes)

        self.assertEqual(codes, {"000002", "000003"})
        self.assertEqual(remaining["股票代码"].tolist(), ["000001"])

    def test_stock_pool_refresh_uses_recent_validated_pool_when_szse_is_unavailable(self) -> None:
        target_day = runner.china_now().date()
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"previous-stock-pool")
            logger = Mock()
            with (
                patch.object(
                    runner.fetch_szse_data,
                    "main",
                    side_effect=runner.fetch_szse_data.SzseApiError("connection failed"),
                ),
                patch.object(
                    runner.screening,
                    "load_mainboard_companies",
                    return_value=pd.DataFrame({"股票代码": ["000001"]}),
                ) as validate_pool,
                patch.object(runner.intraday, "get_token", return_value="test-token"),
                patch.object(
                    runner.fetch_zhitu_stock_pool,
                    "refresh_mainboard_company_frame",
                    side_effect=runner.fetch_zhitu_stock_pool.ZhituStockPoolError(
                        "instrument unavailable"
                    ),
                ) as refresh_mainboard_company_frame,
            ):
                result = runner.refresh_stock_pool(paths, target_day=target_day, logger=logger)

            self.assertFalse(result.refreshed)
            self.assertEqual(result.source, "local")
            self.assertEqual(result.source_day, target_day)
            self.assertEqual(paths.stock_pool.read_bytes(), b"previous-stock-pool")
            validate_pool.assert_called_once_with(paths.stock_pool)
            refresh_mainboard_company_frame.assert_called_once()
            self.assertEqual(
                refresh_mainboard_company_frame.call_args.args[:2],
                (validate_pool.return_value, "test-token"),
            )
            logger.warning.assert_called_once()

    def test_stock_pool_refresh_uses_zhitu_when_szse_is_unavailable(self) -> None:
        target_day = runner.china_now().date()
        reference_companies = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "股票名称": ["参考名称"],
                "所属行业": ["参考行业"],
            }
        )
        zhitu_build = runner.fetch_zhitu_stock_pool.ZhituStockPoolBuild(
            company_frame=pd.DataFrame(
                {
                    "公司代码": ["000001"],
                    "公司简称": ["智图名称"],
                    "所属行业": ["参考行业"],
                }
            ),
            successful_codes=("000001",),
            failed_codes=("000002",),
            unknown_industry_codes=("000003",),
            request_count=2,
        )
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"previous-stock-pool")

            def write_fallback(
                _reference_path: Path,
                _build: runner.fetch_zhitu_stock_pool.ZhituStockPoolBuild,
                output_path: Path,
            ) -> None:
                output_path.write_bytes(b"zhitu-stock-pool")

            with (
                patch.object(
                    runner.fetch_szse_data,
                    "main",
                    side_effect=runner.fetch_szse_data.SzseApiError("connection failed"),
                ),
                patch.object(
                    runner.screening,
                    "load_mainboard_companies",
                    return_value=reference_companies,
                ) as validate_pool,
                patch.object(runner.intraday, "get_token", return_value="test-token"),
                patch.object(
                    runner.fetch_zhitu_stock_pool,
                    "refresh_mainboard_company_frame",
                    return_value=zhitu_build,
                ) as refresh_mainboard_company_frame,
                patch.object(
                    runner.fetch_zhitu_stock_pool,
                    "write_fallback_workbook",
                    side_effect=write_fallback,
                ),
            ):
                result = runner.refresh_stock_pool(
                    paths,
                    target_day=target_day,
                    logger=Mock(),
                )

            self.assertTrue(result.refreshed)
            self.assertEqual(result.source, "zhitu")
            self.assertEqual(result.unknown_industry_codes, ("000003",))
            self.assertEqual(result.failed_zhitu_codes, ("000002",))
            self.assertEqual(result.zhitu_request_count, 2)
            self.assertEqual(paths.stock_pool.read_bytes(), b"zhitu-stock-pool")
            refresh_mainboard_company_frame.assert_called_once()
            self.assertEqual(
                refresh_mainboard_company_frame.call_args.args[:2],
                (reference_companies, "test-token"),
            )
            self.assertEqual(validate_pool.call_count, 2)
            self.assertIn("智图", runner._stock_pool_refresh_message(result))

    def test_stock_pool_refresh_rejects_an_overdue_fallback(self) -> None:
        target_day = runner.china_now().date()
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"old-stock-pool")
            old_timestamp = datetime.combine(
                target_day - timedelta(days=runner.MAX_STOCK_POOL_FALLBACK_AGE_DAYS + 1),
                datetime.min.time(),
                tzinfo=runner.CHINA_TIMEZONE,
            ).timestamp()
            os.utime(paths.stock_pool, (old_timestamp, old_timestamp))
            with (
                patch.object(
                    runner.fetch_szse_data,
                    "main",
                    side_effect=runner.fetch_szse_data.SzseApiError("connection failed"),
                ),
                patch.object(
                    runner.screening,
                    "load_mainboard_companies",
                    return_value=pd.DataFrame({"股票代码": ["000001"]}),
                ),
            ):
                with self.assertRaisesRegex(runner.PipelineError, "超过"):
                    runner.refresh_stock_pool(paths, target_day=target_day, logger=Mock())

    def test_after_close_retries_when_today_closing_data_is_not_ready(self) -> None:
        target_day = date(2026, 7, 28)
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"stock-pool")
            return_update = runner.ReturnUpdate(
                pd.DataFrame(), pd.DataFrame(), date(2026, 7, 27), 0
            )
            with (
                patch.object(
                    runner,
                    "refresh_stock_pool",
                    return_value=runner.StockPoolRefreshResult(True, target_day),
                ),
                patch.object(runner.screening, "load_mainboard_companies", return_value=pd.DataFrame()),
                patch.object(runner, "update_incremental_return_history", return_value=return_update),
            ):
                with self.assertRaisesRegex(runner.PipelineError, "尚未确认收盘行情"):
                    runner.run_after_close(
                        paths,
                        target_day=target_day,
                        no_push=True,
                        force=False,
                        logger=Mock(),
                    )

            self.assertEqual(
                runner.state_section(paths, target_day, "after_close").get("status"),
                "waiting_for_close_data",
            )

    def test_after_close_restores_current_parameters_when_factor_validation_fails(self) -> None:
        target_day = date(2026, 7, 28)
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"stock-pool")
            paths.current_parameter_file.parent.mkdir(parents=True, exist_ok=True)
            paths.current_parameter_file.write_bytes(b"previous-parameters")
            return_update = runner.ReturnUpdate(
                pd.DataFrame(), pd.DataFrame(), target_day, 0
            )

            def write_new_parameters(*_args: object) -> int:
                paths.current_parameter_file.write_bytes(b"new-parameters")
                return 0

            with (
                patch.object(runner, "refresh_stock_pool"),
                patch.object(runner.screening, "load_mainboard_companies", return_value=pd.DataFrame()),
                patch.object(runner, "update_incremental_return_history", return_value=return_update),
                patch.object(runner, "_missing_required_return_codes", return_value=set()),
                patch.object(
                    runner.ensemble, "main", side_effect=write_new_parameters
                ) as optimize,
                patch.object(
                    runner.screening,
                    "collect_factor_frame",
                    side_effect=runner.PipelineError("factor data unavailable"),
                ),
            ):
                with self.assertRaisesRegex(runner.PipelineError, "factor data unavailable"):
                    runner.run_after_close(
                        paths,
                        target_day=target_day,
                        no_push=True,
                        force=False,
                        logger=Mock(),
                    )

            self.assertEqual(paths.current_parameter_file.read_bytes(), b"previous-parameters")
            ensemble_arguments = optimize.call_args.args[0]
            self.assertEqual(
                ensemble_arguments[
                    ensemble_arguments.index("--as-of-date") + 1
                ],
                target_day.isoformat(),
            )
            self.assertEqual(
                ensemble_arguments[
                    ensemble_arguments.index("--returns-workbook") + 1
                ],
                str(paths.returns_workbook),
            )
            self.assertNotIn("--lookback-days", ensemble_arguments)

    def test_after_close_excludes_unavailable_factor_stocks_and_completes(self) -> None:
        target_day = date(2026, 7, 28)
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.stock_pool.write_bytes(b"stock-pool")
            companies = pd.DataFrame(
                {
                    "序号": [1, 2],
                    "股票代码": ["000001", "000002"],
                    "股票名称": ["甲公司", "乙公司"],
                    "所属行业": ["金融业", "制造业"],
                }
            )
            factors = pd.DataFrame(
                {
                    "序号": [1],
                    "股票代码": ["000001"],
                    "股票名称": ["甲公司"],
                    "所属行业": ["金融业"],
                    "数据日期": [target_day.isoformat()],
                }
            )
            factor_errors = pd.DataFrame(
                {
                    "序号": [2],
                    "股票代码": ["000002"],
                    "股票名称": ["乙公司"],
                    "失败原因": ["测试行情失败"],
                }
            )
            return_update = runner.ReturnUpdate(
                pd.DataFrame(), pd.DataFrame(), target_day, 0
            )

            def write_new_parameters(*_args: object) -> int:
                paths.current_parameter_file.parent.mkdir(parents=True, exist_ok=True)
                paths.current_parameter_file.write_text("{}", encoding="utf-8")
                return 0

            def build_from_available_factors(
                _paths: runner.PipelinePaths,
                _companies: pd.DataFrame,
                usable_factors: pd.DataFrame,
                _day: date,
                _logger: Mock,
            ) -> runner.PredictionResult:
                record = {
                    "预测日期": target_day.isoformat(),
                    "数据截至日期": target_day.isoformat(),
                    "预测状态": "已生成",
                    "预测股票代码": "000001",
                    "预测股票名称": "甲公司",
                    "预测排名": 1,
                    "预测得分": 1.0,
                    "风险过滤后候选数": 1,
                    "未过滤前50数": 1,
                }
                top = pd.DataFrame({"股票代码": ["000001"]})
                return runner.PredictionResult(record, top, top, usable_factors, pd.DataFrame())

            with (
                patch.object(
                    runner,
                    "refresh_stock_pool",
                    return_value=runner.StockPoolRefreshResult(True, target_day),
                ),
                patch.object(runner.screening, "load_mainboard_companies", return_value=companies),
                patch.object(runner, "update_incremental_return_history", return_value=return_update),
                patch.object(runner, "_missing_required_return_codes", return_value=set()),
                patch.object(runner.ensemble, "main", side_effect=write_new_parameters),
                patch.object(
                    runner.screening,
                    "collect_factor_frame",
                    return_value=(
                        factors,
                        factor_errors,
                        {"总数": 2, "缓存命中": 0, "成功": 1, "失败": 1},
                    ),
                ),
                patch.object(runner, "build_prediction", side_effect=build_from_available_factors) as build,
            ):
                result = runner.run_after_close(
                    paths,
                    target_day=target_day,
                    no_push=True,
                    force=False,
                    logger=Mock(),
                )

            self.assertEqual(result, 0)
            self.assertEqual(build.call_args.args[2]["股票代码"].tolist(), ["000001"])
            state = runner.state_section(paths, target_day, "after_close")
            self.assertEqual(state["excluded_factor_codes"], ["000002"])
            self.assertEqual(state["excluded_factor_count"], 1)
            errors = pd.read_csv(
                paths.archive_for(target_day) / "每日因子错误.csv",
                encoding="utf-8-sig",
                dtype={"股票代码": str},
            )
            self.assertEqual(errors["股票代码"].tolist(), ["000002"])

    def test_cached_factor_reader_keeps_only_matching_same_day_factors(self) -> None:
        target_day = date(2026, 7, 28)
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            companies = pd.DataFrame(
                {
                    "序号": [1, 2, 3],
                    "股票代码": ["000001", "000002", "000003"],
                    "股票名称": ["甲公司", "乙公司", "丙公司"],
                    "所属行业": ["金融业", "制造业", "信息技术"],
                }
            )
            cache_dir = paths.project_dir / "data_cache" / "szse_quant" / target_day.isoformat()
            cache_dir.mkdir(parents=True, exist_ok=True)
            valid_cache = {
                "schema_version": runner.screening.CACHE_SCHEMA_VERSION,
                "as_of_date": target_day.isoformat(),
                "source": "本地测试缓存",
                "error": None,
                "factor_cache_version": runner.screening.FACTOR_CACHE_VERSION,
                "factors": {"数据日期": target_day.isoformat(), "收盘价": 10.0},
            }
            error_cache = {**valid_cache, "error": "接口失败", "factors": None}
            stale_cache = {
                **valid_cache,
                "factors": {"数据日期": "2026-07-27", "收盘价": 10.0},
            }
            (cache_dir / "000001.json").write_text(json.dumps(valid_cache), encoding="utf-8")
            (cache_dir / "000002.json").write_text(json.dumps(error_cache), encoding="utf-8")
            (cache_dir / "000003.json").write_text(json.dumps(stale_cache), encoding="utf-8")

            with patch.object(runner.screening, "collect_factor_frame") as collect:
                factors, errors, summary = runner.read_cached_factor_frame(
                    paths, companies, target_day
                )

            collect.assert_not_called()
            self.assertEqual(factors["股票代码"].tolist(), ["000001"])
            self.assertEqual(errors["股票代码"].tolist(), ["000002", "000003"])
            self.assertEqual(summary, {"总数": 3, "缓存命中": 3, "成功": 1, "失败": 2})

    def test_prediction_recovery_publishes_dated_parameters_and_cached_result(self) -> None:
        target_day = date(2026, 7, 28)
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            archive = runner.ensure_archive_dir(paths, target_day)
            pool_path = archive / paths.stock_pool.name
            pool_path.write_bytes(b"stock-pool")
            paths.current_parameter_file.parent.mkdir(parents=True, exist_ok=True)
            paths.current_parameter_file.write_text("old", encoding="utf-8")
            dated_parameter_file = (
                paths.optimizer_output_dir
                / f"{runner.optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
            )
            dated_parameter_file.write_text(
                json.dumps({"run_date": target_day.isoformat(), "best_settings": {}}),
                encoding="utf-8",
            )
            companies = pd.DataFrame(
                {
                    "序号": [1, 2],
                    "股票代码": ["000001", "000002"],
                    "股票名称": ["甲公司", "乙公司"],
                    "所属行业": ["金融业", "制造业"],
                }
            )
            factors = pd.DataFrame(
                {
                    "序号": [1],
                    "股票代码": ["000001"],
                    "股票名称": ["甲公司"],
                    "所属行业": ["金融业"],
                    "数据日期": [target_day.isoformat()],
                }
            )
            factor_errors = pd.DataFrame(
                {
                    "序号": [2],
                    "股票代码": ["000002"],
                    "股票名称": ["乙公司"],
                    "失败原因": ["测试行情失败"],
                }
            )
            return_update = runner.ReturnUpdate(
                pd.DataFrame(), pd.DataFrame(), target_day, 0
            )

            def build_from_available_factors(
                _paths: runner.PipelinePaths,
                _companies: pd.DataFrame,
                usable_factors: pd.DataFrame,
                _day: date,
                _logger: Mock,
            ) -> runner.PredictionResult:
                record = {
                    "预测日期": target_day.isoformat(),
                    "数据截至日期": target_day.isoformat(),
                    "预测状态": "已生成",
                    "预测股票代码": "000001",
                    "预测股票名称": "甲公司",
                    "预测排名": 1,
                    "预测得分": 1.0,
                    "风险过滤后候选数": 1,
                    "未过滤前50数": 1,
                }
                top = pd.DataFrame({"股票代码": ["000001"]})
                return runner.PredictionResult(record, top, top, usable_factors, pd.DataFrame())

            with (
                patch.object(runner.screening, "load_mainboard_companies", return_value=companies),
                patch.object(runner, "_load_return_update_for_recovery", return_value=return_update),
                patch.object(
                    runner,
                    "read_cached_factor_frame",
                    return_value=(
                        factors,
                        factor_errors,
                        {"总数": 2, "缓存命中": 2, "成功": 1, "失败": 1},
                    ),
                ) as cached_factors,
                patch.object(runner, "build_prediction", side_effect=build_from_available_factors),
            ):
                result = runner.run_prediction_recovery(
                    paths,
                    target_day=target_day,
                    no_push=True,
                    force=False,
                    logger=Mock(),
                )

            self.assertEqual(result, 0)
            self.assertEqual(paths.current_parameter_file.read_bytes(), dated_parameter_file.read_bytes())
            cached_factors.assert_called_once_with(paths, companies, target_day)
            state = runner.state_section(paths, target_day, "after_close")
            self.assertTrue(state["recovered_from_factor_cache"])
            self.assertEqual(state["excluded_factor_codes"], ["000002"])
            self.assertTrue(paths.top_fifty.is_file())

    def test_first_return_history_run_bootstraps_the_latest_historical_workbook(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            source = (
                paths.returns_workbook.parent
                / "深市主板每日涨跌幅_2026-07-27.xlsx"
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"historical-workbook")

            runner._bootstrap_returns_workbook(paths)

            self.assertTrue(paths.returns_workbook.is_file())
            self.assertEqual(paths.returns_workbook.read_bytes(), b"historical-workbook")

    def test_requested_pushplus_key_has_priority_over_legacy_key(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / ".env"
            env_file.write_text("PushPlus_token=legacy\nPushPlusapi=requested\n", encoding="utf-8")
            with self.managed_logger(runner.PipelinePaths(Path(temporary_directory))) as logger:
                notifier = runner.PushPlusNotifier.from_env(env_file, logger, enabled=True)

        self.assertEqual(notifier._token, "requested")

    def test_prediction_uses_fixed_parameter_file_and_writes_top_fifty(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            paths.current_parameter_file.parent.mkdir(parents=True, exist_ok=True)
            paths.current_parameter_file.write_text(
                json.dumps(
                    {
                        "best_settings": {
                            "szse_quant_filter_rsi_range": [48.0, 55.0]
                        }
                    }
                ),
                encoding="utf-8",
            )
            companies = pd.DataFrame(
                {
                    "序号": [1, 2],
                    "股票代码": ["000001", "000002"],
                    "股票名称": ["甲公司", "乙公司"],
                    "所属行业": ["金融业", "制造业"],
                }
            )
            factors = companies.assign(数据日期="2026-07-28")
            risk_ranked = pd.DataFrame(
                {
                    "股票代码": ["000001"],
                    "股票名称": ["甲公司"],
                    "所属行业": ["金融业"],
                    "得分": [9.5],
                    "未满足条件（扣分项）": ["无"],
                }
            )
            unfiltered_ranked = pd.DataFrame(
                {
                    "股票代码": [f"{index:06d}" for index in range(1, 51)],
                    "股票名称": [f"公司{index}" for index in range(1, 51)],
                    "所属行业": ["C 制造业"] * 50,
                    "得分": [100 - index / 10 for index in range(1, 51)],
                }
            )
            with self.managed_logger(paths) as logger:
                with patch.object(
                    runner.screening,
                    "score_and_select",
                    side_effect=[(risk_ranked, 1, 1), (unfiltered_ranked, 2, 0)],
                ) as score:
                    prediction = runner.build_prediction(paths, companies, factors, date(2026, 7, 28), logger)
            runner._write_prediction_artifacts(
                paths,
                date(2026, 7, 28),
                prediction,
                pd.DataFrame(),
                runner.ReturnUpdate(pd.DataFrame(), pd.DataFrame(), date(2026, 7, 28), 0),
            )

            self.assertEqual(prediction.record["预测股票代码"], "000001")
            self.assertEqual(prediction.record["预测所属行业"], "金融业")
            self.assertEqual(prediction.record["风险过滤后候选数"], 1)
            self.assertEqual(prediction.record["未过滤前50数"], 50)
            self.assertTrue(paths.top_fifty.is_file())
            risk_filtered = pd.read_csv(
                paths.archive_for(date(2026, 7, 28)) / "风险过滤后得分前10.csv",
                encoding="utf-8-sig",
                dtype=str,
            )
            self.assertEqual(
                list(risk_filtered[["股票代码", "股票名称", "未满足条件（扣分项）"]].iloc[0]),
                ["000001", "甲公司", "无"],
            )
            signal = pd.read_csv(paths.combined_signal, encoding="utf-8-sig", dtype=str)
            self.assertEqual(signal.iloc[0]["预测所属行业"], "金融业")
            self.assertFalse(score.call_args_list[0].kwargs["require_all"])
            self.assertTrue(score.call_args_list[0].kwargs["selected_risks"])
            self.assertEqual(score.call_args_list[1].kwargs["selected_risks"], {})
            self.assertFalse(score.call_args_list[1].kwargs["require_all"])

    def test_realtime_artifacts_overwrite_current_and_archive_both_dates(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            prediction = {
                "预测日期": "2026-07-27",
                "数据截至日期": "2026-07-27",
                "预测状态": "已生成",
                "预测股票代码": "000001",
                "预测股票名称": "甲公司",
                "预测所属行业": "金融业",
                "预测排名": 1,
                "预测得分": 9.5,
                "风险过滤后候选数": 1,
                "未过滤前50数": 50,
            }
            record = runner._realtime_record(
                prediction,
                date(2026, 7, 28),
                status="无信号",
                note="测试",
            )
            runner._write_realtime_artifacts(paths, date(2026, 7, 27), date(2026, 7, 28), record)
            current = pd.read_csv(paths.combined_signal, encoding="utf-8-sig", dtype=str)

            self.assertEqual(current.iloc[0]["实时状态"], "无信号")
            self.assertEqual(current.iloc[0]["预测所属行业"], "金融业")
            self.assertTrue(pd.isna(current.iloc[0]["实时所属行业"]))
            self.assertTrue((paths.archive_for(date(2026, 7, 27)) / "每日交易信号.csv").is_file())
            self.assertTrue((paths.archive_for(date(2026, 7, 28)) / "每日交易信号.csv").is_file())

    def test_realtime_trigger_record_keeps_candidate_industry(self) -> None:
        prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
        prediction.update(
            {
                "预测日期": "2026-07-27",
                "预测状态": "已生成",
                "预测股票代码": "000001",
                "预测股票名称": "甲公司",
                "预测所属行业": "J 金融业",
            }
        )
        trigger = runner.intraday.Trigger(
            candidate=runner.intraday.Candidate("000002", "乙公司", 2, "C 制造业"),
            quote=runner.intraday.Quote(
                "000002",
                Decimal("9.10"),
                Decimal("10.00"),
                "2026-07-28 09:35:00",
            ),
        )

        record = runner._realtime_record(
            prediction,
            date(2026, 7, 28),
            status="已触发",
            note="测试",
            trigger=trigger,
            observed_at=datetime(2026, 7, 28, 9, 36),
        )

        self.assertEqual(record["实时所属行业"], "C 制造业")
        self.assertIn("所属行业：C 制造业", runner._realtime_message(record))

    def test_current_parameter_file_is_exposed_by_runner_paths(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))

            self.assertEqual(paths.current_parameter_file.name, optimizer_current_filename())

    def test_no_push_does_not_mark_a_notification_as_sent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            notifier = runner.PushPlusNotifier("unused", Mock(), enabled=False)

            runner._send_notification_once(
                paths,
                date(2026, 7, 28),
                "prediction",
                notifier,
                "test",
                "test",
                force=False,
            )

            self.assertFalse(paths.state_file(date(2026, 7, 28)).exists())

    def test_stale_quotes_are_not_accepted_as_a_current_snapshot(self) -> None:
        snapshot = {
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.15"), runner.intraday.Decimal("10"), "2026-07-27 15:00:00"
            ),
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.15"), runner.intraday.Decimal("10"), "2026-07-28 09:27:59"
            ),
        }

        self.assertEqual(
            runner._stale_quote_codes(snapshot, date(2026, 7, 28)),
            ["000001", "000002"],
        )
        self.assertTrue(runner._has_current_day_quote(snapshot, date(2026, 7, 28)))
        frozen_snapshot = {
            "000003": runner.intraday.Quote(
                "000003", runner.intraday.Decimal("9.15"), runner.intraday.Decimal("10"), "2026-07-28 09:30:00"
            )
        }
        self.assertEqual(
            runner._stale_quote_codes(
                frozen_snapshot,
                date(2026, 7, 28),
                observed_at=datetime(2026, 7, 28, 9, 32),
            ),
            ["000003"],
        )

        late_final_snapshot = {
            "000004": runner.intraday.Quote(
                "000004", runner.intraday.Decimal("9.15"), runner.intraday.Decimal("10"),
                "2026-07-28 09:46:31",
            )
        }
        self.assertEqual(
            runner._stale_quote_codes(
                late_final_snapshot,
                date(2026, 7, 28),
                observed_at=datetime(2026, 7, 28, 9, 46, 30),
                final_report=True,
            ),
            ["000004"],
        )
        future_final_snapshot = {
            "000005": runner.intraday.Quote(
                "000005", runner.intraday.Decimal("9.15"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:06",
            )
        }
        self.assertEqual(
            runner._stale_quote_codes(
                future_final_snapshot,
                date(2026, 7, 28),
                observed_at=datetime(2026, 7, 28, 10, 5),
                final_report=True,
            ),
            ["000005"],
        )

    def test_committing_final_monitor_record_recovers_without_fetching_quotes(self) -> None:
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            record = runner._realtime_record(
                prediction,
                monitor_day,
                status=sorted(runner.FINAL_MONITOR_STATUSES)[0],
                note="test recovery",
            )
            runner.update_state(
                paths,
                monitor_day,
                "monitor",
                {"status": "committing", "result": record},
            )
            direct_session = Mock()

            with (
                patch.object(runner.intraday, "get_token") as get_token,
                patch.object(runner.intraday, "ZhituApiClient") as quote_client,
                patch.object(
                    runner.intraday, "collect_complete_snapshot"
                ) as collect_snapshot,
                patch.object(runner.intraday, "read_candidates") as read_candidates,
                patch.object(runner, "_direct_http_session", return_value=direct_session),
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                runner.state_section(paths, monitor_day, "monitor")["status"], "completed"
            )
            self.assertEqual(
                runner.state_section(paths, monitor_day, "monitor")["result"], record
            )
            self.assertTrue(paths.combined_signal.is_file())
            get_token.assert_not_called()
            quote_client.assert_not_called()
            collect_snapshot.assert_not_called()
            read_candidates.assert_not_called()
            direct_session.get.assert_not_called()

    def test_intervening_market_sessions_reads_indicators_and_handles_no_sessions(self) -> None:
        prediction_day = date(2026, 7, 27)
        monitor_day = date(2026, 7, 30)
        response = Mock()
        response.json.side_effect = [
            [
                {"time": "2026-07-28 15:00:00"},
                {"time": "2026-07-29 15:00:00"},
            ],
            [],
        ]

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            session = Mock()
            session.get.return_value = response
            with (
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner, "_direct_http_session", return_value=session),
            ):
                sessions = runner._intervening_market_sessions(
                    paths, prediction_day, monitor_day, Mock()
                )
                no_sessions = runner._intervening_market_sessions(
                    paths, prediction_day, monitor_day, Mock()
                )

        self.assertEqual(sessions, [date(2026, 7, 28), date(2026, 7, 29)])
        self.assertEqual(no_sessions, [])
        self.assertEqual(response.raise_for_status.call_count, 2)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {"token": "unused", "st": "20260728", "et": "20260729"},
        )
        self.assertEqual(
            session.get.call_args.args[0],
            runner.MARKET_SESSION_API_URL.format(code=runner.MARKET_SESSION_REFERENCE_CODE),
        )

    def test_monitor_uses_snapshot_completion_time_for_quote_age(self) -> None:
        """A slow full-pool request must age quotes at snapshot completion."""
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidate = runner.intraday.Candidate("000001", "Test", 1)
        quote = runner.intraday.Quote(
            "000001",
            runner.intraday.Decimal("9"),
            runner.intraday.Decimal("10"),
            "2026-07-28 09:30:00",
        )
        snapshot = {candidate.code: quote}
        round_started_at = datetime(2026, 7, 28, 9, 30, tzinfo=runner.CHINA_TIMEZONE)
        snapshot_completed_at = datetime(
            2026, 7, 28, 9, 31, 31, tzinfo=runner.CHINA_TIMEZONE
        )
        window_end = datetime(2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter((round_started_at, round_started_at, snapshot_completed_at, window_end))

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, window_end),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=1),
                patch.object(runner.intraday, "read_candidates", return_value=[candidate]),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday, "collect_complete_snapshot", return_value=snapshot
                ),
                patch.object(
                    runner.intraday, "collect_available_snapshot", return_value=snapshot
                ),
                patch.object(
                    runner, "_stale_quote_codes", wraps=runner._stale_quote_codes
                ) as stale_quotes,
                patch.object(runner.intraday, "select_triggers") as select_triggers,
                patch.object(runner, "_write_realtime_artifacts"),
                patch.object(runner, "update_state"),
                patch.object(runner, "_send_notification_once"),
                patch.object(runner.time_module, "sleep"),
            ):
                runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        stale_quotes.assert_has_calls(
            [
                call(
                    snapshot,
                    monitor_day,
                    observed_at=snapshot_completed_at,
                    final_report=False,
                ),
                call(
                    snapshot,
                    monitor_day,
                    observed_at=window_end,
                    final_report=True,
                ),
            ]
        )
        select_triggers.assert_not_called()

    def test_monitor_accepts_a_fresh_pre_1005_quote_for_the_final_report(self) -> None:
        """The fixed report accepts a fresh quote from the 10:05-adjacent window."""
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidate = runner.intraday.Candidate("000001", "Test", 1)
        quote = runner.intraday.Quote(
            "000001",
            runner.intraday.Decimal("9"),
            runner.intraday.Decimal("10"),
            "2026-07-28 10:04:59",
        )
        snapshot = {candidate.code: quote}
        before_snapshot = datetime(2026, 7, 28, 10, 4, 59, tzinfo=runner.CHINA_TIMEZONE)
        snapshot_completed_at = datetime(
            2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE
        )

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter((before_snapshot, before_snapshot, snapshot_completed_at))

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, snapshot_completed_at),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=1),
                patch.object(runner.intraday, "read_candidates", return_value=[candidate]),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday, "collect_complete_snapshot", return_value=snapshot
                ),
                patch.object(runner.intraday, "select_triggers", return_value=[]),
                patch.object(runner, "_write_realtime_artifacts"),
                patch.object(runner, "update_state"),
                patch.object(runner, "_send_notification_once"),
                patch.object(runner, "_commit_final_monitor_record") as commit_final,
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        self.assertEqual(result, 0)
        commit_final.assert_called_once()
        final_record = commit_final.call_args.args[3]
        self.assertEqual(final_record[runner.SIGNAL_COLUMNS[11]], runner.FINAL_MAX_DECLINE_STATUS)
        self.assertEqual(final_record[runner.SIGNAL_COLUMNS[12]], candidate.code)

    def test_final_report_ignores_missing_quotes_and_keeps_the_first_tied_decline(self) -> None:
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidates = [
            runner.intraday.Candidate("000002", "First", 1),
            runner.intraday.Candidate("000001", "Second", 2),
            runner.intraday.Candidate("000003", "Missing", 3),
        ]
        partial_snapshot = {
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.70"), runner.intraday.Decimal("10"),
                "2026-07-28 10:04:30",
            ),
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.70"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            ),
        }
        final_observed_at = datetime(2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter((final_observed_at, final_observed_at, final_observed_at))

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, final_observed_at),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=3),
                patch.object(runner.intraday, "read_candidates", return_value=candidates),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday,
                    "collect_available_snapshot",
                    return_value=partial_snapshot,
                ) as collect_available,
                patch.object(runner.intraday, "select_triggers", return_value=[]),
                patch.object(runner, "update_state"),
                patch.object(runner, "_commit_final_monitor_record") as commit_final,
                patch.object(runner, "_send_notification_once") as send_notification,
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        self.assertEqual(result, 0)
        collect_available.assert_called_once()
        self.assertEqual(
            [call.args[2] for call in send_notification.call_args_list],
            [runner.MONITOR_FINAL_NOTIFICATION_KEY],
        )
        final_record = commit_final.call_args.args[3]
        self.assertEqual(final_record[runner.SIGNAL_COLUMNS[12]], "000002")
        self.assertIn("2/3", final_record[runner.SIGNAL_COLUMNS[22]])
        self.assertIn(
            "可用行情",
            runner._monitor_notification_title(final_record, monitor_day),
        )

    def test_final_report_rejects_a_snapshot_completed_after_the_deadline(self) -> None:
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidate = runner.intraday.Candidate("000001", "First", 1)
        snapshot = {
            candidate.code: runner.intraday.Quote(
                candidate.code,
                runner.intraday.Decimal("9.70"),
                runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            )
        }
        started_at = datetime(2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE)
        completed_at = runner._final_snapshot_deadline(monitor_day) + timedelta(seconds=1)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter((started_at, started_at, completed_at))

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, completed_at),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=1),
                patch.object(runner.intraday, "read_candidates", return_value=[candidate]),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday,
                    "collect_available_snapshot",
                    return_value=snapshot,
                ),
                patch.object(runner, "_commit_final_monitor_record") as commit_final,
                patch.object(runner, "_send_notification_once"),
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        self.assertEqual(result, 0)
        final_record = commit_final.call_args.args[3]
        self.assertNotEqual(final_record[runner.SIGNAL_COLUMNS[11]], runner.FINAL_MAX_DECLINE_STATUS)
        self.assertEqual(final_record[runner.SIGNAL_COLUMNS[12]], "")

    def test_monitor_sends_each_new_trigger_and_the_1005_maximum_decline_report(self) -> None:
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidates = [
            runner.intraday.Candidate("000001", "First", 1),
            runner.intraday.Candidate("000002", "Second", 2),
        ]
        first_snapshot = {
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.10"), runner.intraday.Decimal("10"),
                "2026-07-28 09:28:00",
            ),
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.90"), runner.intraday.Decimal("10"),
                "2026-07-28 09:28:00",
            ),
        }
        final_snapshot = {
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.20"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            ),
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.00"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            ),
        }
        started_at = datetime(2026, 7, 28, 9, 28, tzinfo=runner.CHINA_TIMEZONE)
        first_observed_at = datetime(2026, 7, 28, 9, 28, tzinfo=runner.CHINA_TIMEZONE)
        final_observed_at = datetime(2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter(
                (started_at, started_at, first_observed_at, final_observed_at, final_observed_at)
            )

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, final_observed_at),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=2),
                patch.object(runner.intraday, "read_candidates", return_value=candidates),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday,
                    "collect_complete_snapshot",
                    return_value=first_snapshot,
                ),
                patch.object(
                    runner.intraday,
                    "collect_available_snapshot",
                    return_value=final_snapshot,
                ),
                patch.object(runner, "update_state"),
                patch.object(runner, "_commit_final_monitor_record") as commit_final,
                patch.object(runner, "_send_notification_once") as send_notification,
                patch.object(runner.time_module, "sleep"),
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[2] for call in send_notification.call_args_list],
            [
                "monitor-trigger-000001",
                "monitor-trigger-000002",
                runner.MONITOR_FINAL_NOTIFICATION_KEY,
            ],
        )
        commit_final.assert_called_once()
        final_record = commit_final.call_args.args[3]
        self.assertEqual(final_record["实时状态"], runner.FINAL_MAX_DECLINE_STATUS)
        self.assertEqual(final_record["实时股票代码"], "000002")
        self.assertIn("已有 2 只候选股触发条件", final_record["备注"])

    def test_monitor_sends_the_1005_maximum_decline_when_nothing_triggers(self) -> None:
        monitor_day = date(2026, 7, 28)
        prediction_day = date(2026, 7, 27)
        candidates = [
            runner.intraday.Candidate("000001", "First", 1),
            runner.intraday.Candidate("000002", "Second", 2),
        ]
        first_snapshot = {
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.80"), runner.intraday.Decimal("10"),
                "2026-07-28 09:28:00",
            ),
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.75"), runner.intraday.Decimal("10"),
                "2026-07-28 09:28:00",
            ),
        }
        final_snapshot = {
            "000001": runner.intraday.Quote(
                "000001", runner.intraday.Decimal("9.70"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            ),
            "000002": runner.intraday.Quote(
                "000002", runner.intraday.Decimal("9.60"), runner.intraday.Decimal("10"),
                "2026-07-28 10:05:00",
            ),
        }
        started_at = datetime(2026, 7, 28, 9, 28, tzinfo=runner.CHINA_TIMEZONE)
        first_observed_at = datetime(2026, 7, 28, 9, 28, tzinfo=runner.CHINA_TIMEZONE)
        final_observed_at = datetime(2026, 7, 28, 10, 5, tzinfo=runner.CHINA_TIMEZONE)

        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            candidate_path = paths.archive_for(prediction_day) / paths.top_fifty.name
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.touch()
            prediction = dict.fromkeys(runner.SIGNAL_COLUMNS, "")
            prediction[runner.SIGNAL_COLUMNS[0]] = prediction_day.isoformat()
            clock_values = iter(
                (started_at, started_at, first_observed_at, final_observed_at, final_observed_at)
            )

            with (
                patch.object(
                    runner,
                    "china_now",
                    side_effect=lambda: next(clock_values, final_observed_at),
                ),
                patch.object(runner, "_load_current_signal", return_value=prediction),
                patch.object(runner, "state_section", return_value={"status": "completed"}),
                patch.object(runner, "_use_batch_quotes", return_value=False),
                patch.object(runner, "_validate_monitor_request_budget", return_value=2),
                patch.object(runner.intraday, "read_candidates", return_value=candidates),
                patch.object(runner.intraday, "get_token", return_value="unused"),
                patch.object(runner.intraday, "ZhituApiClient"),
                patch.object(
                    runner.intraday,
                    "collect_complete_snapshot",
                    return_value=first_snapshot,
                ),
                patch.object(
                    runner.intraday,
                    "collect_available_snapshot",
                    return_value=final_snapshot,
                ),
                patch.object(runner, "update_state"),
                patch.object(runner, "_commit_final_monitor_record") as commit_final,
                patch.object(runner, "_send_notification_once") as send_notification,
                patch.object(runner.time_module, "sleep"),
            ):
                result = runner.run_monitor(
                    paths,
                    monitor_day=monitor_day,
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[2] for call in send_notification.call_args_list],
            [runner.MONITOR_FINAL_NOTIFICATION_KEY],
        )
        commit_final.assert_called_once()
        final_record = commit_final.call_args.args[3]
        self.assertEqual(final_record["实时股票代码"], "000002")
        self.assertIn("本时段无触发股票", final_record["备注"])
        self.assertIn("统计时间", runner._realtime_message(final_record))

    def test_single_stock_monitor_budget_requires_unlimited_or_sufficient_quota(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            with self.assertRaisesRegex(runner.PipelineError, "1900"):
                runner._validate_monitor_request_budget(
                    paths, 50, 60, use_batch=False
                )
            (Path(temporary_directory) / ".env").write_text(
                "zhituapi_daily_limit=unlimited\nzhituapi_rate_limit_per_minute=49\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(runner.PipelineError, "50 次/分钟"):
                runner._validate_monitor_request_budget(paths, 50, 60, use_batch=False)

            (Path(temporary_directory) / ".env").write_text(
                "zhituapi_daily_limit=unlimited\nzhituapi_rate_limit_per_minute=1000\n",
                encoding="utf-8",
            )

            requests_needed = runner._validate_monitor_request_budget(
                paths, 50, 60, use_batch=False
            )

            self.assertEqual(requests_needed, 1900)

    def test_old_lock_owner_cannot_delete_a_replaced_lock(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "runner.lock"
            original = runner.ProcessLock(lock_path)
            original.__enter__()
            lock_path.write_text(
                json.dumps({"pid": 0, "owner_token": "new-owner"}), encoding="utf-8"
            )

            original.__exit__(None, None, None)

            self.assertTrue(lock_path.exists())

    def test_old_incomplete_lock_can_be_recovered_after_a_short_grace_period(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "runner.lock"
            lock_path.write_bytes(b"")
            old_timestamp = runner.time_module.time() - (
                runner.LOCK_INITIALIZATION_GRACE_SECONDS + 1
            )
            runner.os.utime(lock_path, (old_timestamp, old_timestamp))
            recovered = runner.ProcessLock(lock_path)

            recovered.__enter__()

            self.assertTrue(recovered.acquired)
            recovered.__exit__(None, None, None)

    def test_monitor_refuses_to_fall_back_to_an_unarchived_fixed_candidate_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            prediction = {
                "预测日期": "2026-07-27",
                "数据截至日期": "2026-07-27",
                "预测状态": "已生成",
                "预测股票代码": "000001",
                "预测股票名称": "甲公司",
                "预测排名": 1,
                "预测得分": 9.5,
                "风险过滤后候选数": 1,
                "未过滤前50数": 50,
                "监控日期": "",
                "实时状态": "待监控",
            }
            runner.atomic_write_csv(
                runner._single_row_frame(prediction, runner.SIGNAL_COLUMNS),
                paths.combined_signal,
            )
            runner.update_state(paths, date(2026, 7, 27), "after_close", {"status": "completed"})
            paths.top_fifty.write_text("not used", encoding="utf-8")

            with self.assertRaises(runner.PipelineError):
                runner.run_monitor(
                    paths,
                    monitor_day=date(2026, 7, 28),
                    no_push=True,
                    force=False,
                    interval_seconds=60,
                    logger=Mock(),
                )

    def test_final_signal_repairs_a_missing_monitor_state_before_returning(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = runner.PipelinePaths(Path(temporary_directory))
            final_signal = {
                "预测日期": "2026-07-27",
                "数据截至日期": "2026-07-27",
                "预测状态": "已生成",
                "预测股票代码": "000001",
                "预测股票名称": "甲公司",
                "预测排名": 1,
                "预测得分": 9.5,
                "风险过滤后候选数": 1,
                "未过滤前50数": 50,
                "监控日期": "2026-07-28",
                "实时状态": "无信号",
            }
            runner.atomic_write_csv(
                runner._single_row_frame(final_signal, runner.SIGNAL_COLUMNS),
                paths.combined_signal,
            )
            runner.update_state(paths, date(2026, 7, 27), "after_close", {"status": "completed"})

            result = runner.run_monitor(
                paths,
                monitor_day=date(2026, 7, 28),
                no_push=True,
                force=False,
                interval_seconds=60,
                logger=Mock(),
            )

            self.assertEqual(result, 0)
            self.assertTrue(runner.completed_stage(paths, date(2026, 7, 28), "monitor"))


def optimizer_current_filename() -> str:
    return "rolling_parameter_optimization_current.json"


if __name__ == "__main__":
    unittest.main()
