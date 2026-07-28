import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import szse_quant_app as app
import warm_szse_quant_cache as warm


def sample_history(as_of_date: date) -> pd.DataFrame:
    """生成满足主程序指标预热要求的完整模拟日线。"""

    dates = pd.date_range(end=pd.Timestamp(as_of_date), periods=app.INDICATOR_WARMUP_BARS, freq="B")
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


def warm_arguments(target_day: date) -> dict[str, object]:
    """创建单股预热所需的非网络依赖。"""

    return {
        "cache_hours": 12.0,
        "force_refresh": False,
        "limiter": app.RequestRateLimiter(0.0),
        "rich_breaker": app.SourceCircuitBreaker("腾讯增强日 K"),
        "legacy_breaker": app.SourceCircuitBreaker("腾讯旧版日 K"),
        "as_of_date": target_day,
    }


class WarmCacheTests(unittest.TestCase):
    def test_tencent_rich_failure_falls_back_without_unbound_local_error(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)
        rich_failure = app.FetchFailure("腾讯增强接口不可用", service_unavailable=True)
        legacy_failure = app.FetchFailure("腾讯旧版接口不可用", service_unavailable=True)

        with (
            patch.object(app, "_fetch_tencent_rich_history", side_effect=rich_failure) as rich_fetch,
            patch.object(app, "_fetch_tencent_legacy_history", return_value=history) as legacy_fetch,
        ):
            fetched_history, source = warm._fetch_tencent_history(
                "000001",
                limiter=app.RequestRateLimiter(0.0),
                rich_breaker=app.SourceCircuitBreaker("腾讯增强日 K"),
                legacy_breaker=app.SourceCircuitBreaker("腾讯旧版日 K"),
                as_of_date=target_day,
            )

        self.assertIs(fetched_history, history)
        self.assertIn("回退", source)
        rich_fetch.assert_called_once()
        legacy_fetch.assert_called_once()

        with (
            patch.object(app, "_fetch_tencent_rich_history", side_effect=rich_failure),
            patch.object(app, "_fetch_tencent_legacy_history", side_effect=legacy_failure),
            self.assertRaises(app.FetchFailure) as raised,
        ):
            warm._fetch_tencent_history(
                "000001",
                limiter=app.RequestRateLimiter(0.0),
                rich_breaker=app.SourceCircuitBreaker("腾讯增强日 K"),
                legacy_breaker=app.SourceCircuitBreaker("腾讯旧版日 K"),
                as_of_date=target_day,
            )

        self.assertIn("腾讯增强接口不可用", str(raised.exception))
        self.assertIn("腾讯旧版接口不可用", str(raised.exception))
        self.assertTrue(raised.exception.service_unavailable)

    def test_tencent_rich_result_writes_cache_reusable_by_main_app(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(app, "_fetch_tencent_rich_history", return_value=history) as rich_fetch,
            ):
                outcome = warm.warm_one_cache("000001", **warm_arguments(target_day))
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=12.0,
                    as_of_date=target_day,
                )

        self.assertIsNone(outcome.error)
        self.assertFalse(outcome.from_cache)
        rich_fetch.assert_called_once()
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertTrue(cached.from_cache)
        self.assertIsNotNone(cached.history)
        assert cached.history is not None
        self.assertEqual(len(cached.history), app.INDICATOR_WARMUP_BARS)
        self.assertIsNotNone(cached.factors)
        assert cached.factors is not None
        self.assertEqual(cached.factors["数据日期"], target_day.isoformat())

    def test_existing_cache_hit_does_not_request_tencent_history(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)
        factors = app.calculate_factors(history, as_of_date=target_day)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(warm, "_fetch_tencent_history") as fetch_history,
            ):
                app._write_cache_record(
                    "000001",
                    source="腾讯增强前复权日 K（缓存预热）",
                    history=history,
                    error=None,
                    factors=factors,
                    as_of_date=target_day,
                )
                outcome = warm.warm_one_cache("000001", **warm_arguments(target_day))

        self.assertIsNone(outcome.error)
        self.assertTrue(outcome.from_cache)
        fetch_history.assert_not_called()

    def test_stale_factor_cache_is_migrated_without_requesting_tencent_history(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)
        expected_factors = app.calculate_factors(history, as_of_date=target_day)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(warm, "_fetch_tencent_history") as fetch_history,
                patch.object(app, "calculate_factors", return_value=expected_factors) as calculate,
            ):
                app._write_cache_record(
                    "000001",
                    source="腾讯增强前复权日 K（缓存预热）",
                    history=history,
                    error=None,
                    factors=None,
                    as_of_date=target_day,
                )
                outcome = warm.warm_one_cache("000001", **warm_arguments(target_day))
                cached = app._read_fresh_cache(
                    "000001",
                    cache_hours=12.0,
                    as_of_date=target_day,
                )

        self.assertIsNone(outcome.error)
        self.assertTrue(outcome.from_cache)
        fetch_history.assert_not_called()
        calculate.assert_called_once()
        self.assertEqual(calculate.call_args.kwargs["as_of_date"], target_day)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.factors, expected_factors)

    def test_stale_factor_cache_returns_error_when_migration_calculation_fails(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(app, "CACHE_DIR", Path(temporary_directory)),
                patch.object(warm, "_fetch_tencent_history") as fetch_history,
                patch.object(app, "calculate_factors", side_effect=ValueError("因子计算失败")),
            ):
                app._write_cache_record(
                    "000001",
                    source="腾讯增强前复权日 K（缓存预热）",
                    history=history,
                    error=None,
                    factors=None,
                    as_of_date=target_day,
                )
                outcome = warm.warm_one_cache("000001", **warm_arguments(target_day))

        self.assertFalse(outcome.from_cache)
        self.assertIn("因子计算失败", outcome.error or "")
        fetch_history.assert_not_called()

    def test_missing_latest_liquidity_does_not_write_cache(self) -> None:
        target_day = date(2026, 7, 24)
        history = sample_history(target_day)
        history.loc[history.index[-1], ["amount", "turnover"]] = float("nan")

        with TemporaryDirectory() as temporary_directory:
            cache_directory = Path(temporary_directory)
            with (
                patch.object(app, "CACHE_DIR", cache_directory),
                patch.object(app, "_fetch_tencent_rich_history", return_value=history),
            ):
                outcome = warm.warm_one_cache("000001", **warm_arguments(target_day))
                cache_path = app._cache_path("000001", target_day)

        self.assertFalse(outcome.from_cache)
        self.assertIn("缺少成交额或换手率", outcome.error or "")
        self.assertFalse(cache_path.exists())

    def test_warm_cache_rejects_invalid_runtime_options_before_requests(self) -> None:
        companies = pd.DataFrame(columns=["序号", "股票代码", "股票名称"])
        arguments = {
            "companies": companies,
            "max_companies": None,
            "cache_hours": 12.0,
            "force_refresh": False,
            "workers": 1,
            "request_interval_seconds": 0.0,
            "as_of_date": date(2026, 7, 24),
        }

        invalid_options = (
            ({"max_companies": 0}, "--max-companies"),
            ({"workers": 0}, "--workers"),
            ({"request_interval_seconds": -0.1}, "--interval"),
            ({"cache_hours": -0.1}, "--cache-hours"),
        )
        for overrides, message in invalid_options:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                warm.warm_cache(**(arguments | overrides))


if __name__ == "__main__":
    unittest.main()
