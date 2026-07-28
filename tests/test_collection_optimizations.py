import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import szse_quant_app as app


def sample_history() -> pd.DataFrame:
    dates = pd.date_range(
        end=pd.Timestamp("2024-03-22"),
        periods=app.INDICATOR_WARMUP_BARS,
        freq="B",
    )
    closes = pd.Series(range(100, 100 + len(dates)), dtype="float64") / 10.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes - 0.05,
            "close": closes,
            "high": closes + 0.1,
            "low": closes - 0.1,
            "volume": [1_000_000.0] * len(dates),
            "amount": [100_000_000.0] * len(dates),
            "amplitude": [2.0] * len(dates),
            "pct_change": [0.5] * len(dates),
            "turnover": [3.0] * len(dates),
        }
    )


class CollectionOptimizationTests(unittest.TestCase):
    def test_tencent_rich_history_parses_historical_amount_and_turnover(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "sz000001": {
                    "qfqday": [
                        [
                            "2026-07-23",
                            "10.92",
                            "11.08",
                            "11.12",
                            "10.90",
                            "1095743.00",
                            {},
                            "0.56",
                            "121083.80",
                            "",
                        ]
                    ]
                }
            },
        }

        history = app.parse_tencent_rich_history(
            f"kline_dayqfq={json.dumps(payload)};",
            "sz000001",
            as_of_date=date(2026, 7, 23),
        )

        self.assertEqual(history.iloc[-1]["date"].date(), date(2026, 7, 23))
        self.assertEqual(history.iloc[-1]["amount"], 1_210_838_000.0)
        self.assertEqual(history.iloc[-1]["turnover"], 0.56)

    def test_incomplete_tencent_cache_is_not_reused(self) -> None:
        history = sample_history()
        history.loc[history.index[-1], ["amount", "turnover"]] = float("nan")
        target_day = date(2024, 3, 22)

        with TemporaryDirectory() as temporary_directory:
            with patch.object(app, "CACHE_DIR", Path(temporary_directory)):
                app._write_cache_record(
                    "000001",
                    source="腾讯前复权日K回退（成交额、换手率缺失）",
                    history=history,
                    error=None,
                    as_of_date=target_day,
                )
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=1.0,
                    as_of_date=target_day,
                )

        self.assertIsNone(cached)

    def test_cached_factors_are_returned_with_fresh_history(self) -> None:
        history = sample_history()
        target_day = date(2024, 3, 22)
        factor_values = {
            "数据日期": "2024-03-22",
            "站上MA5": True,
            "当日成交额": 100_000_000.0,
        }

        with TemporaryDirectory() as temporary_directory:
            with patch.object(app, "CACHE_DIR", Path(temporary_directory)):
                app._write_cache_record(
                    "000001",
                    source="测试来源",
                    history=history,
                    error=None,
                    factors=factor_values,
                    as_of_date=target_day,
                )
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=1.0,
                    as_of_date=target_day,
                )
                wrong_date_cache = app._read_fresh_cache(
                    "000001",
                    cache_hours=1.0,
                    as_of_date=target_day + timedelta(days=1),
                )

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.factors, factor_values)
        self.assertIsNone(wrong_date_cache)

    def test_normalized_fast_path_matches_public_factor_calculation(self) -> None:
        history = sample_history()

        public_factors = app.calculate_factors(history)
        fast_path_factors = app._calculate_factors_from_normalized_history(
            app._normalize_history_frame(history)
        )

        self.assertEqual(fast_path_factors, public_factors)

    def test_float_market_cap_uses_same_day_amount_and_turnover(self) -> None:
        history = sample_history()
        factors = app.calculate_factors(history)

        self.assertAlmostEqual(factors["估算流通市值（亿元）"], 100.0 / 3.0)
        self.assertIsNone(app.estimated_float_market_cap_yi(100_000_000.0, 0.0))
        self.assertIsNone(app.estimated_float_market_cap_yi(None, 3.0))

    def test_historical_date_excludes_later_k_lines_from_factor_calculation(self) -> None:
        history = sample_history()
        target_day = history.iloc[-1]["date"].date()
        future_rows = history.tail(5).copy()
        future_rows["date"] = pd.date_range(
            target_day + timedelta(days=1),
            periods=5,
            freq="B",
        )
        future_rows["close"] += 10.0
        future_rows["open"] += 10.0
        future_rows["high"] += 10.0
        future_rows["low"] += 10.0
        combined_history = pd.concat([history, future_rows], ignore_index=True)

        historical_factors = app.calculate_factors(combined_history, as_of_date=target_day)
        expected_factors = app.calculate_factors(history, as_of_date=target_day)

        self.assertEqual(historical_factors, expected_factors)
        self.assertEqual(historical_factors["数据日期"], target_day.isoformat())

    def test_eastmoney_parameters_use_selected_end_date(self) -> None:
        target_day = date(2024, 6, 3)
        parameters = app._eastmoney_parameters("000001", as_of_date=target_day)

        self.assertEqual(parameters["end"], "20240603")
        self.assertEqual(
            parameters["beg"],
            (target_day - timedelta(days=app.FETCH_CALENDAR_DAYS)).strftime("%Y%m%d"),
        )

    def test_collection_writes_calculated_factors_to_cache(self) -> None:
        history = app._normalize_history_frame(sample_history())
        target_day = date(2024, 3, 22)
        companies = pd.DataFrame(
            {
                "序号": [1],
                "股票代码": ["000001"],
                "股票名称": ["测试股"],
            }
        )
        outcome = app.FetchOutcome(
            code="000001",
            history=history,
            source="测试来源",
            from_cache=False,
        )

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(app, "fetch_stock_history", return_value=outcome) as fetch_history,
            ):
                factors, errors, summary = app.collect_factor_frame(
                    companies,
                    max_companies=1,
                    cache_hours=1.0,
                    force_refresh=False,
                    workers=1,
                    request_interval_seconds=0.25,
                    as_of_date=target_day,
                )
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=1.0,
                    as_of_date=target_day,
                )

        self.assertTrue(errors.empty)
        self.assertEqual(summary["成功"], 1)
        self.assertEqual(fetch_history.call_args.kwargs["as_of_date"], target_day)
        self.assertIsNotNone(cached)
        assert cached is not None and cached.factors is not None
        self.assertEqual(cached.factors["数据日期"], factors.iloc[0]["数据日期"])

    def test_collection_migrates_missing_cached_factors_without_refetching_history(self) -> None:
        history = app._normalize_history_frame(sample_history())
        target_day = date(2024, 3, 22)
        companies = pd.DataFrame(
            {
                "序号": [1],
                "股票代码": ["000001"],
                "股票名称": ["测试股"],
            }
        )
        outcome = app.FetchOutcome(
            code="000001",
            history=history,
            source="测试缓存来源",
            from_cache=True,
            factors=None,
        )

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(app, "fetch_stock_history", return_value=outcome),
            ):
                factors, errors, summary = app.collect_factor_frame(
                    companies,
                    max_companies=1,
                    cache_hours=1.0,
                    force_refresh=False,
                    workers=1,
                    request_interval_seconds=0.25,
                    as_of_date=target_day,
                )
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=1.0,
                    as_of_date=target_day,
                )

        self.assertTrue(errors.empty)
        self.assertEqual(summary["缓存命中"], 1)
        self.assertEqual(summary["成功"], 1)
        self.assertIsNotNone(cached)
        assert cached is not None and cached.factors is not None
        self.assertEqual(cached.factors["KDJ_K(89,3,3)"], factors.iloc[0]["KDJ_K(89,3,3)"])

    def test_historical_date_does_not_request_current_tencent_snapshot(self) -> None:
        history = app._normalize_history_frame(sample_history())
        primary_failure = app.FetchFailure("主源不可用", service_unavailable=False)

        with (
            patch.object(app, "_fetch_eastmoney_history", side_effect=primary_failure),
            patch.object(app, "_fetch_tencent_history", return_value=history),
            patch.object(app, "_fetch_tencent_quote_snapshot") as snapshot_request,
        ):
            outcome = app.fetch_stock_history(
                "000001",
                cache_hours=1.0,
                force_refresh=True,
                limiter=app.RequestRateLimiter(0.0),
                as_of_date=date(2024, 3, 22),
            )

        self.assertIsNotNone(outcome.history)
        self.assertIn("成交额、换手率已提供", outcome.source or "")
        snapshot_request.assert_not_called()

    def test_collection_date_tag_must_match_selected_date(self) -> None:
        self.assertTrue(app._collection_matches_as_of_date("2024-03-22", date(2024, 3, 22)))
        self.assertFalse(app._collection_matches_as_of_date("2024-03-22", date(2024, 3, 25)))

    def test_collection_excludes_stock_without_the_latest_market_day(self) -> None:
        history = app._normalize_history_frame(sample_history())
        companies = pd.DataFrame(
            {
                "序号": [1, 2],
                "股票代码": ["000001", "000002"],
                "股票名称": ["正常交易", "当日停牌"],
            }
        )
        outcomes = {
            "000001": app.FetchOutcome(
                code="000001",
                history=history,
                source="测试",
                from_cache=True,
                factors={"数据日期": "2024-03-22"},
            ),
            "000002": app.FetchOutcome(
                code="000002",
                history=history.iloc[:-1].reset_index(drop=True),
                source="测试",
                from_cache=True,
                factors={"数据日期": "2024-03-21"},
            ),
        }

        with patch.object(
            app,
            "fetch_stock_history",
            side_effect=lambda code, **_: outcomes[code],
        ):
            factors, errors, summary = app.collect_factor_frame(
                companies,
                max_companies=2,
                cache_hours=1.0,
                force_refresh=False,
                workers=1,
                request_interval_seconds=0.0,
                as_of_date=date(2024, 3, 22),
            )

        self.assertEqual(factors["股票代码"].tolist(), ["000001"])
        self.assertEqual(errors["股票代码"].tolist(), ["000002"])
        self.assertIn("最后数据日为 2024-03-21", errors.iloc[0]["失败原因"])
        self.assertEqual(summary["成功"], 1)
        self.assertEqual(summary["失败"], 1)

    def test_circuit_breaker_skips_source_after_consecutive_service_failures(self) -> None:
        breaker = app.SourceCircuitBreaker(
            "测试数据源",
            failure_threshold=2,
            cooldown_seconds=60.0,
        )

        breaker.record_service_failure()
        self.assertTrue(breaker.allow_request())
        breaker.record_service_failure()

        self.assertFalse(breaker.allow_request())
        self.assertTrue(breaker.unavailable_error().service_unavailable)


if __name__ == "__main__":
    unittest.main()
