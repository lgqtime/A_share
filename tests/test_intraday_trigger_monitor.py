from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import requests

from intraday_trigger_monitor import (
    BATCH_API_URL,
    API_URL,
    Candidate,
    Quote,
    ZhituApiClient,
    collect_available_snapshot,
    collect_complete_snapshot,
    read_candidates,
    select_largest_decline,
    select_trigger,
    select_triggers,
)


def make_quote(code: str, last_price: str, previous_close: str) -> Quote:
    return Quote(code, Decimal(last_price), Decimal(previous_close), "2026-07-28 09:35:00")


def make_response(payload: object) -> Mock:
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def make_batch_item(code: str, price: str = "9.15", previous_close: str = "10.00") -> dict[str, str]:
    return {
        "dm": code,
        "p": price,
        "yc": previous_close,
        "t": "2026-07-28 09:35:00",
    }


class TriggerSelectionTests(unittest.TestCase):
    def test_prefers_deeper_drop_before_candidate_rank(self) -> None:
        candidates = [
            Candidate("000001", "First", 1),
            Candidate("000002", "Second", 2),
        ]
        quotes = {
            "000001": make_quote("000001", "9.14", "10.00"),
            "000002": make_quote("000002", "9.10", "10.00"),
        }

        trigger = select_trigger(candidates, quotes)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.candidate.code, "000002")

    def test_uses_rank_then_code_for_equal_drops(self) -> None:
        candidates = [
            Candidate("000002", "Second", 2),
            Candidate("000001", "First", 1),
            Candidate("000003", "Third", 1),
        ]
        quotes = {
            "000001": make_quote("000001", "9.15", "10.00"),
            "000002": make_quote("000002", "9.15", "10.00"),
            "000003": make_quote("000003", "9.15", "10.00"),
        }

        trigger = select_trigger(candidates, quotes)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.candidate.code, "000001")

    def test_includes_exact_negative_eight_point_five_percent(self) -> None:
        candidate = Candidate("000001", "First", 1)
        quotes = {"000001": make_quote("000001", "9.15", "10.00")}

        trigger = select_trigger([candidate], quotes)

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.quote.change_percent, Decimal("-8.500"))

    def test_returns_every_trigger_in_stable_order(self) -> None:
        candidates = [
            Candidate("000001", "First", 1),
            Candidate("000002", "Second", 2),
            Candidate("000003", "Third", 3),
        ]
        quotes = {
            "000001": make_quote("000001", "9.15", "10.00"),
            "000002": make_quote("000002", "9.00", "10.00"),
            "000003": make_quote("000003", "9.40", "10.00"),
        }

        triggers = select_triggers(candidates, quotes)

        self.assertEqual([trigger.candidate.code for trigger in triggers], ["000002", "000001"])

    def test_selects_largest_decline_without_a_trigger_threshold(self) -> None:
        candidates = [
            Candidate("000001", "First", 1),
            Candidate("000002", "Second", 2),
        ]
        quotes = {
            "000001": make_quote("000001", "9.80", "10.00"),
            "000002": make_quote("000002", "9.60", "10.00"),
        }

        selection = select_largest_decline(candidates, quotes)

        self.assertIsNotNone(selection)
        self.assertEqual(selection.candidate.code, "000002")

    def test_largest_decline_tie_keeps_the_first_candidate_in_sequence(self) -> None:
        candidates = [
            Candidate("000002", "First", 2),
            Candidate("000001", "Second", 1),
        ]
        quotes = {
            "000001": make_quote("000001", "9.60", "10.00"),
            "000002": make_quote("000002", "9.60", "10.00"),
        }

        selection = select_largest_decline(candidates, quotes)

        self.assertIsNotNone(selection)
        self.assertEqual(selection.candidate.code, "000002")

    def test_read_candidates_preserves_leading_zeroes(self) -> None:
        header = "评分排名,股票代码,股票名称,所属行业\n"
        rows = [f"{index},{index},Stock {index},C 制造业\n" for index in range(1, 51)]
        with TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "candidates.csv"
            csv_path.write_text(header + "".join(rows), encoding="utf-8")
            candidates = read_candidates(csv_path)

        self.assertEqual(len(candidates), 50)
        self.assertEqual(candidates[0].code, "000001")
        self.assertEqual(candidates[0].industry, "C 制造业")
        self.assertEqual(candidates[-1].rank, 50)

    def test_read_candidates_rejects_missing_or_blank_industry(self) -> None:
        missing_header = "评分排名,股票代码,股票名称\n"
        blank_header = "评分排名,股票代码,股票名称,所属行业\n"
        missing_rows = [f"{index},{index},Stock {index}\n" for index in range(1, 51)]
        blank_rows = [f"{index},{index},Stock {index},\n" for index in range(1, 51)]
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            missing_path = directory / "missing.csv"
            blank_path = directory / "blank.csv"
            missing_path.write_text(missing_header + "".join(missing_rows), encoding="utf-8")
            blank_path.write_text(blank_header + "".join(blank_rows), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "所属行业"):
                read_candidates(missing_path)
            with self.assertRaisesRegex(ValueError, "industry"):
                read_candidates(blank_path)


class ZhituApiClientTests(unittest.TestCase):
    def test_single_quote_uses_documented_url_and_token_parameter(self) -> None:
        session = Mock()
        session.get.return_value = make_response(make_batch_item("000001"))
        client = ZhituApiClient("test-token", timeout_seconds=7.5, session=session)

        quote = client.get_quote("000001")

        self.assertEqual(quote.code, "000001")
        session.get.assert_called_once_with(
            "https://api.zhituapi.com/hs/real/ssjy/000001",
            params={"token": "test-token"},
            timeout=7.5,
        )
        self.assertEqual(API_URL, "https://api.zhituapi.com/hs/real/ssjy/{code}")

    def test_batch_quotes_split_at_twenty_with_documented_parameters(self) -> None:
        codes = [f"{number:06d}" for number in range(1, 42)]
        chunks = [codes[:20], codes[20:40], codes[40:]]
        session = Mock()
        session.get.side_effect = [
            make_response([make_batch_item(code) for code in chunk]) for chunk in chunks
        ]
        client = ZhituApiClient("test-token", timeout_seconds=6.0, session=session)

        quotes = client.get_quotes(codes)

        self.assertEqual(list(quotes), codes)
        self.assertEqual(len(quotes), 41)
        self.assertEqual(session.get.call_count, 3)
        for call, chunk in zip(session.get.call_args_list, chunks, strict=True):
            self.assertEqual(call.args, ("https://api.zhituapi.com/hs/public/ssjymore",))
            self.assertEqual(
                call.kwargs,
                {
                    "params": {"token": "test-token", "stock_codes": ",".join(chunk)},
                    "timeout": 6.0,
                },
            )
        self.assertEqual(BATCH_API_URL, "https://api.zhituapi.com/hs/public/ssjymore")

    def test_batch_quotes_reject_missing_code(self) -> None:
        session = Mock()
        session.get.return_value = make_response([make_batch_item("000001")])
        client = ZhituApiClient("test-token", session=session)

        with self.assertRaisesRegex(ValueError, r"missing codes: 000002"):
            client.get_quotes(["000001", "000002"])

    def test_partial_batch_quotes_keep_the_available_codes(self) -> None:
        session = Mock()
        session.get.return_value = make_response([make_batch_item("000001")])
        client = ZhituApiClient("test-token", session=session)

        quotes = client.get_quotes(
            ["000001", "000002"],
            allow_partial=True,
        )

        self.assertEqual(list(quotes), ["000001"])

    def test_partial_batch_quotes_skip_only_a_failed_batch(self) -> None:
        codes = [f"{number:06d}" for number in range(1, 22)]
        failed_response = Mock()
        failed_response.raise_for_status.side_effect = requests.HTTPError("temporary failure")
        session = Mock()
        session.get.side_effect = [
            make_response([make_batch_item(code) for code in codes[:20]]),
            failed_response,
        ]
        client = ZhituApiClient("test-token", session=session)

        quotes = client.get_quotes(codes, allow_partial=True)

        self.assertEqual(list(quotes), codes[:20])

    def test_final_snapshot_ignores_a_failed_single_stock_request(self) -> None:
        session = Mock()
        failed_response = Mock()
        failed_response.raise_for_status.side_effect = requests.HTTPError("temporary failure")
        session.get.side_effect = [make_response(make_batch_item("000001")), failed_response]
        client = ZhituApiClient("test-token", session=session)
        logger = Mock()

        snapshot = collect_available_snapshot(
            [Candidate("000001", "First", 1), Candidate("000002", "Second", 2)],
            client,
            logger,
            use_batch=False,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(list(snapshot), ["000001"])
        logger.warning.assert_called_once()

    def test_complete_snapshot_discards_a_batch_with_missing_code(self) -> None:
        session = Mock()
        session.get.return_value = make_response([make_batch_item("000001")])
        client = ZhituApiClient("test-token", session=session)
        logger = Mock()

        snapshot = collect_complete_snapshot(
            [Candidate("000001", "First", 1), Candidate("000002", "Second", 2)],
            client,
            logger,
        )

        self.assertIsNone(snapshot)
        logger.warning.assert_called_once()

    def test_batch_quotes_reject_non_finite_price(self) -> None:
        session = Mock()
        session.get.return_value = make_response([make_batch_item("000001", price="NaN")])
        client = ZhituApiClient("test-token", session=session)

        with self.assertRaisesRegex(ValueError, "non-positive price"):
            client.get_quotes(["000001"])
