from __future__ import annotations

from datetime import date
import json
import unittest
from unittest.mock import patch

import pandas as pd

import stock_analysis as analysis


def make_history(rows: int = 160, start: str = "2024-01-02") -> pd.DataFrame:
    """Return deterministic, valid daily OHLCV data for offline tests."""

    dates = pd.bdate_range(start=start, periods=rows)
    close = pd.Series([10.0 + index * 0.1 for index in range(rows)], dtype="float64")
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.03,
            "close": close,
            "high": close + 0.12,
            "low": close - 0.12,
            "volume": [1_000_000.0] * rows,
            "amount": [100_000_000.0] * rows,
            "amplitude": [2.0] * rows,
            "pct_change": [0.5] * rows,
            "turnover": [3.0] * rows,
        }
    )


def eastmoney_payload(frame: pd.DataFrame) -> dict[str, object]:
    """Build a minimal Eastmoney daily-kline response without network access."""

    klines = []
    for row in frame.itertuples(index=False):
        day = pd.Timestamp(row.date).strftime("%Y-%m-%d")
        klines.append(
            ",".join(
                (
                    day,
                    f"{row.open:.2f}",
                    f"{row.close:.2f}",
                    f"{row.high:.2f}",
                    f"{row.low:.2f}",
                    f"{row.volume:.0f}",
                    f"{row.amount:.2f}",
                    f"{row.amplitude:.2f}",
                    f"{row.pct_change:.2f}",
                    "0.00",
                    f"{row.turnover:.2f}",
                )
            )
        )
    return {"rc": 0, "data": {"klines": klines}}


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeTextResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class StockCodeTests(unittest.TestCase):
    def test_normalize_stock_code_accepts_common_shenzhen_and_shanghai_formats(self) -> None:
        cases = {
            " 000001 ": ("000001", "SZ", "0.000001"),
            "SZ000001": ("000001", "SZ", "0.000001"),
            "000001.SZ": ("000001", "SZ", "0.000001"),
            "SH600000": ("600000", "SH", "1.600000"),
            "600000.SH": ("600000", "SH", "1.600000"),
            "688001": ("688001", "SH", "1.688001"),
        }

        for raw_code, expected in cases.items():
            with self.subTest(raw_code=raw_code):
                security = analysis.normalize_stock_code(raw_code)
                self.assertEqual(
                    (security.code, security.exchange, security.secid), expected
                )

    def test_normalize_stock_code_rejects_invalid_or_mismatched_market(self) -> None:
        for raw_code in (None, "12345", "400001", "300001.SH", "SZ600000"):
            with self.subTest(raw_code=raw_code):
                with self.assertRaises(ValueError):
                    analysis.normalize_stock_code(raw_code)


class EastmoneyHistoryTests(unittest.TestCase):
    def test_parse_eastmoney_history_normalizes_valid_rows_and_uses_turnover_field(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2024-01-03,10.20,10.30,10.50,10.10,2000,206000,3.92,1.96,0.20,4.50",
                    "2024-01-02,10.00,10.10,10.20,9.90,1000,101000,2.97,1.00,0.10,3.25",
                    "2024-01-04,10.00,invalid,10.20,9.90,1000,101000,2.97,1.00,0.10,3.25",
                ]
            },
        }

        history = analysis.parse_eastmoney_history(payload)

        self.assertEqual(history["date"].dt.strftime("%Y-%m-%d").tolist(), ["2024-01-02", "2024-01-03"])
        self.assertEqual(history["open"].tolist(), [10.0, 10.2])
        self.assertEqual(history["amount"].tolist(), [101000.0, 206000.0])
        self.assertEqual(history["turnover"].tolist(), [3.25, 4.5])

    def test_parse_eastmoney_history_rejects_non_successful_or_empty_response(self) -> None:
        with self.assertRaises(analysis.StockHistoryError):
            analysis.parse_eastmoney_history({"rc": 1, "data": {"klines": []}})
        with self.assertRaises(analysis.StockHistoryError):
            analysis.parse_eastmoney_history({"rc": 0, "data": {"klines": []}})

    def test_fetch_uses_selected_end_date_and_excludes_future_k_lines(self) -> None:
        source = make_history(rows=200)
        selected_day = pd.Timestamp(source.loc[149, "date"]).date()
        payload = eastmoney_payload(source)

        with patch.object(
            analysis.requests,
            "get",
            return_value=_FakeResponse(payload),
        ) as get:
            history = analysis.fetch_adjusted_daily_history(
                "000001",
                display_bars=5,
                as_of_date=selected_day,
            )

        parameters = get.call_args.kwargs["params"]
        self.assertEqual(parameters["end"], selected_day.strftime("%Y%m%d"))
        self.assertEqual(parameters["secid"], "0.000001")
        self.assertEqual(parameters["lmt"], str(analysis.required_history_bars(5)))
        self.assertEqual(history["date"].max().date(), selected_day)
        self.assertTrue(history["date"].le(pd.Timestamp(selected_day)).all())
        self.assertEqual(len(history), analysis.required_history_bars(5))

    def test_fetch_falls_back_to_tencent_rich_history_with_amount_and_turnover(self) -> None:
        source = make_history(rows=150)
        selected_index = 139
        selected_day = pd.Timestamp(source.loc[selected_index, "date"]).date()
        source.loc[selected_index, "amount"] = 123_450_000.0
        source.loc[selected_index, "turnover"] = 7.25
        qfqday = []
        for row in source.itertuples(index=False):
            qfqday.append(
                [
                    pd.Timestamp(row.date).strftime("%Y-%m-%d"),
                    f"{row.open:.2f}",
                    f"{row.close:.2f}",
                    f"{row.high:.2f}",
                    f"{row.low:.2f}",
                    f"{row.volume:.0f}",
                    "0.00",
                    f"{row.turnover:.2f}",
                    f"{row.amount / 10_000:.2f}",
                ]
            )
        rich_text = "kline_dayqfq=" + json.dumps(
            {"code": 0, "data": {"sz000001": {"qfqday": qfqday}}}
        ) + ";"

        with patch.object(
            analysis.requests,
            "get",
            side_effect=[
                analysis.requests.ConnectionError("eastmoney unavailable"),
                _FakeTextResponse(rich_text),
            ],
        ) as get:
            history = analysis.fetch_adjusted_daily_history(
                "000001",
                display_bars=5,
                as_of_date=selected_day,
            )

        tencent_parameters = get.call_args_list[1].kwargs["params"]
        self.assertEqual(
            tencent_parameters["param"].split(",", 1)[0], "sz000001"
        )
        self.assertEqual(history["date"].max().date(), selected_day)
        self.assertTrue(history["date"].le(pd.Timestamp(selected_day)).all())
        self.assertEqual(len(history), analysis.required_history_bars(5))
        self.assertEqual(history.iloc[-1]["amount"], 123_450_000.0)
        self.assertEqual(history.iloc[-1]["turnover"], 7.25)


class IndicatorCalculationTests(unittest.TestCase):
    def test_ma_and_macd_use_standard_5_20_and_12_26_9_parameters(self) -> None:
        enriched = analysis.enrich_history_with_indicators(make_history())
        close = enriched["close"]

        self.assertAlmostEqual(enriched["ma5"].iloc[-1], close.tail(5).mean())
        self.assertAlmostEqual(enriched["ma20"].iloc[-1], close.tail(20).mean())

        expected_ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
        expected_ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
        expected_dif = expected_ema12 - expected_ema26
        expected_dea = expected_dif.ewm(span=9, adjust=False, min_periods=9).mean()

        pd.testing.assert_series_equal(
            enriched["macd_dif"], expected_dif, check_names=False
        )
        pd.testing.assert_series_equal(
            enriched["macd_dea"], expected_dea, check_names=False
        )
        pd.testing.assert_series_equal(
            enriched["macd_histogram"], 2.0 * (expected_dif - expected_dea), check_names=False
        )

    def test_default_kdj_parameters_remain_89_3_3_and_use_the_50_seed(self) -> None:
        count = analysis.KDJ_RSV_PERIOD + 2
        history = pd.DataFrame(
            {
                "low": [0.0] * count,
                "high": [100.0] * count,
                "close": [0.0] * count,
            }
        )

        calculated = analysis.calculate_kdj(history)
        explicit_default = analysis.calculate_kdj(
            history,
            kdj_parameters=analysis.DEFAULT_KDJ_PARAMETERS,
        )
        first_valid = analysis.KDJ_RSV_PERIOD - 1

        self.assertEqual(
            analysis.DEFAULT_KDJ_PARAMETERS,
            analysis.KdjParameters(89, 3, 3),
        )
        self.assertEqual(analysis.KDJ_RSV_PERIOD, 89)
        self.assertEqual(analysis.KDJ_K_SMOOTHING_PERIOD, 3)
        self.assertEqual(analysis.KDJ_D_SMOOTHING_PERIOD, 3)
        pd.testing.assert_frame_equal(calculated, explicit_default)
        self.assertTrue(calculated["kdj_k"].iloc[:first_valid].isna().all())
        self.assertEqual(calculated["kdj_rsv"].iloc[first_valid], 0.0)
        self.assertAlmostEqual(calculated["kdj_k"].iloc[first_valid], 100.0 / 3.0)
        self.assertAlmostEqual(calculated["kdj_d"].iloc[first_valid], 400.0 / 9.0)
        self.assertAlmostEqual(calculated["kdj_j"].iloc[first_valid], 100.0 / 9.0)
        self.assertAlmostEqual(calculated["kdj_k"].iloc[first_valid + 1], 200.0 / 9.0)
        self.assertAlmostEqual(calculated["kdj_d"].iloc[first_valid + 1], 1000.0 / 27.0)

    def test_kdj_parameters_require_positive_integer_values(self) -> None:
        invalid_values = (0, -1, 1.5, "3", True)
        parameter_names = (
            "rsv_period",
            "k_smoothing_period",
            "d_smoothing_period",
        )

        for parameter_name in parameter_names:
            for invalid_value in invalid_values:
                with self.subTest(
                    parameter_name=parameter_name,
                    invalid_value=invalid_value,
                ):
                    with self.assertRaisesRegex(ValueError, "大于 0"):
                        analysis.KdjParameters(**{parameter_name: invalid_value})

    def test_dynamic_kdj_parameters_change_rsv_and_smoothing_calculation(self) -> None:
        parameters = analysis.KdjParameters(
            rsv_period=5,
            k_smoothing_period=4,
            d_smoothing_period=2,
        )
        history = pd.DataFrame(
            {
                "low": [0.0] * 7,
                "high": [100.0] * 7,
                "close": [0.0] * 7,
            }
        )

        calculated = analysis.calculate_kdj(history, kdj_parameters=parameters)
        first_valid = parameters.rsv_period - 1

        for column in ("kdj_rsv", "kdj_k", "kdj_d", "kdj_j"):
            with self.subTest(column=column):
                self.assertIn(column, calculated.columns)
        self.assertTrue(calculated["kdj_k"].iloc[:first_valid].isna().all())
        self.assertEqual(calculated["kdj_rsv"].iloc[first_valid], 0.0)
        self.assertAlmostEqual(calculated["kdj_k"].iloc[first_valid], 37.5)
        self.assertAlmostEqual(calculated["kdj_d"].iloc[first_valid], 43.75)
        self.assertAlmostEqual(calculated["kdj_j"].iloc[first_valid], 25.0)
        self.assertAlmostEqual(calculated["kdj_k"].iloc[first_valid + 1], 28.125)
        self.assertAlmostEqual(calculated["kdj_d"].iloc[first_valid + 1], 35.9375)
        self.assertAlmostEqual(calculated["kdj_j"].iloc[first_valid + 1], 12.5)

    def test_required_history_bars_grows_for_large_kdj_smoothing_periods(self) -> None:
        display_bars = 120
        default_bars = analysis.required_history_bars(display_bars)
        large_smoothing = analysis.KdjParameters(
            rsv_period=89,
            k_smoothing_period=60,
            d_smoothing_period=40,
        )
        large_smoothing_bars = analysis.required_history_bars(
            display_bars,
            kdj_parameters=large_smoothing,
        )

        self.assertEqual(default_bars, 249)
        self.assertEqual(
            large_smoothing_bars,
            display_bars + large_smoothing.rsv_period + 5 * 60,
        )
        self.assertGreater(large_smoothing_bars, default_bars)

    def test_display_window_preserves_indicator_warmup_from_prior_history(self) -> None:
        history = make_history(rows=160)
        displayed = analysis.build_analysis_frame(history, display_bars=20)
        cold_start = analysis.calculate_kdj(
            analysis.normalize_ohlcv_frame(history).tail(20).reset_index(drop=True)
        )

        self.assertEqual(analysis.required_history_bars(120), 249)
        self.assertEqual(len(displayed), 20)
        self.assertTrue(displayed["ma20"].notna().all())
        self.assertTrue(displayed["kdj_k"].notna().all())
        self.assertTrue(cold_start["kdj_k"].isna().all())


if __name__ == "__main__":
    unittest.main()
