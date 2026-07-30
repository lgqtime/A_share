"""Monitor a ranked A-share candidate list for the first -8.5% morning signal.

This program only reads market data and prints a signal.  It never sends orders
to a broker.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time as time_module
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

import requests


ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATES = ROOT / "前 50 名（含所属行业）.csv"
DEFAULT_ENV_FILE = ROOT / ".env"
API_URL = "https://api.zhituapi.com/hs/real/ssjy/{code}"
BATCH_API_URL = "https://api.zhituapi.com/hs/public/ssjymore"
MAX_BATCH_CODES = 20
START_TIME = time(9, 28)
END_TIME = time(9, 45)
THRESHOLD_PERCENT = Decimal("-8.5")
DEFAULT_INTERVAL_SECONDS = 60
MINIMUM_INTERVAL_SECONDS = 60
RANK_COLUMNS = ("评分排名", "排名（风险过滤后）")


@dataclass(frozen=True)
class Candidate:
    code: str
    name: str
    rank: int
    industry: str = ""


@dataclass(frozen=True)
class Quote:
    code: str
    last_price: Decimal
    previous_close: Decimal
    updated_at: str

    @property
    def change_percent(self) -> Decimal:
        return (self.last_price - self.previous_close) * Decimal("100") / self.previous_close


@dataclass(frozen=True)
class Trigger:
    candidate: Candidate
    quote: Quote


def read_env_value(path: Path, key: str) -> str | None:
    """Read one .env value without adding a dependency or logging secrets."""
    if not path.exists():
        return None

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate_key, value = line.split("=", 1)
        if candidate_key.strip() == key:
            return value.strip().strip("\"'") or None
    return None


def get_token(env_file: Path) -> str:
    token = os.environ.get("zhituapi") or read_env_value(env_file, "zhituapi")
    if not token:
        raise ValueError(f"Missing zhituapi in {env_file} or the environment.")
    return token


def use_batch_quotes(env_file: Path) -> bool:
    """Use the multi-stock endpoint only when the installed plan permits it."""

    raw_value = os.environ.get("zhituapi_batch_quotes") or read_env_value(
        env_file, "zhituapi_batch_quotes"
    )
    return bool(raw_value and raw_value.strip().lower() in {"1", "true", "yes", "batch"})


def read_candidates(path: Path) -> list[Candidate]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    actual_columns = set(rows[0]) if rows else set()
    rank_column = next((column for column in RANK_COLUMNS if column in actual_columns), None)
    required_columns = {"股票代码", "股票名称", "所属行业"}
    missing_columns = required_columns - actual_columns
    if missing_columns:
        raise ValueError(f"Candidate CSV is missing columns: {', '.join(sorted(missing_columns))}")
    if rank_column is None:
        raise ValueError(f"Candidate CSV is missing a rank column: {', '.join(RANK_COLUMNS)}")
    if len(rows) != 50:
        raise ValueError(f"Expected exactly 50 candidates, found {len(rows)}.")

    candidates: list[Candidate] = []
    seen_codes: set[str] = set()
    seen_ranks: set[int] = set()
    for row in rows:
        code = (row["股票代码"] or "").strip().zfill(6)
        name = (row["股票名称"] or "").strip()
        industry = (row["所属行业"] or "").strip()
        try:
            rank = int((row[rank_column] or "").strip())
        except ValueError as exc:
            raise ValueError(f"Invalid candidate rank for {code or '<empty>'}.") from exc
        if (
            len(code) != 6
            or not code.isdigit()
            or not name
            or industry.casefold() in {"", "nan", "none", "nat", "<na>"}
        ):
            raise ValueError(
                f"Invalid candidate row: code={code!r}, name={name!r}, industry={industry!r}."
            )
        if code in seen_codes or rank in seen_ranks:
            raise ValueError("Candidate codes and ranks must both be unique.")
        seen_codes.add(code)
        seen_ranks.add(rank)
        candidates.append(Candidate(code=code, name=name, rank=rank, industry=industry))

    return sorted(candidates, key=lambda candidate: candidate.rank)


class ZhituApiClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float = 5.0,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        if session is None:
            session = requests.Session()
            session.trust_env = False
        self._session = session

    def get_quote(self, code: str) -> Quote:
        """Fetch one quote through the documented single-stock endpoint."""
        code = self._validate_code(code)
        response = self._session.get(
            API_URL.format(code=code),
            params={"token": self._token},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Unexpected quote response format.")
        return self._quote_from_payload(payload, code)

    def get_quotes(self, codes: Iterable[str], *, use_batch: bool = True) -> dict[str, Quote]:
        """Fetch a complete code-to-quote mapping, in batches of at most 20 codes.

        The public multi-stock endpoint is the default because it keeps each
        monitoring round far below the API request limit.  Callers that need
        the legacy per-stock behavior can pass ``use_batch=False``.
        """
        requested_codes = [self._validate_code(code) for code in codes]
        if len(set(requested_codes)) != len(requested_codes):
            raise ValueError("Quote request codes must be unique.")
        if not requested_codes:
            return {}

        if not use_batch:
            return {code: self.get_quote(code) for code in requested_codes}

        quotes: dict[str, Quote] = {}
        for start in range(0, len(requested_codes), MAX_BATCH_CODES):
            batch_codes = requested_codes[start : start + MAX_BATCH_CODES]
            response = self._session.get(
                BATCH_API_URL,
                params={"token": self._token, "stock_codes": ",".join(batch_codes)},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            batch_quotes = self._parse_batch_payload(response.json(), set(batch_codes))
            quotes.update(batch_quotes)

        # This guards against any accidental change to per-batch validation.
        missing_codes = set(requested_codes) - set(quotes)
        if missing_codes:
            raise ValueError(f"Batch quote response is missing codes: {', '.join(sorted(missing_codes))}")
        return {code: quotes[code] for code in requested_codes}

    @staticmethod
    def _validate_code(value: object) -> str:
        code = str(value).strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"Invalid stock code: {value!r}.")
        return code

    @classmethod
    def _quote_from_payload(cls, payload: Mapping[str, object], code: str) -> Quote:
        try:
            last_price = Decimal(str(payload["p"]))
            previous_close = Decimal(str(payload["yc"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Quote response is missing a valid p or yc field.") from exc
        if (
            not last_price.is_finite()
            or not previous_close.is_finite()
            or last_price <= 0
            or previous_close <= 0
        ):
            raise ValueError("Quote response contains a non-positive price.")

        return Quote(
            code=code,
            last_price=last_price,
            previous_close=previous_close,
            updated_at=str(payload.get("t", "")),
        )

    @classmethod
    def _parse_batch_payload(
        cls, payload: object, expected_codes: set[str]
    ) -> dict[str, Quote]:
        if not isinstance(payload, list):
            raise ValueError("Unexpected batch quote response format.")

        quotes: dict[str, Quote] = {}
        for item in payload:
            if not isinstance(item, Mapping):
                raise ValueError("Batch quote response contains a non-object item.")
            try:
                code = cls._validate_code(item["dm"])
            except KeyError as exc:
                raise ValueError("Batch quote response is missing dm.") from exc
            if code not in expected_codes:
                raise ValueError(f"Batch quote response contains unexpected code: {code}.")
            if code in quotes:
                raise ValueError(f"Batch quote response contains duplicate code: {code}.")
            quotes[code] = cls._quote_from_payload(item, code)

        missing_codes = expected_codes - set(quotes)
        if missing_codes:
            raise ValueError(f"Batch quote response is missing codes: {', '.join(sorted(missing_codes))}")
        return quotes


def collect_complete_snapshot(
    candidates: Iterable[Candidate],
    client: ZhituApiClient,
    logger: logging.Logger,
    *,
    use_batch: bool = True,
) -> dict[str, Quote] | None:
    """Return a complete snapshot so ranking never compares an incomplete pool."""
    candidate_list = list(candidates)
    candidate_codes = [candidate.code for candidate in candidate_list]
    try:
        quotes = client.get_quotes(candidate_codes, use_batch=use_batch)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Quote request failed for complete snapshot: %s", exc)
        return None

    missing_codes = [code for code in candidate_codes if code not in quotes]
    if missing_codes:
        logger.warning("Discarded incomplete snapshot; failed codes: %s", ", ".join(missing_codes))
        return None
    return quotes


def _trigger_sort_key(trigger: Trigger) -> tuple[Decimal, int, str]:
    return (
        trigger.quote.change_percent,
        trigger.candidate.rank,
        trigger.candidate.code,
    )


def select_triggers(candidates: Iterable[Candidate], quotes: dict[str, Quote]) -> list[Trigger]:
    """Return every threshold hit in deterministic notification order."""
    eligible = [
        Trigger(candidate=candidate, quote=quotes[candidate.code])
        for candidate in candidates
        if quotes[candidate.code].change_percent <= THRESHOLD_PERCENT
    ]
    return sorted(eligible, key=_trigger_sort_key)


def select_trigger(candidates: Iterable[Candidate], quotes: dict[str, Quote]) -> Trigger | None:
    triggers = select_triggers(candidates, quotes)
    return triggers[0] if triggers else None


def select_largest_decline(
    candidates: Iterable[Candidate], quotes: dict[str, Quote]
) -> Trigger | None:
    """Select the candidate with the most negative current change percentage."""
    selections = [
        Trigger(candidate=candidate, quote=quotes[candidate.code])
        for candidate in candidates
    ]
    if not selections:
        return None

    # More negative change wins; rank and code make every tie deterministic.
    return min(selections, key=_trigger_sort_key)


def format_trigger(trigger: Trigger, observed_at: datetime) -> str:
    quote = trigger.quote
    candidate = trigger.candidate
    return "\n".join(
        [
            "TRIGGERED - manual review required; no order was sent.",
            f"Observed at: {observed_at:%Y-%m-%d %H:%M:%S}",
            f"Quote updated: {quote.updated_at or 'not supplied'}",
            f"Stock: {candidate.code} {candidate.name}",
            f"Industry: {candidate.industry or 'not supplied'}",
            f"Candidate rank: {candidate.rank}",
            f"Last price: {quote.last_price}",
            f"Previous close: {quote.previous_close}",
            f"Change: {quote.change_percent:.4f}%",
        ]
    )


def configure_logger(now: datetime) -> logging.Logger:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("intraday_trigger_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_dir / f"intraday_trigger_{now:%Y-%m-%d}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def run_once(
    candidates: list[Candidate],
    client: ZhituApiClient,
    logger: logging.Logger,
    *,
    use_batch: bool = False,
) -> Trigger | None:
    snapshot = collect_complete_snapshot(candidates, client, logger, use_batch=use_batch)
    if snapshot is None:
        return None
    trigger = select_trigger(candidates, snapshot)
    if trigger:
        logger.warning("\n%s", format_trigger(trigger, datetime.now()))
    else:
        logger.info("No candidate has reached %.1f%% in this complete snapshot.", THRESHOLD_PERCENT)
    return trigger


def monitor(
    candidates: list[Candidate],
    client: ZhituApiClient,
    interval_seconds: int,
    *,
    use_batch: bool = False,
) -> int:
    now = datetime.now()
    logger = configure_logger(now)
    logger.info("Loaded %d candidates. Monitoring window: %s-%s.", len(candidates), START_TIME, END_TIME)
    if now.time() >= END_TIME:
        logger.info("Monitoring window has already ended; no market data was requested.")
        return 0

    while True:
        now = datetime.now()
        if now.time() >= END_TIME:
            logger.info("Monitoring window ended without a trigger.")
            return 0
        if now.time() < START_TIME:
            seconds_until_start = (datetime.combine(now.date(), START_TIME) - now).total_seconds()
            logger.info("Waiting %.0f seconds for the monitoring window.", seconds_until_start)
            time_module.sleep(min(60, max(1, seconds_until_start)))
            continue

        round_started = time_module.monotonic()
        trigger = run_once(candidates, client, logger, use_batch=use_batch)
        if trigger:
            return 0

        delay = interval_seconds - (time_module.monotonic() - round_started)
        if delay > 0:
            time_module.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES, help="Ranked 50-stock CSV.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="File containing zhituapi=TOKEN.")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Full-pool polling interval.")
    parser.add_argument("--once", action="store_true", help="Fetch one full snapshot immediately, outside the time window if needed.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval_seconds < MINIMUM_INTERVAL_SECONDS:
        raise ValueError("interval-seconds must be at least 60; Zhitu quotes update once per minute.")

    candidates = read_candidates(args.candidates)
    client = ZhituApiClient(get_token(args.env_file))
    use_batch = use_batch_quotes(args.env_file)
    if args.once:
        logger = configure_logger(datetime.now())
        run_once(candidates, client, logger, use_batch=use_batch)
        return 0
    return monitor(candidates, client, args.interval_seconds, use_batch=use_batch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
