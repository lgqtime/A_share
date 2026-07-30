from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

import fetch_zhitu_stock_pool as zhitu_pool


def _response(payload: object, *, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class ZhituStockPoolTests(unittest.TestCase):
    def _reference_companies(self, rows: list[tuple[str, str, object]]) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=["股票代码", "股票名称", "所属行业"])

    def test_fetch_instrument_uses_code_with_sz_suffix(self) -> None:
        session = Mock()
        session.get.return_value = _response({"ii": "000001.SZ", "name": "平安银行"})
        pacer = Mock()

        instrument, request_count = zhitu_pool.fetch_instrument(
            "000001.SZ",
            "test-token",
            session=session,
            pacer=pacer,
            max_retries=1,
        )

        self.assertEqual(instrument["name"], "平安银行")
        self.assertEqual(request_count, 1)
        pacer.wait.assert_called_once_with()
        session.get.assert_called_once_with(
            "https://api.zhituapi.com/hs/instrument/000001.SZ",
            params={"token": "test-token"},
            timeout=zhitu_pool.DEFAULT_TIMEOUT_SECONDS,
        )

    def test_refresh_updates_name_and_maps_blank_industry_to_unknown(self) -> None:
        reference = self._reference_companies(
            [
                ("000001", "原名称一", "银行"),
                ("000002", "原名称二", "   "),
            ]
        )
        session = Mock()
        session.get.side_effect = [
            _response({"ii": "000001.SZ", "name": "智图名称一"}),
            _response({"ii": "000002.SZ", "name": "智图名称二"}),
        ]

        with (
            patch.object(zhitu_pool, "MINIMUM_REFERENCE_COMPANY_COUNT", 1),
            patch.object(zhitu_pool, "MINIMUM_SUCCESSFUL_COMPANY_COUNT", 1),
        ):
            result = zhitu_pool.refresh_mainboard_company_frame(
                reference,
                "test-token",
                session=session,
                request_interval_seconds=0,
                max_retries=1,
            )

        self.assertEqual(result.company_frame["公司代码"].tolist(), ["000001", "000002"])
        self.assertEqual(result.company_frame["公司简称"].tolist(), ["智图名称一", "智图名称二"])
        self.assertEqual(result.company_frame["所属行业"].tolist(), ["银行", "未知"])
        self.assertEqual(result.successful_codes, ("000001", "000002"))
        self.assertEqual(result.failed_codes, ())
        self.assertEqual(result.unknown_industry_codes, ("000002",))
        self.assertEqual(result.request_count, 2)

    def test_refresh_keeps_reference_record_when_one_instrument_returns_404(self) -> None:
        reference = self._reference_companies(
            [
                ("000001", "保留名称", "银行"),
                ("000002", "原名称二", "制造"),
            ]
        )
        session = Mock()
        session.get.side_effect = [
            _response({}, status_code=404),
            _response({"ii": "000002.SZ", "name": "智图名称二"}),
        ]

        with (
            patch.object(zhitu_pool, "MINIMUM_REFERENCE_COMPANY_COUNT", 1),
            patch.object(zhitu_pool, "MINIMUM_SUCCESSFUL_COMPANY_COUNT", 1),
            patch.object(zhitu_pool, "MINIMUM_SUCCESS_RATIO", 0),
        ):
            result = zhitu_pool.refresh_mainboard_company_frame(
                reference,
                "test-token",
                session=session,
                request_interval_seconds=0,
                max_retries=1,
            )

        self.assertEqual(result.company_frame["公司代码"].tolist(), ["000001", "000002"])
        self.assertEqual(result.company_frame["公司简称"].tolist(), ["保留名称", "智图名称二"])
        self.assertEqual(result.company_frame["所属行业"].tolist(), ["银行", "制造"])
        self.assertEqual(result.successful_codes, ("000002",))
        self.assertEqual(result.failed_codes, ("000001",))

    def test_refresh_rejects_pool_below_required_success_ratio(self) -> None:
        reference = self._reference_companies(
            [
                ("000001", "名称一", "银行"),
                ("000002", "名称二", "制造"),
            ]
        )
        session = Mock()
        session.get.side_effect = [
            _response({"ii": "000001.SZ", "name": "名称一"}),
            _response({}, status_code=404),
        ]

        with (
            patch.object(zhitu_pool, "MINIMUM_REFERENCE_COMPANY_COUNT", 1),
            patch.object(zhitu_pool, "MINIMUM_SUCCESSFUL_COMPANY_COUNT", 1),
            patch.object(zhitu_pool, "MINIMUM_SUCCESS_RATIO", 1.0),
            self.assertRaisesRegex(zhitu_pool.ZhituStockPoolError, r"成功 1/2"),
        ):
            zhitu_pool.refresh_mainboard_company_frame(
                reference,
                "test-token",
                session=session,
                request_interval_seconds=0,
                max_retries=1,
            )

    def test_fetch_instrument_paces_each_attempt_including_retry(self) -> None:
        events: list[str] = []
        pacer = Mock()
        pacer.wait.side_effect = lambda: events.append("pace")
        response = _response({"ii": "000001.SZ", "name": "平安银行"})
        attempts = iter([requests.ConnectionError("offline"), response])

        def get(*_args: object, **_kwargs: object) -> Mock:
            events.append("request")
            next_result = next(attempts)
            if isinstance(next_result, BaseException):
                raise next_result
            return next_result

        session = Mock()
        session.get.side_effect = get

        with patch.object(zhitu_pool.time, "sleep") as retry_sleep:
            instrument, request_count = zhitu_pool.fetch_instrument(
                "000001",
                "test-token",
                session=session,
                pacer=pacer,
                max_retries=2,
            )

        self.assertEqual(instrument["name"], "平安银行")
        self.assertEqual(request_count, 2)
        self.assertEqual(events, ["pace", "request", "pace", "request"])
        self.assertEqual(pacer.wait.call_count, 2)
        retry_sleep.assert_called_once_with(1.0)

    def test_rate_cap_does_not_exceed_monthly_plan_limit(self) -> None:
        self.assertGreater(zhitu_pool.MAX_REQUESTS_PER_MINUTE, 0)
        self.assertLessEqual(zhitu_pool.MAX_REQUESTS_PER_MINUTE, 1_000)


if __name__ == "__main__":
    unittest.main()
