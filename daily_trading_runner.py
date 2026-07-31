"""Daily Shenzhen mainboard screening and intraday alert pipeline.

This is the single background entry point for the workflow.  It only reads
market data and sends notifications; it never connects to a broker or places
orders.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import shutil
import sys
import time as time_module
from uuid import uuid4
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import fetch_period_returns
import fetch_szse_data
import fetch_zhitu_stock_pool
import intraday_trigger_monitor as intraday
import szse_quant_app as screening
from strategy_backtest import rolling_parameter_optimizer as optimizer


PROJECT_DIR = Path(__file__).resolve().parent
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
AFTER_CLOSE_READY_TIME = time(16, 30)
MONITOR_START_TIME = time(9, 28)
MONITOR_END_TIME = time(9, 45)
FINAL_QUOTE_EARLIEST_TIME = time(9, 44)
FINAL_MAX_DECLINE_STATUS = "09:45候选池最大跌幅"
MONITOR_FINAL_NOTIFICATION_KEY = "monitor-final"
MONITOR_TRIGGER_NOTIFICATION_PREFIX = "monitor-trigger-"
DEFAULT_MONITOR_INTERVAL_SECONDS = 60
MINIMUM_MONITOR_INTERVAL_SECONDS = 60
DEFAULT_DAILY_QUOTE_LIMIT = 200
DEFAULT_QUOTE_RATE_LIMIT_PER_MINUTE = 1_000
MAX_QUOTE_AGE_SECONDS = 90
MAX_QUOTE_FUTURE_SKEW_SECONDS = 5
DEFAULT_WORKERS = 4
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.25
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_STOCK_POOL_FALLBACK_AGE_DAYS = 7
MARKET_SESSION_REFERENCE_CODE = "000001.SZ"
MARKET_SESSION_API_URL = "https://api.zhituapi.com/hs/indicators/{code}"
MARKET_SESSION_TIMEOUT_SECONDS = 15.0
SIGNAL_COLUMNS = (
    "预测日期",
    "数据截至日期",
    "预测状态",
    "预测股票代码",
    "预测股票名称",
    "预测所属行业",
    "预测排名",
    "预测得分",
    "风险过滤后候选数",
    "未过滤前50数",
    "监控日期",
    "实时状态",
    "实时股票代码",
    "实时股票名称",
    "实时所属行业",
    "实时候选排名",
    "实时最新价",
    "实时昨收价",
    "实时跌幅（%）",
    "实时触发时间",
    "行情更新时间",
    "更新时间",
    "备注",
)
PREDICTION_COLUMNS = SIGNAL_COLUMNS[:10]
REALTIME_COLUMNS = SIGNAL_COLUMNS[9:]
FACTOR_ERROR_COLUMNS = ("序号", "股票代码", "股票名称", "失败原因")
STATE_SCHEMA_VERSION = 1
LOCK_STALE_SECONDS = 6 * 60 * 60
LOCK_INITIALIZATION_GRACE_SECONDS = 5


class PipelineError(RuntimeError):
    """A daily pipeline stage could not produce a trustworthy result."""


class PushPlusError(PipelineError):
    """PushPlus did not accept a notification."""


def _direct_http_session() -> requests.Session:
    """Create an HTTP session that ignores inherited proxy configuration."""

    session = requests.Session()
    session.trust_env = False
    return session


@dataclass(frozen=True)
class PipelinePaths:
    project_dir: Path

    @property
    def stock_pool(self) -> Path:
        return self.project_dir / "深交所数据.xlsx"

    @property
    def top_fifty(self) -> Path:
        return self.project_dir / "前 50 名（含所属行业）.csv"

    @property
    def combined_signal(self) -> Path:
        return self.project_dir / "每日交易信号.csv"

    @property
    def output_dir(self) -> Path:
        return self.project_dir / "daily_trading_outputs"

    @property
    def archive_dir(self) -> Path:
        return self.output_dir / "archive"

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def prediction_current(self) -> Path:
        return self.output_dir / "每日预测第一名.csv"

    @property
    def realtime_current(self) -> Path:
        return self.output_dir / "每日实时检测结果.csv"

    @property
    def returns_workbook(self) -> Path:
        return (
            self.project_dir
            / "strategy_backtest"
            / "outputs"
            / "input_data"
            / "深市主板每日涨跌幅_滚动更新.xlsx"
        )

    @property
    def optimizer_output_dir(self) -> Path:
        return (
            self.project_dir
            / "strategy_backtest"
            / "outputs"
            / "rolling_parameter_updates"
        )

    @property
    def current_parameter_file(self) -> Path:
        return self.optimizer_output_dir / optimizer.CURRENT_PARAMETER_FILENAME

    def archive_for(self, trading_day: date) -> Path:
        return self.archive_dir / trading_day.isoformat()

    def state_file(self, trading_day: date) -> Path:
        return self.archive_for(trading_day) / "运行状态.json"


@dataclass(frozen=True)
class ReturnUpdate:
    daily_returns: pd.DataFrame
    failures: pd.DataFrame
    latest_market_day: date | None
    appended_rows: int


@dataclass(frozen=True)
class StockPoolRefreshResult:
    refreshed: bool
    source_day: date
    fallback_reason: str = ""
    source: str = "szse"
    unknown_industry_codes: tuple[str, ...] = ()
    failed_zhitu_codes: tuple[str, ...] = ()
    zhitu_request_count: int = 0


@dataclass(frozen=True)
class PredictionResult:
    record: dict[str, object]
    top_fifty: pd.DataFrame
    top_ten: pd.DataFrame
    factors: pd.DataFrame
    factor_errors: pd.DataFrame


class ProcessLock:
    """A small cross-process lock that becomes recoverable after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False
        self.owner_token = uuid4().hex

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _read_payload(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _can_reclaim_existing_lock(self) -> bool:
        payload = self._read_payload()
        try:
            age_seconds = time_module.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if not payload or not payload.get("owner_token"):
            return age_seconds > LOCK_INITIALIZATION_GRACE_SECONDS
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if pid and self._pid_is_running(pid):
            return False
        return bool(pid) or age_seconds > LOCK_STALE_SECONDS

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if not self._can_reclaim_existing_lock():
                    raise PipelineError(f"已有后台任务正在运行：{self.path}") from exc
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        else:
            raise PipelineError(f"无法取得任务锁：{self.path}")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "owner_token": self.owner_token,
                    "started_at": china_now().isoformat(),
                },
                handle,
                ensure_ascii=False,
            )
        self.acquired = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.acquired and self._read_payload().get("owner_token") == self.owner_token:
            self.path.unlink(missing_ok=True)


class PushPlusNotifier:
    """Send the requested default PushPlus public-account notification."""

    endpoint = "https://www.pushplus.plus/send"

    def __init__(self, token: str | None, logger: logging.Logger, *, enabled: bool) -> None:
        self._token = token
        self._logger = logger
        self._enabled = enabled
        self._session = _direct_http_session()

    @classmethod
    def from_env(
        cls,
        env_file: Path,
        logger: logging.Logger,
        *,
        enabled: bool,
    ) -> "PushPlusNotifier":
        token = (
            os.environ.get("PushPlusapi")
            or os.environ.get("PushPlus_token")
            or read_env_value(env_file, "PushPlusapi")
            or read_env_value(env_file, "PushPlus_token")
        )
        return cls(token, logger, enabled=enabled)

    def send(self, title: str, content: str) -> bool:
        if not self._enabled:
            self._logger.info("PushPlus disabled for this invocation: %s", title)
            return False
        if not self._token:
            raise PushPlusError(".env 缺少 PushPlusapi 或 PushPlus_token。")
        try:
            response = self._session.post(
                self.endpoint,
                data={
                    "token": self._token,
                    "title": title,
                    "content": content,
                    "template": "html",
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PushPlusError(f"PushPlus 请求失败：{exc}") from exc

        if not isinstance(payload, Mapping) or int(payload.get("code", -1)) != 200:
            message = payload.get("msg") if isinstance(payload, Mapping) else "响应不是 JSON 对象"
            raise PushPlusError(f"PushPlus 推送失败：{message}")
        self._logger.info("PushPlus notification sent: %s", title)
        return True


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE)


def read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("\"'") or None
    return None


def configure_logger(paths: PipelinePaths, now: datetime) -> logging.Logger:
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("daily_trading_runner")
    logger.setLevel(logging.INFO)
    close_logger(logger)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(
        paths.logs_dir / f"daily_runner_{now:%Y-%m-%d}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def _temporary_sibling_path(path: Path) -> Path:
    return path.with_name(f".{path.stem}.{uuid4().hex}.tmp{path.suffix}")


def _discard_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _json_safe(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric_value = float(value)
        return numeric_value if math.isfinite(numeric_value) else None
    return value


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling_path(path)
    try:
        temporary_path.write_text(
            json.dumps(
                _json_safe(payload),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
                default=str,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        _discard_temporary_file(temporary_path)


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling_path(path)
    try:
        frame.to_csv(temporary_path, index=False, encoding="utf-8-sig")
        temporary_path.replace(path)
    finally:
        _discard_temporary_file(temporary_path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling_path(path)
    try:
        temporary_path.write_bytes(content)
        temporary_path.replace(path)
    finally:
        _discard_temporary_file(temporary_path)


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling_path(target)
    try:
        shutil.copy2(source, temporary_path)
        temporary_path.replace(target)
    finally:
        _discard_temporary_file(temporary_path)


def atomic_write_workbook(path: Path, writer: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling_path(path)
    try:
        writer(temporary_path)
        temporary_path.replace(path)
    finally:
        _discard_temporary_file(temporary_path)


def load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def update_state(
    paths: PipelinePaths,
    trading_day: date,
    section: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    state_path = paths.state_file(trading_day)
    state = load_json(state_path)
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("trading_day", trading_day.isoformat())
    state.setdefault("stages", {})
    stages = state["stages"]
    if not isinstance(stages, dict):
        stages = {}
        state["stages"] = stages
    stages[section] = {"updated_at": china_now().isoformat(), **dict(payload)}
    atomic_write_json(state_path, state)
    return state


def state_section(paths: PipelinePaths, trading_day: date, section: str) -> dict[str, object]:
    stages = load_json(paths.state_file(trading_day)).get("stages")
    if not isinstance(stages, Mapping):
        return {}
    value = stages.get(section)
    return dict(value) if isinstance(value, Mapping) else {}


def completed_stage(paths: PipelinePaths, trading_day: date, section: str) -> bool:
    return state_section(paths, trading_day, section).get("status") == "completed"


def _as_date(value: object) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed).date()


def _as_code(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def previous_weekday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def resolve_after_close_day(now: datetime, explicit_day: date | None) -> date:
    if explicit_day is not None:
        return explicit_day
    return now.date() if now.timetz().replace(tzinfo=None) >= AFTER_CLOSE_READY_TIME else previous_weekday(now.date())


def ensure_archive_dir(paths: PipelinePaths, trading_day: date) -> Path:
    archive = paths.archive_for(trading_day)
    archive.mkdir(parents=True, exist_ok=True)
    return archive


def _validated_reference_stock_pool(
    paths: PipelinePaths,
    *,
    target_day: date,
    source_error: Exception,
) -> tuple[pd.DataFrame, date, int]:
    """Load the recent official pool needed to retain its industry mapping."""

    if not paths.stock_pool.is_file():
        raise PipelineError("深交所股票池刷新失败，且没有可用的已校验股票池。") from source_error
    try:
        companies = screening.load_mainboard_companies(paths.stock_pool)
    except (OSError, ValueError, KeyError) as validation_error:
        raise PipelineError("深交所股票池刷新失败，且现有股票池未通过完整性校验。") from validation_error
    if companies.empty:
        raise PipelineError("深交所股票池刷新失败，且现有股票池为空。") from source_error

    source_day = datetime.fromtimestamp(
        paths.stock_pool.stat().st_mtime, tz=CHINA_TIMEZONE
    ).date()
    age_days = max(0, (target_day - source_day).days)
    if age_days > MAX_STOCK_POOL_FALLBACK_AGE_DAYS:
        raise PipelineError(
            "深交所股票池刷新失败，现有已校验股票池已超过 "
            f"{MAX_STOCK_POOL_FALLBACK_AGE_DAYS} 天，拒绝继续使用。"
        ) from source_error
    return companies, source_day, age_days


def refresh_stock_pool(
    paths: PipelinePaths,
    *,
    target_day: date,
    logger: logging.Logger,
) -> StockPoolRefreshResult:
    temporary_path = _temporary_sibling_path(paths.stock_pool)
    try:
        try:
            fetch_szse_data.main(["--output", str(temporary_path)])
        except fetch_szse_data.SzseApiError as szse_error:
            companies, source_day, age_days = _validated_reference_stock_pool(
                paths,
                target_day=target_day,
                source_error=szse_error,
            )

            def log_zhitu_progress(
                index: int,
                total: int,
                code: str,
                success: bool,
            ) -> None:
                if index == 1 or index == total or index % 50 == 0:
                    logger.info(
                        "Zhitu single-stock verification %s/%s; latest=%s; status=%s.",
                        index,
                        total,
                        code,
                        "ok" if success else "retained previous record",
                    )

            try:
                token = intraday.get_token(paths.project_dir / ".env")
                zhitu_build = fetch_zhitu_stock_pool.refresh_mainboard_company_frame(
                    companies,
                    token,
                    progress_callback=log_zhitu_progress,
                )
                fetch_zhitu_stock_pool.write_fallback_workbook(
                    paths.stock_pool,
                    zhitu_build,
                    temporary_path,
                )
                # Validate the replacement through the same loader used by the pipeline.
                screening.load_mainboard_companies(temporary_path)
            except (
                OSError,
                ValueError,
                KeyError,
                fetch_zhitu_stock_pool.ZhituStockPoolError,
            ) as zhitu_error:
                logger.warning(
                    "SZSE stock-pool refresh failed; Zhitu fallback also failed; using validated "
                    "pool from %s (%s days old). SZSE=%s; Zhitu=%s",
                    source_day.isoformat(),
                    age_days,
                    szse_error,
                    zhitu_error,
                )
                return StockPoolRefreshResult(
                    False,
                    source_day,
                    f"SZSE: {szse_error}; Zhitu: {zhitu_error}",
                    source="local",
                )

            temporary_path.replace(paths.stock_pool)
            refreshed_day = datetime.fromtimestamp(
                paths.stock_pool.stat().st_mtime, tz=CHINA_TIMEZONE
            ).date()
            logger.warning(
                "SZSE stock-pool refresh failed; Zhitu verified %s known mainboard stocks "
                "one by one, retained %s failed records, marked %s industries as unknown, "
                "and used %s requests.",
                len(zhitu_build.company_frame),
                len(zhitu_build.failed_codes),
                len(zhitu_build.unknown_industry_codes),
                zhitu_build.request_count,
            )
            return StockPoolRefreshResult(
                True,
                refreshed_day,
                str(szse_error),
                source="zhitu",
                unknown_industry_codes=zhitu_build.unknown_industry_codes,
                failed_zhitu_codes=zhitu_build.failed_codes,
                zhitu_request_count=zhitu_build.request_count,
            )

        temporary_path.replace(paths.stock_pool)
        source_day = datetime.fromtimestamp(
            paths.stock_pool.stat().st_mtime, tz=CHINA_TIMEZONE
        ).date()
        return StockPoolRefreshResult(True, source_day, source="szse")
    finally:
        _discard_temporary_file(temporary_path)


def _read_excel_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        with pd.ExcelFile(path) as workbook:
            if sheet_name not in workbook.sheet_names:
                return pd.DataFrame()
            return pd.read_excel(workbook, sheet_name=sheet_name, dtype={"股票代码": str})
    except (OSError, ValueError):
        return pd.DataFrame()


def _normalize_daily_returns(frame: pd.DataFrame) -> pd.DataFrame:
    columns = list(fetch_period_returns.DAILY_RETURN_COLUMNS)
    normalized = frame.reindex(columns=columns).copy()
    if normalized.empty:
        return normalized
    normalized["股票代码"] = normalized["股票代码"].map(_as_code)
    normalized["交易日期"] = pd.to_datetime(normalized["交易日期"], errors="coerce")
    normalized["前一交易日"] = pd.to_datetime(normalized["前一交易日"], errors="coerce")
    normalized["当日涨跌幅（%）"] = pd.to_numeric(
        normalized["当日涨跌幅（%）"], errors="coerce"
    )
    normalized = normalized.dropna(subset=["股票代码", "交易日期"])
    normalized = normalized.sort_values(["交易日期", "股票代码"], kind="stable")
    return normalized.drop_duplicates(["股票代码", "交易日期"], keep="last").reset_index(drop=True)


def _return_codes_on_day(daily_returns: pd.DataFrame, trading_day: date) -> set[str]:
    normalized = _normalize_daily_returns(daily_returns)
    if normalized.empty:
        return set()
    return set(
        normalized.loc[
            normalized["交易日期"].dt.date.eq(trading_day), "股票代码"
        ].map(_as_code)
    )


def _required_return_codes(daily_returns: pd.DataFrame, target_day: date) -> set[str]:
    """Use the prior actual market day to exclude stocks suspended before the run."""
    normalized = _normalize_daily_returns(daily_returns)
    if normalized.empty:
        return set()
    earlier_days = normalized.loc[
        normalized["交易日期"].dt.date.lt(target_day), "交易日期"
    ]
    if earlier_days.empty:
        return set()
    previous_day = pd.Timestamp(earlier_days.max()).date()
    return _return_codes_on_day(normalized, previous_day)


def _missing_required_return_codes(daily_returns: pd.DataFrame, target_day: date) -> set[str]:
    return _required_return_codes(daily_returns, target_day) - _return_codes_on_day(
        daily_returns, target_day
    )


def _return_failure_codes(failures: pd.DataFrame) -> set[str]:
    if failures.empty or "股票代码" not in failures:
        return set()
    return {
        code
        for code in failures["股票代码"].map(_as_code)
        if len(code) == 6 and code.isdigit()
    }


def _exclude_return_failure_companies(
    companies: pd.DataFrame, excluded_codes: set[str]
) -> pd.DataFrame:
    if not excluded_codes:
        return companies.copy()
    if "股票代码" not in companies:
        raise PipelineError("股票池缺少“股票代码”列。")
    remaining = companies.loc[
        ~companies["股票代码"].map(_as_code).isin(excluded_codes)
    ].copy()
    if remaining.empty:
        raise PipelineError("收益异常已排除全部股票池，无法生成当日候选。")
    return remaining


def _summary_from_daily(companies: pd.DataFrame, daily_returns: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if daily_returns.empty:
        return pd.DataFrame(columns=fetch_period_returns.SUMMARY_COLUMNS)
    daily = _normalize_daily_returns(daily_returns)
    for company in companies.to_dict("records"):
        code = str(company["股票代码"])
        rows_for_code = daily.loc[daily["股票代码"].eq(code)].sort_values(
            "交易日期", kind="stable"
        )
        if rows_for_code.empty:
            continue
        first = rows_for_code.iloc[0]
        last = rows_for_code.iloc[-1]
        start_close = pd.to_numeric(pd.Series([first["前收盘价（前复权）"]]), errors="coerce").iloc[0]
        if pd.isna(start_close) or float(start_close) <= 0:
            start_close = pd.to_numeric(pd.Series([first["收盘价（前复权）"]]), errors="coerce").iloc[0]
        end_close = pd.to_numeric(pd.Series([last["收盘价（前复权）"]]), errors="coerce").iloc[0]
        if pd.isna(start_close) or pd.isna(end_close) or float(start_close) <= 0:
            change_pct: float | None = None
        else:
            change_pct = (float(end_close) / float(start_close) - 1.0) * 100.0
        rows.append(
            {
                "序号": company["序号"],
                "股票代码": code,
                "股票名称": company["股票名称"],
                "请求开始日期": pd.Timestamp(first["交易日期"]).date(),
                "实际开始交易日": pd.Timestamp(first["交易日期"]).date(),
                "开始收盘价（前复权）": start_close,
                "请求结束日期": pd.Timestamp(last["交易日期"]).date(),
                "实际结束交易日": pd.Timestamp(last["交易日期"]).date(),
                "结束收盘价（前复权）": end_close,
                "区间涨跌幅（%）": change_pct,
                "数据来源": last.get("数据来源", "未知来源"),
                "缓存命中": bool(last.get("缓存命中", False)),
            }
        )
    return pd.DataFrame(rows, columns=fetch_period_returns.SUMMARY_COLUMNS)


def _bootstrap_returns_workbook(paths: PipelinePaths) -> None:
    if paths.returns_workbook.is_file():
        return
    candidates = [
        path
        for path in paths.returns_workbook.parent.glob("深市主板每日涨跌幅_*.xlsx")
        if path != paths.returns_workbook and "滚动更新" not in path.name
    ]
    if not candidates:
        raise PipelineError("没有可用于初始化的历史每日涨跌幅工作簿。")
    source = max(candidates, key=lambda path: path.stat().st_mtime)
    atomic_copy(source, paths.returns_workbook)


def update_incremental_return_history(
    paths: PipelinePaths,
    companies: pd.DataFrame,
    target_day: date,
    logger: logging.Logger,
) -> ReturnUpdate:
    """Append only missing trading days to the fixed returns workbook."""
    _bootstrap_returns_workbook(paths)
    existing = _normalize_daily_returns(
        _read_excel_sheet(paths.returns_workbook, "每日涨跌幅明细")
    )
    existing_failures = _read_excel_sheet(paths.returns_workbook, "失败明细")
    latest_day = _as_date(existing["交易日期"].max()) if not existing.empty else None
    if latest_day is not None and latest_day >= target_day:
        missing_codes = _missing_required_return_codes(existing, target_day)
        if not missing_codes:
            return ReturnUpdate(existing, existing_failures, latest_day, 0)
        collection_companies = companies.loc[
            companies["股票代码"].map(_as_code).isin(missing_codes)
        ]
        requested_start = target_day
        logger.warning(
            "Retrying %s active stocks missing return data for %s.",
            len(collection_companies),
            target_day,
        )
    else:
        collection_companies = companies
        requested_start = (latest_day + timedelta(days=1)) if latest_day else target_day

    logger.info("Updating daily returns from %s through %s.", requested_start, target_day)
    summary, new_daily, new_failures, collection_summary = fetch_period_returns.collect_period_returns(
        collection_companies.loc[:, ["序号", "股票代码", "股票名称"]],
        requested_start=requested_start,
        requested_end=target_day,
        max_companies=None,
        cache_hours=24.0,
        force_refresh=False,
        workers=DEFAULT_WORKERS,
        request_interval_seconds=DEFAULT_REQUEST_INTERVAL_SECONDS,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    logger.info("Incremental return collection: %s", collection_summary)
    new_daily = _normalize_daily_returns(new_daily)
    merged_daily = _normalize_daily_returns(pd.concat([existing, new_daily], ignore_index=True))
    latest_day = _as_date(merged_daily["交易日期"].max()) if not merged_daily.empty else None
    successful_codes = set(summary.get("股票代码", pd.Series(dtype=str)).map(_as_code))
    retained_failures = existing_failures.copy()
    if not retained_failures.empty and "股票代码" in retained_failures:
        retained_failures = retained_failures.loc[
            ~retained_failures["股票代码"].map(_as_code).isin(successful_codes)
        ]
    merged_failures = pd.concat([retained_failures, new_failures], ignore_index=True)
    if not merged_failures.empty and "股票代码" in merged_failures:
        merged_failures["股票代码"] = merged_failures["股票代码"].map(_as_code)
        merged_failures = merged_failures.drop_duplicates("股票代码", keep="last")

    summary_frame = _summary_from_daily(companies, merged_daily)
    atomic_write_workbook(
        paths.returns_workbook,
        lambda path: fetch_period_returns.write_results_workbook(
            summary_frame,
            merged_daily,
            merged_failures,
            path,
        ),
    )
    return ReturnUpdate(merged_daily, merged_failures, latest_day, len(new_daily))


def _selected_and_risks(settings: Mapping[str, object]) -> tuple[dict[str, bool], dict[str, bool]]:
    selected = {
        key: bool(settings.get(f"szse_quant_filter_{key}", False))
        for key in screening.SCORING_INDICATOR_KEYS
    }
    candlestick_patterns = set(settings.get("szse_quant_risk_candlestick_patterns", []))
    selected_risks = {
        "bias_high": bool(settings.get("szse_quant_risk_bias_high", False)),
        "upper_shadow": bool(settings.get("szse_quant_risk_upper_shadow", False)),
        "resistance_60_day": bool(
            settings.get("szse_quant_risk_resistance_60_day", False)
        ),
        **{
            risk_key: risk_key in candlestick_patterns
            for risk_key in screening.CANDLESTICK_RISK_PATTERN_KEYS
        },
    }
    return selected, selected_risks


def _pair_of_numbers(settings: Mapping[str, object], key: str, *, integer: bool = False) -> tuple[Any, Any]:
    value = settings[key]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PipelineError(f"筛选参数 {key} 不是两个数值组成的区间。")
    converter = int if integer else float
    return converter(value[0]), converter(value[1])


def _score_options(settings: Mapping[str, object]) -> dict[str, object]:
    return {
        "turnover_range": _pair_of_numbers(settings, "szse_quant_filter_turnover_range"),
        "float_market_cap_range_yi": _pair_of_numbers(
            settings, "szse_quant_filter_float_market_cap_range_yi"
        ),
        "pct_change_range": _pair_of_numbers(settings, "szse_quant_filter_pct_change_range"),
        "amplitude_threshold": float(settings["szse_quant_filter_amplitude_threshold"]),
        "rsi_range": _pair_of_numbers(settings, "szse_quant_filter_rsi_range"),
        "macd_golden_cross_lookback_days": int(
            settings["szse_quant_filter_macd_golden_cross_lookback_days"]
        ),
        "kdj_healthy_golden_cross_age_range": _pair_of_numbers(
            settings, "szse_quant_filter_kdj_healthy_golden_cross_age_range", integer=True
        ),
        "macd_dea_minus_dif_range": _pair_of_numbers(
            settings, "szse_quant_filter_macd_dea_minus_dif_range"
        ),
        "volume_ratio_range": _pair_of_numbers(
            settings, "szse_quant_filter_volume_ratio_range"
        ),
        "require_all": bool(settings["szse_quant_filter_require_all"]),
    }


def _progress_logger(logger: logging.Logger):
    def callback(
        completed: int,
        total: int,
        code: str,
        cache_hits: int,
        succeeded: int,
        failed: int,
    ) -> None:
        logger.info(
            "Factors %s/%s, current=%s, cache=%s, success=%s, failed=%s",
            completed,
            total,
            code,
            cache_hits,
            succeeded,
            failed,
        )

    return callback


def _factor_codes_on_day(factors: pd.DataFrame, target_day: date) -> set[str]:
    if factors.empty or not {"股票代码", "数据日期"}.issubset(factors.columns):
        return set()
    factor_dates = pd.to_datetime(factors["数据日期"], errors="coerce")
    return set(
        factors.loc[factor_dates.dt.date.eq(target_day), "股票代码"].map(_as_code)
    )


def _canonical_factor_errors(factor_errors: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(factor_errors, pd.DataFrame):
        return pd.DataFrame(columns=FACTOR_ERROR_COLUMNS)
    errors = factor_errors.reindex(columns=FACTOR_ERROR_COLUMNS).copy()
    if errors.empty:
        return errors
    errors["股票代码"] = errors["股票代码"].map(_as_code)
    errors["股票名称"] = errors["股票名称"].fillna("").astype(str)
    errors["失败原因"] = errors["失败原因"].fillna("未说明原因。").astype(str)
    return errors.sort_values("序号", kind="stable").reset_index(drop=True)


def _prepare_daily_factors(
    companies: pd.DataFrame,
    factors: pd.DataFrame,
    factor_errors: pd.DataFrame,
    target_day: date,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Keep only valid same-day factors and record every excluded pool member."""

    if "股票代码" not in companies:
        raise PipelineError("股票池缺少“股票代码”列。")
    if factors.empty or not {"股票代码", "数据日期"}.issubset(factors.columns):
        raise PipelineError(f"日线因子未覆盖 {target_day.isoformat()}，没有可用因子。")

    factor_dates = pd.to_datetime(factors["数据日期"], errors="coerce")
    same_day_factors = factors.loc[factor_dates.dt.date.eq(target_day)].copy()
    if same_day_factors.empty:
        actual_factor_day = _as_date(factor_dates.max())
        raise PipelineError(
            f"日线因子未覆盖 {target_day.isoformat()}，实际最新日期：{actual_factor_day or '无'}。"
        )

    company_rows: dict[str, Mapping[str, object]] = {}
    for company in companies.to_dict("records"):
        code = _as_code(company.get("股票代码", ""))
        if code:
            company_rows.setdefault(code, company)
    company_codes = set(company_rows)
    errors = _canonical_factor_errors(factor_errors)
    factor_codes = _factor_codes_on_day(same_day_factors, target_day)
    error_codes = set(errors["股票代码"]) if not errors.empty else set()
    excluded_codes = (company_codes - factor_codes) | (company_codes & error_codes)

    recorded_codes = company_codes & error_codes
    unrecorded_codes = sorted(excluded_codes - recorded_codes)
    if unrecorded_codes:
        recovered_errors = pd.DataFrame(
            [
                {
                    "序号": company_rows[code].get("序号", ""),
                    "股票代码": code,
                    "股票名称": str(company_rows[code].get("股票名称", "")),
                    "失败原因": (
                        f"未生成截至 {target_day.isoformat()} 的有效日线因子，"
                        "已从当日评分候选中排除。"
                    ),
                }
                for code in unrecorded_codes
            ]
        )
        errors = _canonical_factor_errors(pd.concat([errors, recovered_errors], ignore_index=True))

    if excluded_codes:
        same_day_factors = same_day_factors.loc[
            ~same_day_factors["股票代码"].map(_as_code).isin(excluded_codes)
        ].copy()
    if same_day_factors.empty:
        raise PipelineError("所有股票的当日因子都不可用，无法生成候选。")
    same_day_factors = same_day_factors.sort_values("序号", kind="stable").reset_index(drop=True)
    return same_day_factors, errors, sorted(excluded_codes)


def read_cached_factor_frame(
    paths: PipelinePaths,
    companies: pd.DataFrame,
    target_day: date,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Read a completed daily factor cache without issuing any market request."""

    cache_dir = paths.project_dir / "data_cache" / "szse_quant" / target_day.isoformat()
    factor_rows: list[dict[str, object]] = []
    error_rows: list[dict[str, object]] = []
    cache_hits = 0
    for company in companies.to_dict("records"):
        code = _as_code(company.get("股票代码", ""))
        cache_path = cache_dir / f"{code}.json"
        reason: str | None = None
        payload: Mapping[str, object] | None = None
        try:
            decoded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(decoded, Mapping):
                payload = decoded
        except (OSError, ValueError, json.JSONDecodeError):
            reason = "未找到或无法读取当日因子缓存。"

        if payload is not None:
            cache_hits += 1
            if payload.get("schema_version") != screening.CACHE_SCHEMA_VERSION:
                reason = "当日因子缓存版本不匹配。"
            elif payload.get("as_of_date") != target_day.isoformat():
                reason = "当日因子缓存的截至日期不匹配。"
            elif payload.get("error"):
                reason = f"缓存记录失败：{payload['error']}"
            elif payload.get("factor_cache_version") != screening.FACTOR_CACHE_VERSION:
                reason = "当日因子缓存缺少兼容的因子结果。"
            else:
                cached_factors = payload.get("factors")
                if not isinstance(cached_factors, Mapping):
                    reason = "当日因子缓存缺少因子结果。"
                elif _as_date(cached_factors.get("数据日期")) != target_day:
                    reason = "缓存因子的数据日期不是当日。"
                else:
                    factor_rows.append(
                        {
                            "序号": company.get("序号", ""),
                            "股票代码": code,
                            "股票名称": str(company.get("股票名称", "")),
                            "所属行业": str(company.get("所属行业", "未分类")),
                            "数据来源": str(payload.get("source") or "本地缓存"),
                            "缓存命中": True,
                            **dict(cached_factors),
                        }
                    )
        elif reason is None:
            reason = "当日因子缓存格式无效。"

        if reason is not None:
            error_rows.append(
                {
                    "序号": company.get("序号", ""),
                    "股票代码": code,
                    "股票名称": str(company.get("股票名称", "")),
                    "失败原因": reason,
                }
            )

    factors = pd.DataFrame(factor_rows)
    if not factors.empty:
        factors = factors.sort_values("序号", kind="stable").reset_index(drop=True)
    errors = _canonical_factor_errors(pd.DataFrame(error_rows, columns=FACTOR_ERROR_COLUMNS))
    return factors, errors, {
        "总数": len(companies),
        "缓存命中": cache_hits,
        "成功": len(factors),
        "失败": len(errors),
    }


def build_prediction(
    paths: PipelinePaths,
    companies: pd.DataFrame,
    factors: pd.DataFrame,
    target_day: date,
    logger: logging.Logger,
) -> PredictionResult:
    if not paths.current_parameter_file.is_file():
        raise PipelineError(f"缺少固定优化参数文件：{paths.current_parameter_file}")
    settings = screening.default_screening_settings()
    screening.apply_optimized_parameter_overrides(settings, paths.current_parameter_file)
    selected, selected_risks = _selected_and_risks(settings)
    score_options = _score_options(settings)
    risk_ranked, eligible_count, _ = screening.score_and_select(
        factors,
        selected,
        selected_risks=selected_risks,
        top_n=screening.PREDICTION_REVIEW_TOP_N,
        **score_options,
    )
    top_ten = risk_ranked.head(10).reset_index(drop=True)
    review_score_options = {**score_options, "require_all": False}
    unfiltered_ranked, _, _ = screening.score_and_select(
        factors,
        selected,
        selected_risks={},
        top_n=screening.PREDICTION_REVIEW_TOP_N,
        **review_score_options,
    )
    top_fifty = screening.prepare_prediction_review_candidates(unfiltered_ranked, factors)
    if len(top_fifty) != screening.PREDICTION_REVIEW_TOP_N:
        raise PipelineError(
            f"未过滤评分候选只有 {len(top_fifty)} 只，不能覆盖固定前 50 名文件。"
        )

    if top_ten.empty:
        first: dict[str, object] = {
            "预测状态": "风险过滤后无第一名",
            "预测股票代码": "",
            "预测股票名称": "",
            "预测所属行业": "",
            "预测排名": "",
            "预测得分": "",
        }
    else:
        row = top_ten.iloc[0]
        first = {
            "预测状态": "已生成",
            "预测股票代码": _as_code(row.get("股票代码", "")),
            "预测股票名称": str(row.get("股票名称", "")),
            "预测所属行业": str(row.get("所属行业", "")),
            "预测排名": 1,
            "预测得分": row.get("得分", ""),
        }
    data_as_of = _as_date(factors.get("数据日期", pd.Series(dtype=object)).max())
    record: dict[str, object] = {
        "预测日期": target_day.isoformat(),
        "数据截至日期": data_as_of.isoformat() if data_as_of else "",
        **first,
        "风险过滤后候选数": eligible_count,
        "未过滤前50数": len(top_fifty),
    }
    logger.info("Prediction record: %s", record)
    return PredictionResult(record, top_fifty, top_ten, factors, pd.DataFrame())


def _single_row_frame(record: Mapping[str, object], columns: Iterable[str]) -> pd.DataFrame:
    return pd.DataFrame([{column: record.get(column, "") for column in columns}])


def _load_signal_record(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    except (OSError, ValueError, pd.errors.ParserError):
        return {}
    return frame.iloc[0].to_dict() if not frame.empty else {}


def _load_current_signal(paths: PipelinePaths) -> dict[str, object]:
    return _load_signal_record(paths.combined_signal)


def _write_prediction_artifacts(
    paths: PipelinePaths,
    target_day: date,
    prediction: PredictionResult,
    factor_errors: pd.DataFrame,
    return_update: ReturnUpdate,
) -> None:
    archive = ensure_archive_dir(paths, target_day)
    atomic_write_csv(prediction.top_fifty, paths.top_fifty)
    atomic_write_csv(_single_row_frame(prediction.record, PREDICTION_COLUMNS), paths.prediction_current)
    atomic_write_csv(_single_row_frame(prediction.record, PREDICTION_COLUMNS), archive / "每日预测第一名.csv")
    atomic_write_csv(prediction.top_ten, archive / "风险过滤后得分前10.csv")
    atomic_write_csv(prediction.top_fifty, archive / "前 50 名（含所属行业）.csv")
    atomic_write_csv(prediction.factors, archive / "每日因子.csv")
    atomic_write_csv(factor_errors, archive / "每日因子错误.csv")
    atomic_write_csv(return_update.daily_returns, archive / "累计每日收益.csv")
    signal = {
        **prediction.record,
        "监控日期": "",
        "实时状态": "待监控",
        "实时股票代码": "",
        "实时股票名称": "",
        "实时所属行业": "",
        "实时候选排名": "",
        "实时最新价": "",
        "实时昨收价": "",
        "实时跌幅（%）": "",
        "实时触发时间": "",
        "行情更新时间": "",
        "更新时间": china_now().isoformat(),
        "备注": "等待下一交易日09:28-09:45实时检测。",
    }
    atomic_write_csv(_single_row_frame(signal, SIGNAL_COLUMNS), paths.combined_signal)
    atomic_write_csv(_single_row_frame(signal, SIGNAL_COLUMNS), archive / "每日交易信号.csv")


def _prediction_message(record: Mapping[str, object]) -> str:
    if record.get("预测状态") != "已生成":
        main_line = "风险过滤后的前 10 名没有可用第一名。"
    else:
        main_line = (
            f"第一名：{html.escape(str(record['预测股票代码']))} "
            f"{html.escape(str(record['预测股票名称']))}，"
            f"行业：{html.escape(str(record.get('预测所属行业', '')))}，"
            f"得分：{html.escape(str(record['预测得分']))}"
        )
    return "<br>".join(
        [
            f"预测日期：{html.escape(str(record['预测日期']))}",
            main_line,
            f"未过滤前50数：{html.escape(str(record['未过滤前50数']))}",
        ]
    )


def _stock_pool_refresh_message(result: StockPoolRefreshResult) -> str:
    if result.source == "szse":
        return ""
    if result.source == "zhitu":
        message = (
            "<br>股票池：深交所接口本次不可用，已通过智图 API 逐只校验现有深市主板股票，"
            "并沿用已校验的行业映射。"
        )
        if result.unknown_industry_codes:
            message += (
                "行业映射缺失已标记为未知："
                f"{len(result.unknown_industry_codes)} 只。"
            )
        if result.failed_zhitu_codes:
            message += (
                "智图单股请求失败并保留上次已校验记录："
                f"{len(result.failed_zhitu_codes)} 只。"
            )
        message += f"本次智图请求：{result.zhitu_request_count} 次。"
        return message
    return (
        "<br>股票池：深交所接口本次不可用，已使用 "
        f"{result.source_day.isoformat()} 的已校验股票池；将在下次收盘任务自动重试。"
    )


def _return_failure_message(excluded_codes: set[str]) -> str:
    if not excluded_codes:
        return ""
    return f"<br>收益数据异常排除：{len(excluded_codes)} 只股票。"


def _realtime_message(record: Mapping[str, object]) -> str:
    status = str(record.get("实时状态", ""))
    observed_label = "统计时间" if status == FINAL_MAX_DECLINE_STATUS else "触发时间"
    lines = [
        f"监控日期：{html.escape(str(record.get('监控日期', '')))}",
        f"实时状态：{html.escape(status)}",
    ]
    if record.get("实时股票代码"):
        lines.extend(
            [
                f"标的：{html.escape(str(record.get('实时股票代码', '')))} "
                f"{html.escape(str(record.get('实时股票名称', '')))}",
                f"所属行业：{html.escape(str(record.get('实时所属行业', '')))}",
                f"跌幅：{html.escape(str(record.get('实时跌幅（%）', '')))}%",
                f"{observed_label}：{html.escape(str(record.get('实时触发时间', '')))}",
            ]
        )
    if record.get("备注"):
        lines.append(f"说明：{html.escape(str(record['备注']))}")
    return "<br>".join(lines)


def _send_notification_once(
    paths: PipelinePaths,
    state_day: date,
    notification_key: str,
    notifier: PushPlusNotifier,
    title: str,
    content: str,
    *,
    force: bool,
) -> None:
    state = load_json(paths.state_file(state_day))
    notifications = state.get("notifications")
    existing = notifications.get(notification_key) if isinstance(notifications, Mapping) else None
    if not force and isinstance(existing, Mapping) and existing.get("status") == "sent":
        return
    if not notifier.send(title, content):
        return
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("trading_day", state_day.isoformat())
    if not isinstance(state.get("notifications"), dict):
        state["notifications"] = {}
    state["notifications"][notification_key] = {
        "status": "sent",
        "sent_at": china_now().isoformat(),
        "title": title,
    }
    atomic_write_json(paths.state_file(state_day), state)


def _monitor_notification_title(record: Mapping[str, object], monitor_day: date) -> str:
    if str(record.get("实时状态", "")) == FINAL_MAX_DECLINE_STATUS:
        return f"深市主板09:45可用行情最大跌幅 {monitor_day.isoformat()}"
    return f"深市主板实时检测 {monitor_day.isoformat()}"


def _monitor_trigger_codes(paths: PipelinePaths, monitor_day: date) -> set[str]:
    state = load_json(paths.state_file(monitor_day))
    stages = state.get("stages")
    monitor_state = stages.get("monitor") if isinstance(stages, Mapping) else None
    raw_codes = (
        monitor_state.get("triggered_codes", [])
        if isinstance(monitor_state, Mapping)
        else []
    )
    codes = {
        str(code).strip()
        for code in raw_codes
        if len(str(code).strip()) == 6 and str(code).strip().isdigit()
    }
    notifications = state.get("notifications")
    if not isinstance(notifications, Mapping):
        return codes
    for key, notification in notifications.items():
        key_text = str(key)
        code = key_text.removeprefix(MONITOR_TRIGGER_NOTIFICATION_PREFIX)
        if (
            key_text.startswith(MONITOR_TRIGGER_NOTIFICATION_PREFIX)
            and len(code) == 6
            and code.isdigit()
            and isinstance(notification, Mapping)
            and notification.get("status") == "sent"
        ):
            codes.add(code)
    return codes


def _record_monitor_progress(
    paths: PipelinePaths,
    monitor_day: date,
    triggered_codes: set[str],
) -> None:
    update_state(
        paths,
        monitor_day,
        "monitor",
        {"status": "monitoring", "triggered_codes": sorted(triggered_codes)},
    )


def run_after_close(
    paths: PipelinePaths,
    *,
    target_day: date,
    no_push: bool,
    force: bool,
    logger: logging.Logger,
) -> int:
    archive = ensure_archive_dir(paths, target_day)
    notifier = PushPlusNotifier.from_env(paths.project_dir / ".env", logger, enabled=not no_push)
    if completed_stage(paths, target_day, "after_close") and not force:
        record = _load_current_signal(paths)
        if record.get("预测日期") == target_day.isoformat():
            _send_notification_once(
                paths,
                target_day,
                "prediction",
                notifier,
                f"深市主板每日预测 {target_day.isoformat()}",
                _prediction_message(record),
                force=False,
            )
        logger.info("After-close stage is already complete for %s.", target_day)
        return 0

    logger.info("Refreshing Shenzhen mainboard stock pool.")
    pool_refresh = refresh_stock_pool(paths, target_day=target_day, logger=logger)
    companies = screening.load_mainboard_companies(paths.stock_pool)
    atomic_copy(paths.stock_pool, archive / "深交所数据.xlsx")

    return_update = update_incremental_return_history(paths, companies, target_day, logger)
    if return_update.latest_market_day != target_day:
        update_state(
            paths,
            target_day,
            "after_close",
            {
                "status": "waiting_for_close_data",
                "reason": "当前日期没有确认的收盘行情，可能为非交易日或数据尚未更新。",
                "latest_market_day": return_update.latest_market_day.isoformat()
                if return_update.latest_market_day
                else "",
            },
        )
        raise PipelineError(
            f"{target_day.isoformat()} 尚未确认收盘行情；保留已有候选并让计划任务重试。"
        )

    missing_return_codes = _missing_required_return_codes(return_update.daily_returns, target_day)
    return_failure_codes = _return_failure_codes(return_update.failures)
    excluded_return_codes = return_failure_codes | missing_return_codes
    ranking_companies = _exclude_return_failure_companies(companies, excluded_return_codes)
    if excluded_return_codes:
        logger.warning(
            "Excluded %s stocks with unavailable return data from optimization and today's ranking: %s",
            len(excluded_return_codes),
            ", ".join(sorted(excluded_return_codes)),
        )

    previous_current_parameter = (
        paths.current_parameter_file.read_bytes()
        if paths.current_parameter_file.is_file()
        else None
    )
    optimizer_exit_code = optimizer.main(
        [
            "--returns-workbook",
            str(paths.returns_workbook),
            "--stock-pool",
            str(paths.stock_pool),
            "--output-dir",
            str(paths.optimizer_output_dir),
            "--as-of-date",
            target_day.isoformat(),
            "--lookback-days",
            "30",
        ]
    )
    if optimizer_exit_code != 0 or not paths.current_parameter_file.is_file():
        raise PipelineError("滚动参数优化未生成固定参数文件。")
    dated_parameter_file = (
        paths.optimizer_output_dir / f"{optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
    )
    try:
        logger.info("Collecting factors for %s mainboard companies.", len(ranking_companies))
        factors, factor_errors, factor_summary = screening.collect_factor_frame(
            ranking_companies,
            max_companies=len(ranking_companies),
            cache_hours=0.0,
            force_refresh=True,
            workers=DEFAULT_WORKERS,
            request_interval_seconds=DEFAULT_REQUEST_INTERVAL_SECONDS,
            as_of_date=target_day,
            progress_callback=_progress_logger(logger),
        )
        factors, factor_errors, excluded_factor_codes = _prepare_daily_factors(
            ranking_companies, factors, factor_errors, target_day
        )
        factor_summary = {
            **factor_summary,
            "有效因子": len(factors),
            "排除": len(excluded_factor_codes),
            "收益异常排除": len(excluded_return_codes),
        }
        if excluded_factor_codes:
            logger.warning(
                "Excluded %s stocks with unavailable daily factors from today's ranking: %s",
                len(excluded_factor_codes),
                ", ".join(excluded_factor_codes),
            )
        prediction = build_prediction(paths, ranking_companies, factors, target_day, logger)
        prediction = PredictionResult(
            prediction.record,
            prediction.top_fifty,
            prediction.top_ten,
            prediction.factors,
            factor_errors,
        )
    except Exception:
        if previous_current_parameter is None:
            paths.current_parameter_file.unlink(missing_ok=True)
        else:
            atomic_write_bytes(paths.current_parameter_file, previous_current_parameter)
        raise

    _write_prediction_artifacts(paths, target_day, prediction, factor_errors, return_update)
    if dated_parameter_file.is_file():
        atomic_copy(dated_parameter_file, archive / dated_parameter_file.name)
    atomic_copy(paths.current_parameter_file, archive / paths.current_parameter_file.name)
    update_state(
        paths,
        target_day,
        "after_close",
        {
            "status": "completed",
            "factor_summary": factor_summary,
            "excluded_factor_codes": excluded_factor_codes,
            "excluded_factor_count": len(excluded_factor_codes),
            "return_failure_codes": sorted(return_failure_codes),
            "missing_return_codes": sorted(missing_return_codes),
            "return_excluded_codes": sorted(excluded_return_codes),
            "return_excluded_count": len(excluded_return_codes),
            "stock_pool": {
                "status": "refreshed" if pool_refresh.refreshed else "fallback",
                "source": pool_refresh.source,
                "source_day": pool_refresh.source_day.isoformat(),
                "fallback_reason": pool_refresh.fallback_reason,
                "unknown_industry_codes": list(pool_refresh.unknown_industry_codes),
                "unknown_industry_count": len(pool_refresh.unknown_industry_codes),
                "failed_zhitu_codes": list(pool_refresh.failed_zhitu_codes),
                "failed_zhitu_count": len(pool_refresh.failed_zhitu_codes),
                "zhitu_request_count": pool_refresh.zhitu_request_count,
            },
            "return_rows_appended": return_update.appended_rows,
            "fixed_parameter_file": str(paths.current_parameter_file),
            "top_fifty_file": str(paths.top_fifty),
            "combined_signal_file": str(paths.combined_signal),
        },
    )
    _send_notification_once(
        paths,
        target_day,
        "prediction",
        notifier,
        f"深市主板每日预测 {target_day.isoformat()}",
        _prediction_message(prediction.record)
        + _stock_pool_refresh_message(pool_refresh)
        + _return_failure_message(excluded_return_codes),
        force=force,
    )
    logger.info("After-close workflow completed for %s.", target_day)
    return 0


def _load_return_update_for_recovery(paths: PipelinePaths, target_day: date) -> ReturnUpdate:
    daily_returns = _normalize_daily_returns(
        _read_excel_sheet(paths.returns_workbook, "每日涨跌幅明细")
    )
    latest_market_day = (
        _as_date(daily_returns["交易日期"].max()) if not daily_returns.empty else None
    )
    if latest_market_day != target_day:
        raise PipelineError(
            f"收益历史未覆盖 {target_day.isoformat()}，不能恢复当天预测。"
        )
    return ReturnUpdate(
        daily_returns,
        _read_excel_sheet(paths.returns_workbook, "失败明细"),
        latest_market_day,
        0,
    )


def run_prediction_recovery(
    paths: PipelinePaths,
    *,
    target_day: date,
    no_push: bool,
    force: bool,
    logger: logging.Logger,
) -> int:
    """Rebuild one failed prediction from its dated parameters and local factor cache."""

    archive = ensure_archive_dir(paths, target_day)
    notifier = PushPlusNotifier.from_env(paths.project_dir / ".env", logger, enabled=not no_push)
    if completed_stage(paths, target_day, "after_close") and not force:
        logger.info("After-close stage is already complete for %s.", target_day)
        return 0

    dated_parameter_file = (
        paths.optimizer_output_dir / f"{optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
    )
    parameter_payload = load_json(dated_parameter_file)
    if parameter_payload.get("run_date") != target_day.isoformat():
        raise PipelineError(
            f"缺少与 {target_day.isoformat()} 一致的日期参数文件：{dated_parameter_file}"
        )
    existing_signal = _load_current_signal(paths)
    existing_prediction_day = _as_date(existing_signal.get("预测日期"))
    if existing_prediction_day is not None and existing_prediction_day > target_day and not force:
        raise PipelineError("当前固定候选比待恢复日期更新，拒绝覆盖。")

    pool_path = archive / paths.stock_pool.name
    if not pool_path.is_file():
        pool_path = paths.stock_pool
    if not pool_path.is_file():
        raise PipelineError("缺少用于恢复预测的深市主板股票池。")
    companies = screening.load_mainboard_companies(pool_path)
    return_update = _load_return_update_for_recovery(paths, target_day)
    factors, factor_errors, factor_summary = read_cached_factor_frame(
        paths, companies, target_day
    )
    factors, factor_errors, excluded_factor_codes = _prepare_daily_factors(
        companies, factors, factor_errors, target_day
    )
    factor_summary = {
        **factor_summary,
        "有效因子": len(factors),
        "排除": len(excluded_factor_codes),
    }
    logger.warning(
        "Recovering %s from local factor cache only; excluded %s unavailable stocks.",
        target_day,
        len(excluded_factor_codes),
    )

    previous_current_parameter = (
        paths.current_parameter_file.read_bytes()
        if paths.current_parameter_file.is_file()
        else None
    )
    try:
        atomic_copy(dated_parameter_file, paths.current_parameter_file)
        prediction = build_prediction(paths, companies, factors, target_day, logger)
        prediction = PredictionResult(
            prediction.record,
            prediction.top_fifty,
            prediction.top_ten,
            prediction.factors,
            factor_errors,
        )
    except Exception:
        if previous_current_parameter is None:
            paths.current_parameter_file.unlink(missing_ok=True)
        else:
            atomic_write_bytes(paths.current_parameter_file, previous_current_parameter)
        raise

    _write_prediction_artifacts(paths, target_day, prediction, factor_errors, return_update)
    atomic_copy(dated_parameter_file, archive / dated_parameter_file.name)
    atomic_copy(paths.current_parameter_file, archive / paths.current_parameter_file.name)
    update_state(
        paths,
        target_day,
        "after_close",
        {
            "status": "completed",
            "recovered_from_factor_cache": True,
            "factor_summary": factor_summary,
            "excluded_factor_codes": excluded_factor_codes,
            "excluded_factor_count": len(excluded_factor_codes),
            "return_rows_appended": 0,
            "fixed_parameter_file": str(paths.current_parameter_file),
            "top_fifty_file": str(paths.top_fifty),
            "combined_signal_file": str(paths.combined_signal),
        },
    )
    _send_notification_once(
        paths,
        target_day,
        "prediction",
        notifier,
        f"深市主板每日预测 {target_day.isoformat()}",
        _prediction_message(prediction.record),
        force=force,
    )
    logger.info("Recovered after-close prediction for %s.", target_day)
    return 0


def _current_prediction_day(record: Mapping[str, object]) -> date:
    prediction_day = _as_date(record.get("预测日期"))
    if prediction_day is None:
        raise PipelineError("每日交易信号文件缺少有效的预测日期。")
    return prediction_day


def _quote_timestamp(value: str) -> datetime | None:
    try:
        timestamp = pd.to_datetime(value, errors="raise")
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return pd.Timestamp(timestamp).to_pydatetime()


def _final_snapshot_deadline(monitor_day: date) -> datetime:
    return datetime.combine(
        monitor_day, MONITOR_END_TIME, CHINA_TIMEZONE
    ) + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)


def _stale_quote_codes(
    snapshot: Mapping[str, intraday.Quote],
    monitor_day: date,
    *,
    observed_at: datetime | None = None,
    final_report: bool = False,
) -> list[str]:
    stale: list[str] = []
    observed_local = observed_at
    if observed_local is not None and observed_local.tzinfo is not None:
        observed_local = observed_local.astimezone(CHINA_TIMEZONE).replace(tzinfo=None)
    final_quote_deadline = _final_snapshot_deadline(monitor_day).replace(tzinfo=None)
    for code, quote in snapshot.items():
        timestamp = _quote_timestamp(quote.updated_at)
        if timestamp is not None and timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(CHINA_TIMEZONE).replace(tzinfo=None)
        quote_time = timestamp.time() if timestamp is not None else None
        earliest_quote_time = (
            FINAL_QUOTE_EARLIEST_TIME if final_report else MONITOR_START_TIME
        )
        if (
            timestamp is None
            or timestamp.date() != monitor_day
            or quote_time is None
            or quote_time < earliest_quote_time
            or (not final_report and quote_time >= MONITOR_END_TIME)
            or (final_report and timestamp > final_quote_deadline)
            or (
                observed_local is not None
                and (observed_local - timestamp).total_seconds() > MAX_QUOTE_AGE_SECONDS
            )
            or (
                final_report
                and observed_local is not None
                and timestamp
                > observed_local + timedelta(seconds=MAX_QUOTE_FUTURE_SKEW_SECONDS)
            )
        ):
            stale.append(code)
    return stale


def _has_current_day_quote(snapshot: Mapping[str, intraday.Quote], monitor_day: date) -> bool:
    return any(
        (timestamp := _quote_timestamp(quote.updated_at)) is not None
        and timestamp.date() == monitor_day
        for quote in snapshot.values()
    )


def _read_daily_quote_limit(paths: PipelinePaths) -> int | None:
    raw_value = (
        os.environ.get("zhituapi_daily_limit")
        or read_env_value(paths.project_dir / ".env", "zhituapi_daily_limit")
    )
    if raw_value is None:
        return DEFAULT_DAILY_QUOTE_LIMIT
    normalized = raw_value.strip().lower()
    if normalized in {"0", "none", "unlimited", "无限"}:
        return None
    try:
        limit = int(normalized)
    except ValueError as exc:
        raise PipelineError("zhituapi_daily_limit 必须是正整数或 unlimited。") from exc
    if limit <= 0:
        raise PipelineError("zhituapi_daily_limit 必须是正整数或 unlimited。")
    return limit


def _read_quote_rate_limit_per_minute(paths: PipelinePaths) -> int:
    raw_value = (
        os.environ.get("zhituapi_rate_limit_per_minute")
        or read_env_value(paths.project_dir / ".env", "zhituapi_rate_limit_per_minute")
    )
    if raw_value is None:
        return DEFAULT_QUOTE_RATE_LIMIT_PER_MINUTE
    try:
        limit = int(raw_value.strip())
    except ValueError as exc:
        raise PipelineError("zhituapi_rate_limit_per_minute 必须是正整数。") from exc
    if limit <= 0:
        raise PipelineError("zhituapi_rate_limit_per_minute 必须是正整数。")
    return limit


def _use_batch_quotes(paths: PipelinePaths) -> bool:
    raw_value = (
        os.environ.get("zhituapi_batch_quotes")
        or read_env_value(paths.project_dir / ".env", "zhituapi_batch_quotes")
        or "false"
    )
    return raw_value.strip().lower() in {"1", "true", "yes", "batch"}


def _validate_monitor_request_budget(
    paths: PipelinePaths,
    candidate_count: int,
    interval_seconds: int,
    *,
    use_batch: bool,
) -> int:
    monitor_window_seconds = (
        (MONITOR_END_TIME.hour * 60 + MONITOR_END_TIME.minute) * 60
        - (MONITOR_START_TIME.hour * 60 + MONITOR_START_TIME.minute) * 60
    )
    # One extra complete snapshot is required for the fixed 09:45 report.
    snapshots = (monitor_window_seconds + interval_seconds - 1) // interval_seconds + 1
    requests_per_snapshot = (
        (candidate_count + intraday.MAX_BATCH_CODES - 1) // intraday.MAX_BATCH_CODES
        if use_batch
        else candidate_count
    )
    required_requests = snapshots * requests_per_snapshot
    requests_per_minute = ((60 + interval_seconds - 1) // interval_seconds) * requests_per_snapshot
    rate_limit = _read_quote_rate_limit_per_minute(paths)
    if requests_per_minute > rate_limit:
        raise PipelineError(
            f"当前监控计划峰值约 {requests_per_minute} 次/分钟，但 "
            f"zhituapi_rate_limit_per_minute={rate_limit}。"
        )
    daily_limit = _read_daily_quote_limit(paths)
    if daily_limit is not None and required_requests > daily_limit:
        raise PipelineError(
            f"当前监控计划需要约 {required_requests} 次行情请求，但 zhituapi_daily_limit="
            f"{daily_limit}；请提高额度或拉长轮询间隔。"
        )
    return required_requests


def _intervening_market_sessions(
    paths: PipelinePaths,
    prediction_day: date,
    monitor_day: date,
    logger: logging.Logger,
) -> list[date]:
    """Confirm whether a pending signal already missed an actual session.

    This probe is needed only after the ordinary next weekday.  It lets a
    pending Friday signal survive a Monday holiday, while refusing to reuse it
    on Tuesday when Monday was an open market session and the PC was offline.
    """

    first_day = prediction_day + timedelta(days=1)
    last_day = monitor_day - timedelta(days=1)
    if monitor_day <= next_weekday(prediction_day) or first_day > last_day:
        return []

    try:
        token = intraday.get_token(paths.project_dir / ".env")
        response = _direct_http_session().get(
            MARKET_SESSION_API_URL.format(code=MARKET_SESSION_REFERENCE_CODE),
            params={
                "token": token,
                "st": first_day.strftime("%Y%m%d"),
                "et": last_day.strftime("%Y%m%d"),
            },
            timeout=MARKET_SESSION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PipelineError("无法确认候选期间是否已有交易日，拒绝使用旧候选监控。") from exc

    if not isinstance(payload, list):
        raise PipelineError("交易日确认接口返回格式异常，拒绝使用旧候选监控。")

    sessions: set[date] = set()
    malformed_rows = 0
    for item in payload:
        if not isinstance(item, Mapping):
            malformed_rows += 1
            continue
        timestamp = _quote_timestamp(str(item.get("time", "")))
        if timestamp is None:
            malformed_rows += 1
            continue
        session_day = timestamp.date()
        if first_day <= session_day <= last_day:
            sessions.add(session_day)
    if payload and malformed_rows == len(payload):
        raise PipelineError("交易日确认接口缺少有效时间，拒绝使用旧候选监控。")

    result = sorted(sessions)
    logger.info(
        "Checked pending candidate sessions from %s through %s: %s.",
        first_day,
        last_day,
        ", ".join(day.isoformat() for day in result) or "none",
    )
    return result


def _realtime_record(
    prediction: Mapping[str, object],
    monitor_day: date,
    *,
    status: str,
    note: str,
    trigger: intraday.Trigger | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    record = dict(prediction)
    record.update(
        {
            "监控日期": monitor_day.isoformat(),
            "实时状态": status,
            "实时股票代码": "",
            "实时股票名称": "",
            "实时所属行业": "",
            "实时候选排名": "",
            "实时最新价": "",
            "实时昨收价": "",
            "实时跌幅（%）": "",
            "实时触发时间": "",
            "行情更新时间": "",
            "更新时间": china_now().isoformat(),
            "备注": note,
        }
    )
    if trigger is not None:
        quote = trigger.quote
        record.update(
            {
                "实时股票代码": trigger.candidate.code,
                "实时股票名称": trigger.candidate.name,
                "实时所属行业": trigger.candidate.industry,
                "实时候选排名": trigger.candidate.rank,
                "实时最新价": str(quote.last_price),
                "实时昨收价": str(quote.previous_close),
                "实时跌幅（%）": f"{quote.change_percent:.4f}",
                "实时触发时间": (observed_at or china_now()).strftime("%Y-%m-%d %H:%M:%S"),
                "行情更新时间": quote.updated_at,
            }
        )
    return record


def _write_realtime_artifacts(
    paths: PipelinePaths,
    prediction_day: date,
    monitor_day: date,
    record: Mapping[str, object],
) -> None:
    realtime = _single_row_frame(record, REALTIME_COLUMNS)
    combined = _single_row_frame(record, SIGNAL_COLUMNS)
    # The fixed signal is the recovery source for an interrupted archive copy.
    atomic_write_csv(combined, paths.combined_signal)
    atomic_write_csv(realtime, paths.realtime_current)
    archive_days = (monitor_day,) if prediction_day == monitor_day else (monitor_day, prediction_day)
    for archive_day in archive_days:
        archive = ensure_archive_dir(paths, archive_day)
        atomic_write_csv(realtime, archive / "每日实时检测结果.csv")
        atomic_write_csv(combined, archive / "每日交易信号.csv")


FINAL_MONITOR_STATUSES = frozenset(
    {"已触发", "无信号", "监控窗口错过", "行情异常", FINAL_MAX_DECLINE_STATUS}
)


def _is_final_monitor_record(record: Mapping[str, object], monitor_day: date) -> bool:
    return (
        _as_date(record.get("监控日期")) == monitor_day
        and str(record.get("实时状态", "")) in FINAL_MONITOR_STATUSES
    )


def _load_archived_final_monitor_record(
    paths: PipelinePaths, monitor_day: date
) -> dict[str, object] | None:
    record = _load_signal_record(paths.archive_for(monitor_day) / "每日交易信号.csv")
    return record if _is_final_monitor_record(record, monitor_day) else None


def _commit_final_monitor_record(
    paths: PipelinePaths,
    prediction_day: date,
    monitor_day: date,
    record: Mapping[str, object],
) -> None:
    # Journal before copying artifacts so a restart cannot select a second stock.
    update_state(paths, monitor_day, "monitor", {"status": "committing", "result": dict(record)})
    _write_realtime_artifacts(paths, prediction_day, monitor_day, record)
    update_state(paths, monitor_day, "monitor", {"status": "completed", "result": dict(record)})


def _recover_pending_monitor_commit(
    paths: PipelinePaths, monitor_day: date, logger: logging.Logger
) -> dict[str, object] | None:
    state = state_section(paths, monitor_day, "monitor")
    if state.get("status") != "committing" or not isinstance(state.get("result"), Mapping):
        return None
    record = dict(state["result"])
    if not _is_final_monitor_record(record, monitor_day):
        raise PipelineError("实时检测恢复记录无效，拒绝继续监控。")
    prediction_day = _current_prediction_day(record)
    if prediction_day >= monitor_day:
        raise PipelineError("实时检测恢复记录的预测日期无效。")
    _write_realtime_artifacts(paths, prediction_day, monitor_day, record)
    update_state(paths, monitor_day, "monitor", {"status": "completed", "result": record})
    logger.warning("Recovered an interrupted realtime artifact commit for %s.", monitor_day)
    return record


def run_monitor(
    paths: PipelinePaths,
    *,
    monitor_day: date,
    no_push: bool,
    force: bool,
    interval_seconds: int,
    logger: logging.Logger,
) -> int:
    notifier = PushPlusNotifier.from_env(paths.project_dir / ".env", logger, enabled=not no_push)
    recovered_commit = _recover_pending_monitor_commit(paths, monitor_day, logger)
    if recovered_commit is not None:
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(recovered_commit, monitor_day),
            _realtime_message(recovered_commit),
            force=False,
        )
        return 0
    archived_final = _load_archived_final_monitor_record(paths, monitor_day)
    if archived_final is not None and not force:
        archived_prediction_day = _current_prediction_day(archived_final)
        if archived_prediction_day >= monitor_day:
            raise PipelineError("归档实时检测记录的预测日期无效。")
        _write_realtime_artifacts(paths, archived_prediction_day, monitor_day, archived_final)
        update_state(
            paths,
            monitor_day,
            "monitor",
            {"status": "completed", "result": archived_final, "recovered_from_archive": True},
        )
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(archived_final, monitor_day),
            _realtime_message(archived_final),
            force=False,
        )
        logger.warning("Recovered the fixed realtime signal from its monitor-day archive.")
        return 0
    prediction = _load_current_signal(paths)
    prediction_day = _current_prediction_day(prediction)
    if prediction_day >= monitor_day:
        raise PipelineError("实时监控日期必须晚于候选预测日期。")
    after_close_state = state_section(paths, prediction_day, "after_close")
    if after_close_state.get("status") != "completed":
        raise PipelineError("候选预测没有完成的收盘任务记录，拒绝使用固定旧文件监控。")
    existing_status = str(prediction.get("实时状态", ""))
    if existing_status in FINAL_MONITOR_STATUSES and not force:
        record_monitor_day = _as_date(prediction.get("监控日期"))
        if record_monitor_day != monitor_day:
            raise PipelineError("候选预测已经在其他日期结束监控，拒绝重复使用。")
        _write_realtime_artifacts(paths, prediction_day, monitor_day, prediction)
        if not completed_stage(paths, monitor_day, "monitor"):
            update_state(
                paths,
                monitor_day,
                "monitor",
                {
                    "status": "completed",
                    "result": prediction,
                    "recovered_from_final_signal": True,
                },
            )
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(prediction, monitor_day),
            _realtime_message(prediction),
            force=False,
        )
        logger.info("Realtime stage is already final: %s", existing_status)
        return 0
    candidate_path = paths.archive_for(prediction_day) / "前 50 名（含所属行业）.csv"
    if not candidate_path.is_file():
        raise PipelineError(f"缺少与预测日期一致的实时监控候选文件：{candidate_path}")
    candidates = intraday.read_candidates(candidate_path)
    if not candidates:
        raise PipelineError("实时监控候选文件为空。")

    now = china_now()
    current_time = now.timetz().replace(tzinfo=None)
    final_snapshot_deadline = _final_snapshot_deadline(monitor_day)
    if now > final_snapshot_deadline:
        record = _realtime_record(
            prediction,
            monitor_day,
            status="监控窗口错过",
            note="程序启动时已超过09:45行情可确认时限，未伪造盘中触发结果。",
        )
        _commit_final_monitor_record(paths, prediction_day, monitor_day, record)
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(record, monitor_day),
            _realtime_message(record),
            force=force,
        )
        return 0

    intervening_sessions = _intervening_market_sessions(
        paths, prediction_day, monitor_day, logger
    )
    if intervening_sessions:
        missed_days = "、".join(day.isoformat() for day in intervening_sessions)
        record = _realtime_record(
            prediction,
            monitor_day,
            status="监控窗口错过",
            note=f"预测后的实际交易日 {missed_days} 已错过，未使用旧候选继续监控。",
        )
        _commit_final_monitor_record(paths, prediction_day, monitor_day, record)
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(record, monitor_day),
            _realtime_message(record),
            force=force,
        )
        logger.warning("Expired stale candidate from %s; missed sessions: %s", prediction_day, missed_days)
        return 0

    while current_time < MONITOR_START_TIME:
        seconds_until_start = (
            datetime.combine(now.date(), MONITOR_START_TIME, CHINA_TIMEZONE) - now
        ).total_seconds()
        logger.info("Waiting %.0f seconds for the monitoring window.", seconds_until_start)
        time_module.sleep(min(60.0, max(1.0, seconds_until_start)))
        now = china_now()
        current_time = now.timetz().replace(tzinfo=None)

    use_batch = _use_batch_quotes(paths)
    required_requests = _validate_monitor_request_budget(
        paths,
        len(candidates),
        interval_seconds,
        use_batch=use_batch,
    )
    logger.info(
        "Monitoring %s candidates every %s seconds with %s requests planned (%s mode).",
        len(candidates),
        interval_seconds,
        required_requests,
        "batch" if use_batch else "single-stock",
    )
    client = intraday.ZhituApiClient(intraday.get_token(paths.project_dir / ".env"))
    successful_snapshots = 0
    current_day_quote_seen = False
    final_snapshot: dict[str, intraday.Quote] | None = None
    final_snapshot_observed_at: datetime | None = None
    triggered_codes = _monitor_trigger_codes(paths, monitor_day)
    sent_trigger_codes: set[str] = set()

    def send_trigger_notifications(
        triggers: Iterable[intraday.Trigger], observed_at: datetime
    ) -> None:
        nonlocal triggered_codes
        trigger_list = list(triggers)
        new_codes = {trigger.candidate.code for trigger in trigger_list} - triggered_codes
        if new_codes:
            triggered_codes.update(new_codes)
            _record_monitor_progress(paths, monitor_day, triggered_codes)
        for trigger in trigger_list:
            code = trigger.candidate.code
            if code in sent_trigger_codes:
                continue
            record = _realtime_record(
                prediction,
                monitor_day,
                status="已触发",
                note="仅提示人工判断；程序未发送任何交易委托。",
                trigger=trigger,
                observed_at=observed_at,
            )
            _send_notification_once(
                paths,
                monitor_day,
                f"{MONITOR_TRIGGER_NOTIFICATION_PREFIX}{code}",
                notifier,
                f"深市主板实时触发 {monitor_day.isoformat()}",
                _realtime_message(record),
                force=force,
            )
            sent_trigger_codes.add(code)
            logger.warning("Realtime trigger: %s", record)

    def validate_snapshot(
        snapshot: dict[str, intraday.Quote] | None,
        observed_at: datetime,
        *,
        final_report: bool,
        allow_partial: bool = False,
    ) -> dict[str, intraday.Quote] | None:
        nonlocal successful_snapshots, current_day_quote_seen
        if snapshot is None:
            return None
        if final_report and observed_at > final_snapshot_deadline:
            logger.warning(
                "Discarded 09:45 snapshot completed after the final deadline: %s",
                observed_at,
            )
            return None
        successful_snapshots += 1
        current_day_quote_seen = current_day_quote_seen or _has_current_day_quote(
            snapshot, monitor_day
        )
        stale_codes = _stale_quote_codes(
            snapshot,
            monitor_day,
            observed_at=observed_at,
            final_report=final_report,
        )
        label = "09:45" if final_report else "monitoring"
        if stale_codes:
            logger.warning(
                "Discarded stale %s quote snapshot for %s codes; examples: %s",
                label,
                len(stale_codes),
                ", ".join(stale_codes[:5]),
            )
            if not allow_partial:
                return None
            stale_code_set = set(stale_codes)
            snapshot = {
                code: quote for code, quote in snapshot.items() if code not in stale_code_set
            }
        available_candidates = [
            candidate for candidate in candidates if candidate.code in snapshot
        ]
        if not available_candidates:
            logger.warning("No usable %s quotes remain after validation.", label)
            return None
        send_trigger_notifications(
            intraday.select_triggers(available_candidates, snapshot), observed_at
        )
        return snapshot

    while True:
        now = china_now()
        if now.timetz().replace(tzinfo=None) >= MONITOR_END_TIME:
            break
        round_started = time_module.monotonic()
        snapshot = intraday.collect_complete_snapshot(
            candidates,
            client,
            logger,
            use_batch=use_batch,
        )
        snapshot_observed_at = china_now()
        snapshot_reached_final_minute = (
            snapshot_observed_at.timetz().replace(tzinfo=None) >= MONITOR_END_TIME
        )
        valid_snapshot = validate_snapshot(
            snapshot,
            snapshot_observed_at,
            final_report=snapshot_reached_final_minute,
        )
        if snapshot_reached_final_minute:
            final_snapshot = valid_snapshot
            final_snapshot_observed_at = snapshot_observed_at if valid_snapshot is not None else None
            break
        delay = interval_seconds - (time_module.monotonic() - round_started)
        if delay > 0:
            time_module.sleep(delay)

    if final_snapshot is None:
        snapshot = intraday.collect_available_snapshot(
            candidates,
            client,
            logger,
            use_batch=use_batch,
        )
        snapshot_observed_at = china_now()
        final_snapshot = validate_snapshot(
            snapshot,
            snapshot_observed_at,
            final_report=True,
            allow_partial=True,
        )
        if final_snapshot is not None:
            final_snapshot_observed_at = snapshot_observed_at

    if final_snapshot is not None and final_snapshot_observed_at is not None:
        largest_decline = intraday.select_largest_decline(candidates, final_snapshot)
        if largest_decline is None:
            raise PipelineError("09:45可用行情为空，无法确定最大跌幅股票。")
        ignored_quote_count = len(candidates) - len(final_snapshot)
        coverage_note = (
            f"本次可用行情 {len(final_snapshot)}/{len(candidates)}，已忽略 "
            f"{ignored_quote_count} 只失败或过期股票；"
            if ignored_quote_count
            else ""
        )
        if triggered_codes:
            note = (
                f"09:45定时推送：本时段已有 {len(triggered_codes)} 只候选股触发条件；"
                f"{coverage_note}此为09:45可用行情中跌幅最大股票。"
            )
        else:
            note = f"本时段无触发股票；{coverage_note}按规则推送09:45可用行情中跌幅最大股票。"
        record = _realtime_record(
            prediction,
            monitor_day,
            status=FINAL_MAX_DECLINE_STATUS,
            note=note,
            trigger=largest_decline,
            observed_at=final_snapshot_observed_at,
        )
        _commit_final_monitor_record(paths, prediction_day, monitor_day, record)
        _send_notification_once(
            paths,
            monitor_day,
            MONITOR_FINAL_NOTIFICATION_KEY,
            notifier,
            _monitor_notification_title(record, monitor_day),
            _realtime_message(record),
            force=force,
        )
        logger.info("Realtime monitoring completed with the 09:45 maximum-decline report.")
        return 0

    if successful_snapshots and not current_day_quote_seen:
        note = "所有完整行情快照都不是当日 09:28 后数据，疑似休市；保留待监控信号。"
        update_state(
            paths,
            monitor_day,
            "monitor",
            {
                "status": "skipped",
                "reason": note,
                "prediction_day": prediction_day.isoformat(),
            },
        )
        logger.info("Realtime monitor skipped for %s: %s", monitor_day, note)
        return 0
    status = "行情异常"
    note = "未取得可确认的当日09:45附近可用行情，未推送最大跌幅。"
    record = _realtime_record(prediction, monitor_day, status=status, note=note)
    _commit_final_monitor_record(paths, prediction_day, monitor_day, record)
    _send_notification_once(
        paths,
        monitor_day,
        MONITOR_FINAL_NOTIFICATION_KEY,
        notifier,
        _monitor_notification_title(record, monitor_day),
        _realtime_message(record),
        force=force,
    )
    logger.info("Realtime monitoring completed: %s", status)
    return 0


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD 格式。") from exc


def default_project_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_DIR


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("after-close", "recover-prediction", "monitor"),
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="包含 .env、历史数据和策略输出的项目目录。",
    )
    parser.add_argument("--as-of-date", type=parse_date, help="仅用于补跑指定交易日。")
    parser.add_argument("--force", action="store_true", help="忽略已完成标记，重新执行该阶段。")
    parser.add_argument("--no-push", action="store_true", help="不发送 PushPlus，仅执行本地流程。")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_MONITOR_INTERVAL_SECONDS,
        help="完整候选池行情轮询间隔，最少60秒。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval_seconds < MINIMUM_MONITOR_INTERVAL_SECONDS:
        raise ValueError("interval-seconds 必须至少为 60；智兔实时行情按分钟更新。")
    project_dir = (args.project_dir or default_project_dir()).resolve()
    if not project_dir.is_dir():
        raise ValueError(f"项目目录不存在：{project_dir}")
    paths = PipelinePaths(project_dir)
    now = china_now()
    logger = configure_logger(paths, now)
    lock_path = paths.output_dir / "daily_trading_runner.lock"
    try:
        with ProcessLock(lock_path):
            if args.mode == "after-close":
                target_day = resolve_after_close_day(now, args.as_of_date)
                return run_after_close(
                    paths,
                    target_day=target_day,
                    no_push=bool(args.no_push),
                    force=bool(args.force),
                    logger=logger,
                )
            if args.mode == "recover-prediction":
                target_day = resolve_after_close_day(now, args.as_of_date)
                return run_prediction_recovery(
                    paths,
                    target_day=target_day,
                    no_push=bool(args.no_push),
                    force=bool(args.force),
                    logger=logger,
                )
            monitor_day = args.as_of_date or now.date()
            return run_monitor(
                paths,
                monitor_day=monitor_day,
                no_push=bool(args.no_push),
                force=bool(args.force),
                interval_seconds=int(args.interval_seconds),
                logger=logger,
            )
    except Exception:
        logger.exception("Daily trading runner failed.")
        raise
    finally:
        close_logger(logger)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PipelineError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
