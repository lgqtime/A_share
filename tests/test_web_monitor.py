import unittest
import os
from pathlib import Path
import subprocess
import sys

from web_monitor import (
    QUOTE_UPDATE_SECONDS,
    QuoteParseError,
    append_quote_history,
    filter_quotes_for_stocks,
    merge_quotes,
    build_quote_url,
    parse_quote_payload,
)


class QuoteParsingTests(unittest.TestCase):
    def test_import_does_not_load_project_dotenv_into_process(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        clean_env = os.environ.copy()
        clean_env.pop("TAVILY_HUB_API_KEY1", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; import web_monitor; "
                    "print('present' if os.getenv('TAVILY_HUB_API_KEY1') else 'absent')"
                ),
            ],
            cwd=project_root,
            env=clean_env,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(result.stdout.strip().splitlines()[-1], "absent")

    def test_filter_quotes_for_stocks_discards_removed_candidates(self) -> None:
        quotes = {"000001": {"name": "First"}, "000002": {"name": "Second"}}

        filtered = filter_quotes_for_stocks(quotes, [{"code": "000002", "name": "Second"}])

        self.assertEqual(filtered, {"000002": {"name": "Second"}})

    def test_merge_quotes_reuses_previous_data_when_a_request_fails(self) -> None:
        previous = {
            "000001": {"code": "000001", "name": "First", "change_pct": 1.2}
        }
        merged = merge_quotes(
            previous,
            {},
            [{"code": "000001", "name": "First"}],
            "t2",
        )

        self.assertEqual(merged["000001"]["change_pct"], 1.2)
        self.assertTrue(merged["000001"]["stale"])

    def test_quote_history_keeps_points_and_trims_oldest_points(self) -> None:
        history = {}
        append_quote_history(history, {"000001": {"change_pct": 1.2}}, "t1", max_points=2)
        append_quote_history(history, {"000001": {"change_pct": 1.5}}, "t2", max_points=2)
        append_quote_history(history, {"000001": {"change_pct": 1.8}}, "t3", max_points=2)

        self.assertEqual(
            history,
            {"000001": [{"timestamp": "t2", "change_pct": 1.5}, {"timestamp": "t3", "change_pct": 1.8}]},
        )

    def test_default_backend_refresh_interval_is_ten_seconds(self) -> None:
        self.assertEqual(QUOTE_UPDATE_SECONDS, 10.0)

    def test_build_quote_url_uses_the_six_digit_code_without_exchange_suffix(self) -> None:
        self.assertEqual(
            build_quote_url("000001"),
            "https://api.zhituapi.com/hs/real/ssjy/000001",
        )

    def test_parse_quote_payload_calculates_percentage_from_latest_and_previous_close(self) -> None:
        quote = parse_quote_payload(
            {"t": "2026-08-06 10:23:29", "p": 11.14, "yc": 11.25, "ud": -0.11},
            {"code": "000001", "name": "Ping An Bank"},
        )

        self.assertEqual(quote["last_price"], 11.14)
        self.assertEqual(quote["prev_close"], 11.25)
        self.assertEqual(quote["change_pct"], -0.98)

    def test_parse_quote_payload_rejects_api_errors_instead_of_displaying_zeroes(self) -> None:
        with self.assertRaisesRegex(QuoteParseError, "404"):
            parse_quote_payload(
                {"error": "404: resource not found"},
                {"code": "000001", "name": "Ping An Bank"},
            )
