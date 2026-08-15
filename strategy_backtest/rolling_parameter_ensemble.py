"""Fuse independently optimized rolling parameter windows without future data."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd

from . import backtest_core as core
from . import rolling_parameter_optimizer as optimizer


ENSEMBLE_WINDOWS = (14, 30, 45)
ROLLING_RETURN_COUNT = 5
WEIGHT_TOLERANCE = 1e-9
WINDOW_COVERAGE_FACTORS = {14: 0.65, 30: 0.85, 45: 1.20}
RANGE_GUARDRAILS = {
    "szse_quant_filter_pct_change_range": (-5.5, 10.5),
    "szse_quant_filter_rsi_range": (20, 120),
    "szse_quant_filter_turnover_range": (2.5, 10.5),
    "szse_quant_filter_volume_ratio_range": (0.8, 7.0),
}
OUT_OF_RANGE_WEIGHT_MULTIPLIER = 0.1
ENSEMBLE_DIRECTORY_NAME = "rolling_parameter_ensemble"
WINDOW_SNAPSHOT_DIRECTORY_NAME = "window_snapshots"
WINDOW_RUN_DIRECTORY_NAME = "window_runs"
WINDOW_REPORT_DIRECTORY_NAME = "window_reports"
RETURN_HISTORY_FILENAME = "return_history.json"
ENSEMBLE_REPORT_PREFIX = "rolling_parameter_ensemble_"
ENSEMBLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RollingScore:
    """The five-return score, coverage adjustment and linear allocation."""

    lookback_days: int
    returns: tuple[float, ...]
    mean: float
    sample_stddev: float
    raw_sharpe: float
    relu_sharpe: float
    coverage_factor: float
    coverage_adjusted_score: float
    out_of_range_setting_keys: tuple[str, ...]
    range_penalty_multiplier: float
    penalized_score: float
    weight: float


@dataclass(frozen=True)
class WindowSnapshot:
    """The immutable optimizer result for one window and one verification day."""

    as_of_date: date
    verification_date: date
    lookback_days: int
    best_settings: dict[str, tuple[float | int, float | int]]
    total_return_pct: float
    source_report: str
    data_window: dict[str, object]
    input_fingerprint: str = ""


@dataclass(frozen=True)
class EnsemblePublication:
    """Whether a complete five-day ensemble was safely published."""

    published: bool
    reason: str | None = None
    parameter_path: Path | None = None
    report_path: Path | None = None


def _required_window_values(
    returns_by_window: Mapping[int, Sequence[float]],
) -> dict[int, tuple[float, ...]]:
    supplied_windows = {int(window) for window in returns_by_window}
    expected_windows = set(ENSEMBLE_WINDOWS)
    if supplied_windows != expected_windows:
        raise ValueError("收益序列必须且只能包含14、30、45日窗口。")

    normalized: dict[int, tuple[float, ...]] = {}
    for window in ENSEMBLE_WINDOWS:
        values = tuple(float(value) for value in returns_by_window[window])
        if len(values) != ROLLING_RETURN_COUNT:
            raise ValueError("每个窗口必须提供恰好5个连续交易日收益率。")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("滚动收益率必须全部是有限数值。")
        normalized[window] = values
    return normalized


def _out_of_range_setting_keys(settings: Mapping[str, object]) -> tuple[str, ...]:
    normalized = optimizer.normalize_tunable_settings(settings)
    return tuple(
        setting_key
        for setting_key, (minimum, maximum) in RANGE_GUARDRAILS.items()
        if float(normalized[setting_key][0]) < minimum
        or float(normalized[setting_key][1]) > maximum
    )


def _range_violations_by_window(
    settings_by_window: Mapping[int, Mapping[str, object]] | None,
) -> dict[int, tuple[str, ...]]:
    if settings_by_window is None:
        return {window: () for window in ENSEMBLE_WINDOWS}
    supplied_windows = {int(window) for window in settings_by_window}
    if supplied_windows != set(ENSEMBLE_WINDOWS):
        raise ValueError("参数必须且只能包含14、30、45日窗口。")
    return {
        window: _out_of_range_setting_keys(settings_by_window[window])
        for window in ENSEMBLE_WINDOWS
    }


def calculate_rolling_weights(
    returns_by_window: Mapping[int, Sequence[float]],
    settings_by_window: Mapping[int, Mapping[str, object]] | None = None,
) -> dict[int, RollingScore]:
    """Calculate coverage-adjusted ReLU Sharpe scores with linear normalization.

    The inputs are raw total-return values for exactly five completed trading
    days. ReLU-truncated Sharpe scores are multiplied by the fixed coverage
    factor for their window. When settings are supplied, a component whose
    guarded parameter range exceeds its allowed interval receives a further
    0.1 multiplier before normalization. This intentionally does not use
    exponential Softmax.
    """

    values_by_window = _required_window_values(returns_by_window)
    range_violations = _range_violations_by_window(settings_by_window)
    statistics: dict[
        int, tuple[tuple[float, ...], float, float, float, float, float, float]
    ] = {}
    for window, values in values_by_window.items():
        series = np.asarray(values, dtype=float)
        mean = float(np.mean(series))
        sample_stddev = float(np.std(series, ddof=1))
        raw_sharpe = 0.0 if sample_stddev == 0.0 else mean / sample_stddev
        relu_sharpe = max(raw_sharpe, 0.0)
        coverage_factor = WINDOW_COVERAGE_FACTORS[window]
        coverage_adjusted_score = relu_sharpe * coverage_factor
        statistics[window] = (
            values,
            mean,
            sample_stddev,
            raw_sharpe,
            relu_sharpe,
            coverage_factor,
            coverage_adjusted_score,
        )

    range_penalty_multipliers = {
        window: (
            OUT_OF_RANGE_WEIGHT_MULTIPLIER
            if range_violations[window]
            else 1.0
        )
        for window in ENSEMBLE_WINDOWS
    }
    penalized_scores = {
        window: statistics[window][-1] * range_penalty_multipliers[window]
        for window in ENSEMBLE_WINDOWS
    }
    total_score = sum(penalized_scores.values())
    if total_score > 0.0:
        weights = {
            window: penalized_scores[window] / total_score
            for window in ENSEMBLE_WINDOWS
        }
    else:
        equal_weight = 1.0 / len(ENSEMBLE_WINDOWS)
        weights = {window: equal_weight for window in ENSEMBLE_WINDOWS}

    return {
        window: RollingScore(
            lookback_days=window,
            returns=values,
            mean=mean,
            sample_stddev=sample_stddev,
            raw_sharpe=raw_sharpe,
            relu_sharpe=relu_sharpe,
            coverage_factor=coverage_factor,
            coverage_adjusted_score=coverage_adjusted_score,
            out_of_range_setting_keys=range_violations[window],
            range_penalty_multiplier=range_penalty_multipliers[window],
            penalized_score=penalized_scores[window],
            weight=weights[window],
        )
        for window, (
            values,
            mean,
            sample_stddev,
            raw_sharpe,
            relu_sharpe,
            coverage_factor,
            coverage_adjusted_score,
        ) in statistics.items()
    }


def _normalized_weights(weights: Mapping[int, float]) -> dict[int, float]:
    supplied_windows = {int(window) for window in weights}
    if supplied_windows != set(ENSEMBLE_WINDOWS):
        raise ValueError("权重必须且只能包含14、30、45日窗口。")
    normalized = {window: float(weights[window]) for window in ENSEMBLE_WINDOWS}
    if not all(math.isfinite(weight) and weight >= 0.0 for weight in normalized.values()):
        raise ValueError("权重必须是非负有限数值。")
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=WEIGHT_TOLERANCE):
        raise ValueError("权重之和必须为1。")
    return normalized


def blend_tunable_settings(
    settings_by_window: Mapping[int, Mapping[str, object]],
    weights: Mapping[int, float],
) -> dict[str, tuple[float | int, float | int]]:
    """Linearly blend tunable ranges and round integer bounds outward."""

    supplied_windows = {int(window) for window in settings_by_window}
    if supplied_windows != set(ENSEMBLE_WINDOWS):
        raise ValueError("参数必须且只能包含14、30、45日窗口。")
    normalized_weights = _normalized_weights(weights)
    normalized_settings = {
        window: optimizer.normalize_tunable_settings(settings_by_window[window])
        for window in ENSEMBLE_WINDOWS
    }

    blended: dict[str, tuple[float | int, float | int]] = {}
    for spec in optimizer.PARAMETER_SPECS:
        lower = sum(
            normalized_weights[window]
            * float(normalized_settings[window][spec.setting_key][0])
            for window in ENSEMBLE_WINDOWS
        )
        upper = sum(
            normalized_weights[window]
            * float(normalized_settings[window][spec.setting_key][1])
            for window in ENSEMBLE_WINDOWS
        )
        lower = max(lower, spec.minimum)
        upper = min(upper, spec.maximum)
        if spec.integer:
            blended[spec.setting_key] = (math.floor(lower), math.ceil(upper))
        else:
            blended[spec.setting_key] = (float(lower), float(upper))

    return optimizer.normalize_tunable_settings(blended)


def _as_date(value: date | datetime | str) -> date:
    return optimizer._as_date(value)


def _ensemble_directory(output_dir: Path) -> Path:
    return Path(output_dir) / ENSEMBLE_DIRECTORY_NAME


def window_snapshot_path(
    output_dir: Path,
    as_of_date: date | datetime | str,
    lookback_days: int,
    *,
    input_fingerprint: str = "",
) -> Path:
    snapshot_day = _as_date(as_of_date)
    suffix = _fingerprint_suffix(input_fingerprint)
    return (
        _ensemble_directory(Path(output_dir))
        / WINDOW_SNAPSHOT_DIRECTORY_NAME
        / snapshot_day.isoformat()
        / f"lookback_{int(lookback_days):02d}{suffix}.json"
    )


def _window_run_directory(output_dir: Path, lookback_days: int) -> Path:
    return (
        _ensemble_directory(Path(output_dir))
        / WINDOW_RUN_DIRECTORY_NAME
        / f"lookback_{int(lookback_days):02d}"
    )


def _fingerprint_suffix(input_fingerprint: str) -> str:
    if not input_fingerprint:
        return ""
    return "_" + hashlib.sha256(input_fingerprint.encode("utf-8")).hexdigest()[:16]


def _window_report_path(
    output_dir: Path,
    as_of_date: date | datetime | str,
    lookback_days: int,
    *,
    input_fingerprint: str,
) -> Path:
    snapshot_day = _as_date(as_of_date)
    return (
        _ensemble_directory(Path(output_dir))
        / WINDOW_REPORT_DIRECTORY_NAME
        / snapshot_day.isoformat()
        / f"lookback_{int(lookback_days):02d}{_fingerprint_suffix(input_fingerprint)}.json"
    )


def _return_history_path(output_dir: Path) -> Path:
    return _ensemble_directory(Path(output_dir)) / RETURN_HISTORY_FILENAME


def _json_safe(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary_path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _write_bytes_atomically(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary_path.write_bytes(value)
    temporary_path.replace(path)


def _read_json_mapping(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取JSON文件：{path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON文件必须是对象：{path}")
    return dict(payload)


def _snapshot_payload(snapshot: WindowSnapshot) -> dict[str, object]:
    return {
        "schema_version": ENSEMBLE_SCHEMA_VERSION,
        "as_of_date": snapshot.as_of_date.isoformat(),
        "verification_date": snapshot.verification_date.isoformat(),
        "lookback_days": snapshot.lookback_days,
        "best_settings": snapshot.best_settings,
        "total_return_pct": snapshot.total_return_pct,
        "source_report": snapshot.source_report,
        "data_window": snapshot.data_window,
        "input_fingerprint": snapshot.input_fingerprint,
    }


def _snapshot_from_payload(payload: Mapping[str, object]) -> WindowSnapshot:
    try:
        lookback_days = int(payload["lookback_days"])
        as_of_date = _as_date(str(payload["as_of_date"]))
        verification_date = _as_date(str(payload["verification_date"]))
        total_return_pct = float(payload["total_return_pct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("窗口快照缺少有效的日期、窗口或收益率。") from exc
    if lookback_days not in ENSEMBLE_WINDOWS:
        raise ValueError("窗口快照包含不支持的回看天数。")
    if not math.isfinite(total_return_pct):
        raise ValueError("窗口快照收益率必须是有限数值。")
    raw_settings = payload.get("best_settings")
    if not isinstance(raw_settings, Mapping):
        raise ValueError("窗口快照缺少best_settings。")
    raw_data_window = payload.get("data_window", {})
    if not isinstance(raw_data_window, Mapping):
        raise ValueError("窗口快照data_window必须是对象。")
    source_report = payload.get("source_report")
    if not isinstance(source_report, str) or not source_report:
        raise ValueError("窗口快照缺少source_report。")
    input_fingerprint = payload.get("input_fingerprint", "")
    if not isinstance(input_fingerprint, str):
        raise ValueError("窗口快照input_fingerprint必须是文本。")
    return WindowSnapshot(
        as_of_date=as_of_date,
        verification_date=verification_date,
        lookback_days=lookback_days,
        best_settings=optimizer.normalize_tunable_settings(raw_settings),
        total_return_pct=total_return_pct,
        source_report=source_report,
        data_window=dict(raw_data_window),
        input_fingerprint=input_fingerprint,
    )


def save_window_snapshot(output_dir: Path, snapshot: WindowSnapshot) -> Path:
    """Persist one window result independently from the canonical parameter file."""

    path = window_snapshot_path(
        output_dir,
        snapshot.as_of_date,
        snapshot.lookback_days,
        input_fingerprint=snapshot.input_fingerprint,
    )
    existing = load_window_snapshot(
        output_dir,
        snapshot.as_of_date,
        snapshot.lookback_days,
        input_fingerprint=snapshot.input_fingerprint,
    )
    if existing is not None:
        if existing != snapshot:
            raise ValueError(f"窗口快照已存在，不能覆盖历史结果：{path}")
        return path
    _write_json_atomically(path, _snapshot_payload(snapshot))
    return path


def load_window_snapshot(
    output_dir: Path,
    as_of_date: date | datetime | str,
    lookback_days: int,
    *,
    input_fingerprint: str = "",
) -> WindowSnapshot | None:
    path = window_snapshot_path(
        output_dir,
        as_of_date,
        lookback_days,
        input_fingerprint=input_fingerprint,
    )
    if not path.is_file():
        return None
    snapshot = _snapshot_from_payload(_read_json_mapping(path))
    expected_day = _as_date(as_of_date)
    if (
        snapshot.as_of_date != expected_day
        or snapshot.lookback_days != int(lookback_days)
        or snapshot.input_fingerprint != input_fingerprint
    ):
        raise ValueError(f"窗口快照与请求日期或窗口不匹配：{path}")
    return snapshot


def _return_history_payload(output_dir: Path) -> dict[str, object]:
    path = _return_history_path(output_dir)
    if not path.is_file():
        return {"schema_version": ENSEMBLE_SCHEMA_VERSION, "returns": {}}
    payload = _read_json_mapping(path)
    records = payload.get("returns")
    if not isinstance(records, Mapping):
        raise ValueError("收益历史缺少returns对象。")
    return payload


def _update_return_history(
    output_dir: Path,
    values_by_date: Mapping[date, Mapping[int, float]],
    *,
    source: str,
) -> int:
    payload = _return_history_payload(output_dir)
    raw_records = payload.setdefault("returns", {})
    if not isinstance(raw_records, dict):
        raise ValueError("收益历史returns对象不可写。")
    updated = 0
    for verification_date, values_by_window in values_by_date.items():
        day_label = _as_date(verification_date).isoformat()
        raw_day = raw_records.setdefault(day_label, {})
        if not isinstance(raw_day, dict):
            raise ValueError(f"收益历史日期记录无效：{day_label}")
        for window, raw_value in values_by_window.items():
            lookback_days = int(window)
            if lookback_days not in ENSEMBLE_WINDOWS:
                continue
            value = float(raw_value)
            if not math.isfinite(value):
                continue
            raw_day[str(lookback_days)] = {
                "total_return_pct": value,
                "source": source,
            }
            updated += 1
    payload["schema_version"] = ENSEMBLE_SCHEMA_VERSION
    _write_json_atomically(_return_history_path(output_dir), payload)
    return updated


def record_return(output_dir: Path, snapshot: WindowSnapshot) -> None:
    """Record the raw total return used by the five-day Sharpe calculation."""

    _update_return_history(
        output_dir,
        {
            snapshot.verification_date: {
                snapshot.lookback_days: snapshot.total_return_pct,
            }
        },
        source=snapshot.source_report,
    )


def load_return_history(output_dir: Path) -> dict[date, dict[int, float]]:
    """Load validated raw total-return values keyed by verification date."""

    payload = _return_history_payload(output_dir)
    raw_records = payload["returns"]
    assert isinstance(raw_records, Mapping)
    result: dict[date, dict[int, float]] = {}
    for raw_day, raw_values in raw_records.items():
        if not isinstance(raw_values, Mapping):
            raise ValueError(f"收益历史日期记录无效：{raw_day}")
        verification_date = _as_date(str(raw_day))
        values: dict[int, float] = {}
        for raw_window, raw_entry in raw_values.items():
            try:
                window = int(raw_window)
            except (TypeError, ValueError) as exc:
                raise ValueError("收益历史窗口标签无效。") from exc
            if window not in ENSEMBLE_WINDOWS:
                continue
            raw_value = (
                raw_entry.get("total_return_pct")
                if isinstance(raw_entry, Mapping)
                else raw_entry
            )
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("收益历史收益率无效。") from exc
            if not math.isfinite(value):
                raise ValueError("收益历史收益率必须是有限数值。")
            values[window] = value
        if values:
            result[verification_date] = values
    return result


def import_return_matrix(csv_path: Path, output_dir: Path) -> int:
    """Seed the raw-return cache from a BOM-tolerant optimizer sweep matrix."""

    frame = pd.read_csv(Path(csv_path), encoding="utf-8-sig")
    if frame.shape[1] < 2:
        raise ValueError("收益率矩阵至少需要一个窗口列和一个日期列。")
    window_column = frame.columns[0]
    date_columns: list[tuple[str, date]] = []
    for column in frame.columns[1:]:
        parsed = pd.to_datetime(column, errors="coerce")
        if not pd.isna(parsed):
            date_columns.append((str(column), pd.Timestamp(parsed).date()))
    if not date_columns:
        raise ValueError("收益率矩阵没有可识别的日期列。")

    values_by_date: dict[date, dict[int, float]] = {}
    for _, row in frame.iterrows():
        raw_window = pd.to_numeric(row[window_column], errors="coerce")
        if pd.isna(raw_window):
            continue
        window = int(raw_window)
        if window not in ENSEMBLE_WINDOWS:
            continue
        for column, verification_date in date_columns:
            raw_value = pd.to_numeric(row[column], errors="coerce")
            if pd.isna(raw_value):
                continue
            value = float(raw_value)
            if math.isfinite(value):
                values_by_date.setdefault(verification_date, {})[window] = value
    return _update_return_history(
        output_dir,
        values_by_date,
        source=f"matrix_import:{Path(csv_path).resolve()}",
    )


def select_completed_verification_dates(
    return_data: core.ReturnData,
    *,
    as_of_date: date | datetime | str | None = None,
) -> tuple[date, ...]:
    """Return the latest five actual trading days with known next-day returns."""

    cutoff = _as_date(as_of_date or date.today())
    completed_days = sorted(
        {
            verification_day
            for signal_day, verification_day in return_data.next_trade_dates.items()
            if signal_day <= cutoff and verification_day <= cutoff
        }
    )
    if len(completed_days) < ROLLING_RETURN_COUNT:
        raise ValueError(
            f"截至{cutoff.isoformat()}的已完成验证交易日不足{ROLLING_RETURN_COUNT}个。"
        )
    return tuple(completed_days[-ROLLING_RETURN_COUNT:])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_fingerprint(returns_workbook: Path, stock_pool: Path) -> str:
    """Fingerprint inputs whose revisions must create a new component snapshot."""

    optimizer_module_path = Path(optimizer.__file__).resolve()
    identity = {
        "returns_workbook": str(Path(returns_workbook).resolve()),
        "returns_workbook_sha256": _file_sha256(Path(returns_workbook)),
        "stock_pool": str(Path(stock_pool).resolve()),
        "stock_pool_sha256": _file_sha256(Path(stock_pool)),
        "strategy_sha256": core.STRATEGY_SNAPSHOT_SIGNATURE,
        "optimizer_sha256": _file_sha256(optimizer_module_path),
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replace_optimizer_argument(
    arguments: list[str],
    option: str,
    value: str,
) -> None:
    try:
        index = arguments.index(option)
    except ValueError:
        arguments.extend((option, value))
        return
    if index + 1 >= len(arguments):
        raise ValueError(f"优化器参数缺少{option}的值。")
    arguments[index + 1] = value


def _snapshot_from_optimizer_report(
    report_path: Path,
    *,
    as_of_date: date,
    lookback_days: int,
    input_fingerprint: str,
) -> WindowSnapshot:
    payload = _read_json_mapping(report_path)
    if _as_date(str(payload.get("run_date", ""))) != as_of_date:
        raise ValueError(f"优化器报告日期不匹配：{report_path}")
    raw_result = payload.get("best_result")
    raw_settings = payload.get("best_settings")
    raw_data_window = payload.get("data_window")
    if not isinstance(raw_result, Mapping) or not isinstance(raw_settings, Mapping):
        raise ValueError(f"优化器报告缺少最优结果：{report_path}")
    if not isinstance(raw_data_window, Mapping):
        raise ValueError(f"优化器报告缺少数据窗口：{report_path}")
    try:
        verification_date = _as_date(str(raw_data_window["last_verification_date"]))
        reported_lookback = int(raw_data_window["lookback_signal_days"])
        total_return_pct = float(raw_result["total_return_pct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"优化器报告数据窗口无效：{report_path}") from exc
    if verification_date != as_of_date or reported_lookback != lookback_days:
        raise ValueError(f"优化器报告没有严格覆盖目标交易日：{report_path}")
    return WindowSnapshot(
        as_of_date=as_of_date,
        verification_date=verification_date,
        lookback_days=lookback_days,
        best_settings=optimizer.normalize_tunable_settings(raw_settings),
        total_return_pct=total_return_pct,
        source_report=str(report_path.resolve()),
        data_window=dict(raw_data_window),
        input_fingerprint=input_fingerprint,
    )


def _archive_optimizer_report(
    report_path: Path,
    *,
    output_dir: Path,
    as_of_date: date,
    lookback_days: int,
    input_fingerprint: str,
) -> Path:
    """Copy a mutable optimizer output into the immutable snapshot audit tree."""

    archived_path = _window_report_path(
        output_dir,
        as_of_date,
        lookback_days,
        input_fingerprint=input_fingerprint,
    )
    report_bytes = report_path.read_bytes()
    if archived_path.is_file():
        if archived_path.read_bytes() != report_bytes:
            raise ValueError(f"窗口审计报告已存在，不能覆盖历史结果：{archived_path}")
        return archived_path
    _write_bytes_atomically(archived_path, report_bytes)
    return archived_path


def ensure_window_snapshot(
    *,
    output_dir: Path,
    as_of_date: date | datetime | str,
    lookback_days: int,
    optimizer_arguments: Sequence[str],
    optimizer_main: Callable[[Sequence[str]], int] = optimizer.main,
    force_refresh: bool = False,
    input_fingerprint: str = "",
) -> WindowSnapshot:
    """Reuse or generate one isolated window snapshot for the target day."""

    target_day = _as_date(as_of_date)
    if lookback_days not in ENSEMBLE_WINDOWS:
        raise ValueError("只支持14、30、45日窗口。")
    existing = load_window_snapshot(
        output_dir,
        target_day,
        lookback_days,
        input_fingerprint=input_fingerprint,
    )
    if existing is not None:
        # Snapshots are immutable.  A retry may repair a prior failure between
        # snapshot persistence and return-history persistence, but never
        # recomputes the historical component result in place.
        record_return(output_dir, existing)
        return existing

    window_output_dir = _window_run_directory(output_dir, lookback_days)
    arguments = list(optimizer_arguments)
    _replace_optimizer_argument(arguments, "--output-dir", str(window_output_dir))
    _replace_optimizer_argument(arguments, "--as-of-date", target_day.isoformat())
    _replace_optimizer_argument(arguments, "--lookback-days", str(lookback_days))
    exit_code = optimizer_main(arguments)
    if exit_code != 0:
        raise RuntimeError(f"{lookback_days}日窗口优化失败，退出码{exit_code}。")

    report_path = window_output_dir / (
        f"{optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
    )
    if not report_path.is_file():
        raise RuntimeError(f"{lookback_days}日窗口未生成日期参数文件：{report_path}")
    archived_report_path = _archive_optimizer_report(
        report_path,
        output_dir=Path(output_dir),
        as_of_date=target_day,
        lookback_days=lookback_days,
        input_fingerprint=input_fingerprint,
    )
    snapshot = _snapshot_from_optimizer_report(
        archived_report_path,
        as_of_date=target_day,
        lookback_days=lookback_days,
        input_fingerprint=input_fingerprint,
    )
    save_window_snapshot(output_dir, snapshot)
    record_return(output_dir, snapshot)
    return snapshot


def _missing_return_history_reason(
    history: Mapping[date, Mapping[int, float]],
    verification_dates: Sequence[date],
) -> str | None:
    for verification_date in verification_dates:
        values = history.get(verification_date)
        if values is None:
            return f"缺少{verification_date.isoformat()}的收益历史。"
        missing_windows = [
            str(window) for window in ENSEMBLE_WINDOWS if window not in values
        ]
        if missing_windows:
            return (
                f"{verification_date.isoformat()}缺少窗口"
                + "、".join(missing_windows)
                + "日的收益历史。"
            )
    return None


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _write_bytes_atomically(path, previous)


def _parameter_file_day(path: Path) -> date | None:
    if not path.is_file():
        return None
    try:
        payload = _read_json_mapping(path)
        return _as_date(str(payload.get("run_date", "")))
    except ValueError:
        return None


def _parameter_file_verification_day(path: Path) -> date | None:
    """Read the data vintage behind either a single-window or ensemble file."""

    if not path.is_file():
        return None
    try:
        payload = _read_json_mapping(path)
        data_window = payload.get("data_window")
        if isinstance(data_window, Mapping):
            raw_verification_day = data_window.get("last_verification_date")
            if raw_verification_day:
                return _as_date(str(raw_verification_day))
        ensemble_details = payload.get("ensemble")
        if isinstance(ensemble_details, Mapping):
            verification_dates = ensemble_details.get("verification_dates")
            if isinstance(verification_dates, Sequence) and verification_dates:
                return _as_date(str(verification_dates[-1]))
    except ValueError:
        return None
    return None


def publish_ensemble_if_ready(
    *,
    output_dir: Path,
    as_of_date: date | datetime | str,
    verification_dates: Sequence[date | datetime | str],
    input_fingerprint: str = "",
) -> EnsemblePublication:
    """Publish only when exactly five complete, non-future verification days exist."""

    target_day = _as_date(as_of_date)
    recent_days = tuple(_as_date(value) for value in verification_dates)
    if len(recent_days) != ROLLING_RETURN_COUNT:
        return EnsemblePublication(False, "必须提供恰好5个验证交易日。")
    if recent_days != tuple(sorted(set(recent_days))):
        return EnsemblePublication(False, "验证交易日必须严格递增且不重复。")
    if recent_days[-1] != target_day or any(day > target_day for day in recent_days):
        return EnsemblePublication(False, "验证交易日必须截至目标交易日。")

    snapshots: dict[int, WindowSnapshot] = {}
    for window in ENSEMBLE_WINDOWS:
        snapshot = load_window_snapshot(
            output_dir,
            target_day,
            window,
            input_fingerprint=input_fingerprint,
        )
        if snapshot is None:
            return EnsemblePublication(False, f"缺少{window}日当前窗口快照。")
        if snapshot.verification_date != target_day:
            return EnsemblePublication(False, f"{window}日窗口快照不是目标验证日。")
        snapshots[window] = snapshot

    history = load_return_history(output_dir)
    missing_reason = _missing_return_history_reason(history, recent_days)
    if missing_reason is not None:
        return EnsemblePublication(False, missing_reason)
    returns_by_window = {
        window: tuple(history[day][window] for day in recent_days)
        for window in ENSEMBLE_WINDOWS
    }
    scores = calculate_rolling_weights(
        returns_by_window,
        {window: snapshots[window].best_settings for window in ENSEMBLE_WINDOWS},
    )
    best_settings = blend_tunable_settings(
        {window: snapshots[window].best_settings for window in ENSEMBLE_WINDOWS},
        {window: scores[window].weight for window in ENSEMBLE_WINDOWS},
    )
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ensemble_details = {
        "windows": list(ENSEMBLE_WINDOWS),
        "verification_dates": [day.isoformat() for day in recent_days],
        "raw_total_returns": {
            str(window): list(scores[window].returns) for window in ENSEMBLE_WINDOWS
        },
        "scores": {
            str(window): {
                "mean": scores[window].mean,
                "sample_stddev_ddof1": scores[window].sample_stddev,
                "raw_sharpe": scores[window].raw_sharpe,
                "relu_sharpe": scores[window].relu_sharpe,
                "coverage_factor": scores[window].coverage_factor,
                "coverage_adjusted_score": scores[window].coverage_adjusted_score,
                "out_of_range_setting_keys": list(
                    scores[window].out_of_range_setting_keys
                ),
                "range_penalty_multiplier": scores[window].range_penalty_multiplier,
                "penalized_score": scores[window].penalized_score,
                "weight": scores[window].weight,
            }
            for window in ENSEMBLE_WINDOWS
        },
        "weights": {
            str(window): scores[window].weight for window in ENSEMBLE_WINDOWS
        },
        "component_snapshots": {
            str(window): str(
                window_snapshot_path(
                    output_dir,
                    target_day,
                    window,
                    input_fingerprint=input_fingerprint,
                ).resolve()
            )
            for window in ENSEMBLE_WINDOWS
        },
        "component_reports": {
            str(window): snapshots[window].source_report for window in ENSEMBLE_WINDOWS
        },
        "component_data_windows": {
            str(window): snapshots[window].data_window for window in ENSEMBLE_WINDOWS
        },
        "effective_from": "下一交易日开盘",
        "input_fingerprint": input_fingerprint,
    }
    canonical_payload: dict[str, object] = {
        "schema_version": ENSEMBLE_SCHEMA_VERSION,
        "run_date": target_day.isoformat(),
        "generated_at": generated_at,
        "parameter_source": "rolling_parameter_ensemble",
        "best_settings": best_settings,
        "ensemble": ensemble_details,
    }
    report_path = _ensemble_directory(Path(output_dir)) / (
        f"{ENSEMBLE_REPORT_PREFIX}{target_day.isoformat()}.json"
    )
    report_payload = {
        **canonical_payload,
        "report_type": "rolling_parameter_ensemble",
        "fused_backtest": "未运行；融合后的区间不能由组件收益率线性推导。",
    }
    dated_path = Path(output_dir) / (
        f"{optimizer.REPORT_PREFIX}{target_day.isoformat()}.json"
    )
    current_path = Path(output_dir) / optimizer.CURRENT_PARAMETER_FILENAME
    current_day = _parameter_file_day(current_path)
    current_verification_day = _parameter_file_verification_day(current_path)
    if (
        current_verification_day is not None
        and current_verification_day > target_day
    ) or (
        current_verification_day is None
        and current_day is not None
        and current_day > target_day
    ):
        return EnsemblePublication(
            False,
            "当前参数文件日期更新，拒绝用较早交易日覆盖。",
        )
    previous_dated = dated_path.read_bytes() if dated_path.is_file() else None
    previous_current = current_path.read_bytes() if current_path.is_file() else None
    previous_report = report_path.read_bytes() if report_path.is_file() else None
    try:
        _write_json_atomically(report_path, report_payload)
        _write_json_atomically(dated_path, canonical_payload)
        _write_json_atomically(current_path, canonical_payload)
    except Exception:
        _restore_file(report_path, previous_report)
        _restore_file(dated_path, previous_dated)
        _restore_file(current_path, previous_current)
        raise
    return EnsemblePublication(
        True,
        parameter_path=current_path,
        report_path=report_path,
    )


def _optimizer_arguments(
    *,
    returns_workbook: Path,
    stock_pool: Path,
    output_dir: Path,
    as_of_date: date,
    minimum_prediction_days: int | None = None,
    cache_hours: float = core.DEFAULT_CACHE_HOURS,
    force_refresh: bool = False,
    workers: int = core.DEFAULT_WORKERS,
    factor_workers: int = optimizer.FACTOR_WORKERS,
    interval: float = core.DEFAULT_REQUEST_INTERVAL_SECONDS,
    timeout: float = core.DEFAULT_TIMEOUT_SECONDS,
    max_passes: int = optimizer.DEFAULT_MAX_PASSES,
    confirm_top: int = optimizer.DEFAULT_CONFIRM_TOP,
    batch_size: int = optimizer.DEFAULT_BATCH_SIZE,
) -> list[str]:
    """Build the common input arguments used by every isolated optimizer run."""

    arguments = [
        "--returns-workbook",
        str(Path(returns_workbook)),
        "--stock-pool",
        str(Path(stock_pool)),
        "--output-dir",
        str(Path(output_dir)),
        "--as-of-date",
        as_of_date.isoformat(),
        "--cache-hours",
        str(cache_hours),
        "--workers",
        str(workers),
        "--factor-workers",
        str(factor_workers),
        "--interval",
        str(interval),
        "--timeout",
        str(timeout),
        "--max-passes",
        str(max_passes),
        "--confirm-top",
        str(confirm_top),
        "--batch-size",
        str(batch_size),
    ]
    if minimum_prediction_days is not None:
        arguments.extend(("--minimum-prediction-days", str(minimum_prediction_days)))
    if force_refresh:
        arguments.append("--force-refresh")
    return arguments


def run_ensemble_update(
    *,
    returns_workbook: Path,
    stock_pool: Path,
    output_dir: Path,
    as_of_date: date | datetime | str | None = None,
    optimizer_main: Callable[[Sequence[str]], int] = optimizer.main,
    minimum_prediction_days: int | None = None,
    cache_hours: float = core.DEFAULT_CACHE_HOURS,
    force_refresh: bool = False,
    workers: int = core.DEFAULT_WORKERS,
    factor_workers: int = optimizer.FACTOR_WORKERS,
    interval: float = core.DEFAULT_REQUEST_INTERVAL_SECONDS,
    timeout: float = core.DEFAULT_TIMEOUT_SECONDS,
    max_passes: int = optimizer.DEFAULT_MAX_PASSES,
    confirm_top: int = optimizer.DEFAULT_CONFIRM_TOP,
    batch_size: int = optimizer.DEFAULT_BATCH_SIZE,
) -> EnsemblePublication:
    """Update or reuse all component windows and safely publish the fusion.

    The supplied cutoff is normalized to the latest actual verification day in
    the return workbook.  This keeps manual invocations on weekends from
    generating an impossible future-dated optimizer report.
    """

    return_data = core.load_strict_next_day_returns(Path(returns_workbook))
    verification_dates = select_completed_verification_dates(
        return_data,
        as_of_date=as_of_date,
    )
    target_day = verification_dates[-1]
    input_fingerprint = _input_fingerprint(
        Path(returns_workbook),
        Path(stock_pool),
    )
    # A changed window set has no compatible five-day return history. Backfill
    # only the missing verification-day snapshots; normal daily runs touch the
    # latest target day alone and reuse all prior history.
    history = load_return_history(Path(output_dir))
    for verification_day in verification_dates:
        missing_windows = {
            window
            for window in ENSEMBLE_WINDOWS
            if window not in history.get(verification_day, {})
        }
        if verification_day == target_day:
            missing_windows.update(
                window
                for window in ENSEMBLE_WINDOWS
                if load_window_snapshot(
                    Path(output_dir),
                    verification_day,
                    window,
                    input_fingerprint=input_fingerprint,
                )
                is None
            )
        if not missing_windows:
            continue
        common_arguments = _optimizer_arguments(
            returns_workbook=Path(returns_workbook),
            stock_pool=Path(stock_pool),
            output_dir=Path(output_dir),
            as_of_date=verification_day,
            minimum_prediction_days=minimum_prediction_days,
            cache_hours=cache_hours,
            force_refresh=force_refresh,
            workers=workers,
            factor_workers=factor_workers,
            interval=interval,
            timeout=timeout,
            max_passes=max_passes,
            confirm_top=confirm_top,
            batch_size=batch_size,
        )
        for lookback_days in sorted(missing_windows):
            ensure_window_snapshot(
                output_dir=Path(output_dir),
                as_of_date=verification_day,
                lookback_days=lookback_days,
                optimizer_arguments=common_arguments,
                optimizer_main=optimizer_main,
                input_fingerprint=input_fingerprint,
            )
    return publish_ensemble_if_ready(
        output_dir=Path(output_dir),
        as_of_date=target_day,
        verification_dates=verification_dates,
        input_fingerprint=input_fingerprint,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="融合14、30、45日滚动优化参数，并安全发布当前参数文件。"
    )
    parser.add_argument("--returns-workbook", type=Path)
    parser.add_argument("--stock-pool", type=Path, default=optimizer.DEFAULT_STOCK_POOL)
    parser.add_argument("--output-dir", type=Path, default=optimizer.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--as-of-date", type=_as_date)
    parser.add_argument(
        "--import-return-matrix",
        type=Path,
        help="只导入历史收益率矩阵到融合缓存，不运行优化。",
    )
    parser.add_argument("--minimum-prediction-days", type=int)
    parser.add_argument("--cache-hours", type=float, default=core.DEFAULT_CACHE_HOURS)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--workers", type=int, default=core.DEFAULT_WORKERS)
    parser.add_argument("--factor-workers", type=int, default=optimizer.FACTOR_WORKERS)
    parser.add_argument("--interval", type=float, default=core.DEFAULT_REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=core.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-passes", type=int, default=optimizer.DEFAULT_MAX_PASSES)
    parser.add_argument("--confirm-top", type=int, default=optimizer.DEFAULT_CONFIRM_TOP)
    parser.add_argument("--batch-size", type=int, default=optimizer.DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    if args.import_return_matrix is not None:
        imported_count = import_return_matrix(args.import_return_matrix, output_dir)
        print(
            f"已导入{imported_count}条窗口收益记录：{_return_history_path(output_dir)}",
            flush=True,
        )
        return 0

    returns_workbook = (
        Path(args.returns_workbook)
        if args.returns_workbook is not None
        else optimizer._find_latest_returns_workbook()
    )
    try:
        publication = run_ensemble_update(
            returns_workbook=returns_workbook,
            stock_pool=Path(args.stock_pool),
            output_dir=output_dir,
            as_of_date=args.as_of_date,
            minimum_prediction_days=args.minimum_prediction_days,
            cache_hours=float(args.cache_hours),
            force_refresh=bool(args.force_refresh),
            workers=int(args.workers),
            factor_workers=int(args.factor_workers),
            interval=float(args.interval),
            timeout=float(args.timeout),
            max_passes=int(args.max_passes),
            confirm_top=int(args.confirm_top),
            batch_size=int(args.batch_size),
        )
    except Exception as exc:
        print(f"滚动参数融合失败：{exc}", flush=True)
        return 1

    if publication.published:
        print(f"融合参数文件：{publication.parameter_path}", flush=True)
        print(f"融合审计报告：{publication.report_path}", flush=True)
        return 0

    current_path = output_dir / optimizer.CURRENT_PARAMETER_FILENAME
    print(f"融合参数暂未发布：{publication.reason}", flush=True)
    if current_path.is_file():
        print(f"继续保留当前参数文件：{current_path}", flush=True)
        print("本次参数更新未完成，交由定时任务重试。", flush=True)
        return 1
    print("没有可用的当前参数文件。", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
