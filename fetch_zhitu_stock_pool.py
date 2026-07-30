"""Refresh a known Shenzhen mainboard pool through Zhitu single-stock data.

The monthly Zhitu plan does not expose the full-stock-list API.  This module
therefore starts from the recent validated SZSE pool, checks every known code
through Zhitu's single-stock instrument endpoint, and preserves that pool's
industry mapping.  A missing mapping is explicitly labelled ``未知``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import requests

import fetch_szse_data


ZHITU_INSTRUMENT_URL = "https://api.zhituapi.com/hs/instrument/{code}.SZ"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 3
MAX_REQUESTS_PER_MINUTE = 480
DEFAULT_REQUEST_INTERVAL_SECONDS = 60.0 / MAX_REQUESTS_PER_MINUTE
MINIMUM_REFERENCE_COMPANY_COUNT = 1_000
MINIMUM_SUCCESSFUL_COMPANY_COUNT = 1_000
MINIMUM_SUCCESS_RATIO = 0.95
_SIX_DIGIT_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_BLANK_TEXT = frozenset({"", "nan", "none", "nat", "<na>"})


class ZhituStockPoolError(RuntimeError):
    """The Zhitu fallback could not produce a trustworthy stock pool."""


@dataclass(frozen=True)
class ZhituStockPoolBuild:
    company_frame: pd.DataFrame
    successful_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    unknown_industry_codes: tuple[str, ...]
    request_count: int


class RequestPacer:
    """Keep every request, including retries, below one shared rate cap."""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._sleep = sleep
        self._clock = clock
        self._next_allowed_at = 0.0
        self.request_count = 0

    def wait(self) -> None:
        now = self._clock()
        scheduled_at = max(now, self._next_allowed_at)
        self._next_allowed_at = scheduled_at + self.minimum_interval_seconds
        delay = scheduled_at - now
        if delay > 0:
            self._sleep(delay)
        self.request_count += 1


def build_session() -> requests.Session:
    """Create a direct session so inherited Windows proxy settings are ignored."""

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
    )
    return session


def _six_digit_code(value: object) -> str | None:
    match = _SIX_DIGIT_CODE_PATTERN.search(str(value or "").strip())
    return match.group(1) if match else None


def _text(value: object) -> str:
    normalized = str(value or "").strip()
    return "" if normalized.casefold() in _BLANK_TEXT else normalized


def _instrument_from_payload(payload: object) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        return nested
    return payload if {"ii", "name"}.intersection(payload) else None


def fetch_instrument(
    code: str,
    token: str,
    *,
    session: requests.Session,
    pacer: RequestPacer,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[Mapping[str, Any], int]:
    """Fetch one code, returning the payload and requests consumed.

    Request errors never include the URL in messages because that URL carries the
    token as a query parameter.
    """

    normalized_code = _six_digit_code(code)
    if normalized_code is None:
        raise ValueError(f"无效股票代码：{code!r}")
    normalized_token = token.strip()
    if not normalized_token:
        raise ZhituStockPoolError("智图 token 为空。")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数。")
    if max_retries <= 0:
        raise ValueError("max_retries 必须为正整数。")

    last_error = "unknown response"
    request_count = 0
    for attempt in range(1, max_retries + 1):
        pacer.wait()
        request_count += 1
        try:
            response = session.get(
                ZHITU_INSTRUMENT_URL.format(code=normalized_code),
                params={"token": normalized_token},
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        else:
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise ZhituStockPoolError(
                    f"智图股票 {normalized_code} 基础信息接口返回 HTTP "
                    f"{response.status_code}。"
                )
            else:
                try:
                    instrument = _instrument_from_payload(response.json())
                except ValueError:
                    instrument = None
                returned_code = (
                    _six_digit_code(instrument.get("ii")) if instrument is not None else None
                )
                if instrument is not None and (
                    returned_code is None or returned_code == normalized_code
                ):
                    return instrument, request_count
                last_error = "unexpected JSON payload"

        if attempt < max_retries:
            # The next attempt is also paced; this merely avoids immediate retries.
            time.sleep(float(attempt))

    raise ZhituStockPoolError(
        f"智图股票 {normalized_code} 基础信息不可用：{last_error}。"
    )


def refresh_mainboard_company_frame(
    reference_companies: pd.DataFrame,
    token: str,
    *,
    session: requests.Session | None = None,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    progress_callback: Callable[[int, int, str, bool], None] | None = None,
) -> ZhituStockPoolBuild:
    """Refresh known codes one by one while retaining or marking industries."""

    required_columns = {"股票代码", "股票名称", "所属行业"}
    missing_columns = required_columns.difference(reference_companies.columns)
    if missing_columns:
        missing_text = "、".join(sorted(missing_columns))
        raise ZhituStockPoolError(f"参考股票池缺少列：{missing_text}。")

    reference_rows: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for row in reference_companies.loc[:, ["股票代码", "股票名称", "所属行业"]].to_dict("records"):
        code = _six_digit_code(row["股票代码"])
        name = _text(row["股票名称"])
        if code is None or not name or code in seen_codes:
            raise ZhituStockPoolError("参考股票池包含无效、空名称或重复的股票代码。")
        seen_codes.add(code)
        reference_rows.append(
            {
                "股票代码": code,
                "股票名称": name,
                "所属行业": _text(row["所属行业"]) or "未知",
            }
        )

    if len(reference_rows) < MINIMUM_REFERENCE_COMPANY_COUNT:
        raise ZhituStockPoolError("参考股票池过小，拒绝用单股接口覆盖。")

    active_session = session or build_session()
    pacer = RequestPacer(request_interval_seconds)
    companies: list[dict[str, str]] = []
    successful_codes: list[str] = []
    failed_codes: list[str] = []
    unknown_industry_codes: list[str] = []
    total = len(reference_rows)

    for index, reference in enumerate(sorted(reference_rows, key=lambda item: item["股票代码"]), start=1):
        code = reference["股票代码"]
        name = reference["股票名称"]
        success = False
        try:
            instrument, _used_requests = fetch_instrument(
                code,
                token,
                session=active_session,
                pacer=pacer,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
            name = _text(instrument.get("name")) or name
            successful_codes.append(code)
            success = True
        except ZhituStockPoolError:
            # Keep the last validated entry on an isolated transient failure.
            failed_codes.append(code)

        industry = reference["所属行业"]
        if industry == "未知":
            unknown_industry_codes.append(code)
        companies.append(
            {
                "公司代码": code,
                "公司简称": name,
                "所属行业": industry,
            }
        )
        if progress_callback is not None:
            progress_callback(index, total, code, success)

    required_successes = max(
        MINIMUM_SUCCESSFUL_COMPANY_COUNT,
        math.ceil(total * MINIMUM_SUCCESS_RATIO),
    )
    if len(successful_codes) < required_successes:
        raise ZhituStockPoolError(
            f"智图单股基础信息成功 {len(successful_codes)}/{total}，"
            "不足以安全更新股票池。"
        )

    company_frame = pd.DataFrame(
        companies,
        columns=["公司代码", "公司简称", "所属行业"],
    )
    return ZhituStockPoolBuild(
        company_frame=company_frame,
        successful_codes=tuple(successful_codes),
        failed_codes=tuple(failed_codes),
        unknown_industry_codes=tuple(unknown_industry_codes),
        request_count=pacer.request_count,
    )


def _reference_etf_frame(reference_workbook: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(reference_workbook, sheet_name="ETF", dtype=str)
    except (OSError, ValueError):
        return pd.DataFrame()


def write_fallback_workbook(
    reference_workbook: Path,
    build: ZhituStockPoolBuild,
    output_path: Path,
) -> None:
    """Write the same workbook structure the screening pipeline already expects."""

    fetch_szse_data.write_workbook(
        _reference_etf_frame(reference_workbook),
        build.company_frame,
        output_path,
    )
