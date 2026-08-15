"""每日滚动优化默认区间参数，并将结果按日期保存。

本模块复用交互回测的完整因子和严格次日收益口径。它不会改变勾选项或风险
过滤，只会对已启用的可调区间做确定性的坐标搜索。每次运行会优先读取当前
运行日期之前最近一次结果作为起点，因而适合每日滚动更新。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import heapq
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

try:  # 支持 `python -m` 与直接执行两种方式。
    from . import backtest_core as core
    from .runtime_strategy import strategy_app
except ImportError:  # pragma: no cover - 直接执行脚本时使用。
    import backtest_core as core
    from runtime_strategy import strategy_app


PROJECT_DIR = Path(__file__).resolve().parent.parent
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = MODULE_DIR / "outputs" / "rolling_parameter_updates"
DEFAULT_STOCK_POOL = PROJECT_DIR / "深交所数据.xlsx"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MAX_PASSES = 4
DEFAULT_CONFIRM_TOP = 3
DEFAULT_BATCH_SIZE = 2_048
FACTOR_WORKERS = max(1, min(core.MAX_FACTOR_WORKERS, 8))
REPORT_PREFIX = "rolling_parameter_optimization_"
CURRENT_PARAMETER_FILENAME = "rolling_parameter_optimization_current_copy.json"
BACKTEST_PREFIX = "rolling_parameter_backtest_"
BACKTEST_REPORT_TYPE = "rolling_parameter_backtest"
ROLLING_RETURNS_WORKBOOK_NAME = "深市主板每日涨跌幅_滚动更新.xlsx"
BACKTEST_WORKSHEET_NAMES = (
    "优化汇总",
    "参数对比",
    "每日回测",
    "优化路径",
    "数据问题",
)
EPSILON = 1e-9


@dataclass(frozen=True)
class ParameterSpec:
    """一个可搜索的双端闭区间参数。"""

    name: str
    setting_key: str
    selected_key: str
    factor_kind: str
    minimum: float
    maximum: float
    step: float
    integer: bool = False

    @property
    def grid_minimum(self) -> int:
        return int(round(self.minimum / self.step))

    @property
    def grid_maximum(self) -> int:
        return int(round(self.maximum / self.step))


@dataclass(frozen=True)
class OptimizationResult:
    """一组参数经严格回测确认后的结果。"""

    settings: dict[str, tuple[float | int, float | int]]
    total_return_pct: float
    prediction_days: int
    correct_days: int
    accuracy_pct: float


@dataclass(frozen=True)
class RangeCandidate:
    """向量化搜索阶段产生的单坐标候选区间。"""

    minimum: float | int
    maximum: float | int
    total_return_pct: float
    prediction_days: int
    correct_days: int
    accuracy_pct: float


@dataclass(frozen=True)
class CandidateUniverse:
    """移除当前坐标后，保留的每日有序候选股票。"""

    floors: np.ndarray
    ceilings: np.ndarray
    log_returns: np.ndarray
    has_strict_return: np.ndarray
    is_correct: np.ndarray
    day_candidate_indexes: tuple[np.ndarray, ...]


PARAMETER_SPECS = (
    ParameterSpec(
        name="rsi",
        setting_key="szse_quant_filter_rsi_range",
        selected_key="rsi_in_range",
        factor_kind="rsi",
        minimum=0.0,
        maximum=100.0,
        step=0.1,
    ),
    ParameterSpec(
        name="turnover",
        setting_key="szse_quant_filter_turnover_range",
        selected_key="turnover_in_range",
        factor_kind="turnover",
        minimum=0.0,
        maximum=100.0,
        step=0.1,
    ),
    ParameterSpec(
        name="volume_ratio",
        setting_key="szse_quant_filter_volume_ratio_range",
        selected_key="volume_breakout",
        factor_kind="volume_ratio",
        minimum=0.0,
        maximum=15.0,
        step=0.1,
    ),
    ParameterSpec(
        name="pct_change",
        setting_key="szse_quant_filter_pct_change_range",
        selected_key="pct_change_in_range",
        factor_kind="pct_change",
        minimum=-20.0,
        maximum=20.0,
        step=0.1,
    ),
    ParameterSpec(
        name="kdj_age",
        setting_key="szse_quant_filter_kdj_healthy_golden_cross_age_range",
        selected_key="kdj_healthy_golden_cross_3d",
        factor_kind="kdj_age",
        minimum=float(strategy_app.MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE),
        maximum=float(strategy_app.MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE),
        step=1.0,
        integer=True,
    ),
    ParameterSpec(
        name="macd_dea_minus_dif",
        setting_key="szse_quant_filter_macd_dea_minus_dif_range",
        selected_key="macd_dea_minus_dif_high",
        factor_kind="macd_dea_minus_dif",
        minimum=float(strategy_app.MACD_DEA_MINUS_DIF_MIN_THRESHOLD),
        maximum=float(strategy_app.MACD_DEA_MINUS_DIF_MAX_THRESHOLD),
        step=0.01,
    ),
)
SPEC_BY_SETTING_KEY = {spec.setting_key: spec for spec in PARAMETER_SPECS}
TUNABLE_SETTING_KEYS = tuple(spec.setting_key for spec in PARAMETER_SPECS)


def _as_date(value: date | datetime | str | None) -> date:
    """将命令行和报告中的日期统一为 ``date``。"""

    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"日期无效：{value!r}。")
    return pd.Timestamp(parsed).date()


def _as_six_digit_code(value: object) -> str | None:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return None


def _pair(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{label}必须是两个端点组成的区间。")
    try:
        lower = float(value[0])
        upper = float(value[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数值区间。") from exc
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise ValueError(f"{label}不是有效的递增区间。")
    return lower, upper


def _normalize_one_range(
    value: object,
    spec: ParameterSpec,
) -> tuple[float | int, float | int]:
    lower, upper = _pair(value, spec.name)
    if lower < spec.minimum - EPSILON or upper > spec.maximum + EPSILON:
        raise ValueError(
            f"{spec.name}必须在{spec.minimum:g}至{spec.maximum:g}之间。"
        )
    if spec.integer:
        if not lower.is_integer() or not upper.is_integer():
            raise ValueError(f"{spec.name}的端点必须是整数。")
        return int(lower), int(upper)
    return float(lower), float(upper)


def normalize_tunable_settings(
    settings: Mapping[str, object],
) -> dict[str, tuple[float | int, float | int]]:
    """验证并规范化所有可滚动优化的参数。"""

    normalized: dict[str, tuple[float | int, float | int]] = {}
    for spec in PARAMETER_SPECS:
        if spec.setting_key not in settings:
            raise ValueError(f"初始参数缺少{spec.setting_key}。")
        normalized[spec.setting_key] = _normalize_one_range(
            settings[spec.setting_key], spec
        )
    return normalized


def _extract_best_settings(payload: Mapping[str, object]) -> Mapping[str, object]:
    candidate = payload.get("best_settings", payload)
    if not isinstance(candidate, Mapping):
        raise ValueError("参数文件缺少best_settings对象。")
    return candidate


def _merge_tunable_settings(
    defaults: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    """把报告中的区间覆盖到完整默认设置，保留未优化的开关和风险规则。"""

    merged = deepcopy(dict(defaults))
    for key in TUNABLE_SETTING_KEYS:
        if key in override:
            merged[key] = override[key]
    normalized = normalize_tunable_settings(merged)
    merged.update(normalized)
    return merged


def _load_settings_file(path: Path, defaults: Mapping[str, object]) -> dict[str, object]:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取初始参数文件：{path}") from exc
    if not isinstance(raw_payload, Mapping):
        raise ValueError(f"初始参数文件不是JSON对象：{path}")
    return _merge_tunable_settings(defaults, _extract_best_settings(raw_payload))


def _report_date(path: Path, payload: Mapping[str, object]) -> date | None:
    # 文件名是本模块用于每日继承的权威日期；报告内容被手动复制或编辑时，
    # 不能让内部旧日期把未来文件误识别为可用的历史初始值。
    match = re.fullmatch(
        rf"{re.escape(REPORT_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})", path.stem
    )
    if match is not None:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    raw_date = payload.get("run_date")
    if raw_date is not None:
        try:
            return _as_date(str(raw_date))
        except ValueError:
            pass
    return None


def load_previous_settings(
    output_dir: Path,
    defaults: Mapping[str, object],
    *,
    starter_json: Path | None = None,
    as_of_date: date | datetime | str | None = None,
) -> dict[str, object]:
    """读取显式初始参数或当前日期之前最近一次优化结果。

    未找到可用历史结果时返回独立的默认设置副本。报告日期必须早于本次运行日，
    防止未来结果或同日重复运行结果泄露为初始参数。
    """

    if starter_json is not None:
        return _load_settings_file(Path(starter_json), defaults)

    cutoff = _as_date(as_of_date)
    candidates: list[tuple[date, Path]] = []
    if output_dir.is_dir():
        for report_path in output_dir.glob(f"{REPORT_PREFIX}????-??-??.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            report_day = _report_date(report_path, payload)
            if report_day is not None and report_day < cutoff:
                candidates.append((report_day, report_path))

    for _, report_path in sorted(
        candidates,
        key=lambda item: (item[0], item[1].name),
        reverse=True,
    ):
        try:
            return _load_settings_file(report_path, defaults)
        except ValueError:
            # 损坏或过期的报告不应阻断下一交易日的常规更新。
            continue
    return deepcopy(dict(defaults))


def select_recent_signal_dates(
    return_data: core.ReturnData,
    *,
    as_of_date: date | datetime | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> core.ReturnData:
    """截取截至日期前最近 N 个已有严格次日收益的实际选股日。"""

    if lookback_days <= 0:
        raise ValueError("lookback_days必须为正数。")
    cutoff = _as_date(as_of_date)
    available_days = tuple(
        signal_day
        for signal_day in return_data.signal_dates
        if signal_day <= cutoff
        and return_data.next_trade_dates[signal_day] <= cutoff
    )
    signal_days = available_days[-lookback_days:]
    if not signal_days:
        raise core.BacktestDataError("截至指定日期没有可验证的选股日。")
    selected_days = set(signal_days)
    return core.ReturnData(
        signal_dates=signal_days,
        next_trade_dates={
            signal_day: return_data.next_trade_dates[signal_day]
            for signal_day in signal_days
        },
        strict_returns={
            (signal_day, code): change_pct
            for (signal_day, code), change_pct in return_data.strict_returns.items()
            if signal_day in selected_days
        },
        failed_return_codes=return_data.failed_return_codes,
    )


def _settings_sort_key(
    settings: Mapping[str, tuple[float | int, float | int]],
) -> tuple[tuple[str, float, float], ...]:
    return tuple(
        (key, float(values[0]), float(values[1]))
        for key, values in sorted(settings.items())
    )


def _settings_distance(
    settings: Mapping[str, tuple[float | int, float | int]],
    initial_settings: Mapping[str, tuple[float | int, float | int]] | None,
) -> float:
    if initial_settings is None:
        return 0.0
    distance = 0.0
    for key, spec in SPEC_BY_SETTING_KEY.items():
        current = settings.get(key)
        initial = initial_settings.get(key)
        if current is None or initial is None:
            continue
        span = spec.maximum - spec.minimum
        if span <= 0:
            continue
        distance += (abs(float(current[0]) - float(initial[0])) + abs(
            float(current[1]) - float(initial[1])
        )) / span
    return distance


def best_result(
    results: Iterable[OptimizationResult],
    initial_settings: Mapping[str, tuple[float | int, float | int]] | None = None,
) -> OptimizationResult:
    """按收益、预测覆盖、正确率、与起点距离和字典序确定唯一最佳结果。"""

    candidates = list(results)
    if not candidates:
        raise ValueError("没有可比较的优化结果。")
    return min(
        candidates,
        key=lambda result: (
            -result.total_return_pct,
            -result.prediction_days,
            -result.accuracy_pct,
            _settings_distance(result.settings, initial_settings),
            _settings_sort_key(result.settings),
        ),
    )


def _numeric(value: object) -> float | None:
    numeric_value = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric_value) or not math.isfinite(float(numeric_value)):
        return None
    return float(numeric_value)


def _factor_value(row: Mapping[str, object], spec: ParameterSpec) -> float | None:
    if spec.factor_kind == "rsi":
        return _numeric(row.get("RSI14"))
    if spec.factor_kind == "turnover":
        return _numeric(row.get("换手率"))
    if spec.factor_kind == "volume_ratio":
        return _numeric(row.get("量比"))
    if spec.factor_kind == "pct_change":
        return _numeric(row.get("当日涨跌幅"))
    if spec.factor_kind == "kdj_age":
        return _numeric(row.get(strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN))
    if spec.factor_kind == "macd_dea_minus_dif":
        dea = _numeric(row.get("MACD_DEA"))
        dif = _numeric(row.get("MACD_DIF"))
        return None if dea is None or dif is None else dea - dif
    raise ValueError(f"未知优化参数类型：{spec.factor_kind}")


def _grid_floor(value: float, spec: ParameterSpec) -> int:
    return int(math.floor(value / spec.step + EPSILON))


def _grid_ceil(value: float, spec: ParameterSpec) -> int:
    return int(math.ceil(value / spec.step - EPSILON))


def _grid_value(value: int, spec: ParameterSpec) -> float | int:
    numeric_value = value * spec.step
    if spec.integer:
        return int(round(numeric_value))
    precision = 2 if spec.step < 0.1 else 1
    return round(numeric_value, precision)


def _selected_and_risks(
    settings: Mapping[str, object],
) -> tuple[dict[str, bool], dict[str, bool]]:
    selected = {
        key: bool(settings.get(f"szse_quant_filter_{key}", False))
        for key in strategy_app.SCORING_INDICATOR_KEYS
    }
    patterns = set(settings.get("szse_quant_risk_candlestick_patterns", ()))
    selected_risks = {
        "bias_high": bool(settings.get("szse_quant_risk_bias_high", False)),
        "upper_shadow": bool(settings.get("szse_quant_risk_upper_shadow", False)),
        "resistance_60_day": bool(
            settings.get("szse_quant_risk_resistance_60_day", False)
        ),
        **{
            key: key in patterns for key in strategy_app.CANDLESTICK_RISK_PATTERN_KEYS
        },
    }
    return selected, selected_risks


def _evaluation_kwargs(settings: Mapping[str, object]) -> dict[str, object]:
    """将完整控件配置转换成回测核心需要的参数。"""

    tunable = normalize_tunable_settings(settings)
    float_cap = _pair(
        settings.get(
            "szse_quant_filter_float_market_cap_range_yi",
            strategy_app.DEFAULT_FLOAT_MARKET_CAP_RANGE_YI,
        ),
        "流通市值区间",
    )
    if (
        float_cap[0] < strategy_app.FLOAT_MARKET_CAP_MIN_YI
        or float_cap[1] > strategy_app.FLOAT_MARKET_CAP_MAX_YI
    ):
        raise ValueError("流通市值区间超出页面允许范围。")
    macd_lookback = strategy_app._macd_golden_cross_lookback_days(
        settings.get(
            "szse_quant_filter_macd_golden_cross_lookback_days",
            strategy_app.DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS,
        )
    )
    amplitude = _numeric(
        settings.get("szse_quant_filter_amplitude_threshold", 3.0)
    )
    if amplitude is None or amplitude < 0.0 or amplitude > 30.0:
        raise ValueError("振幅阈值必须在0至30之间。")
    return {
        "turnover_range": tuple(
            float(value)
            for value in tunable["szse_quant_filter_turnover_range"]
        ),
        "float_market_cap_range_yi": float_cap,
        "pct_change_range": tuple(
            float(value)
            for value in tunable["szse_quant_filter_pct_change_range"]
        ),
        "amplitude_threshold": float(amplitude),
        "rsi_range": tuple(
            float(value) for value in tunable["szse_quant_filter_rsi_range"]
        ),
        "macd_golden_cross_lookback_days": macd_lookback,
        "kdj_healthy_golden_cross_age_range": tuple(
            int(value)
            for value in tunable[
                "szse_quant_filter_kdj_healthy_golden_cross_age_range"
            ]
        ),
        "macd_dea_minus_dif_range": tuple(
            float(value)
            for value in tunable["szse_quant_filter_macd_dea_minus_dif_range"]
        ),
        "volume_ratio_range": tuple(
            float(value)
            for value in tunable["szse_quant_filter_volume_ratio_range"]
        ),
    }


def _build_candidate_universe(
    return_data: core.ReturnData,
    factors_by_day: Mapping[date, Sequence[Mapping[str, object]]],
    settings: Mapping[str, object],
    selected: Mapping[str, bool],
    selected_risks: Mapping[str, bool],
    spec: ParameterSpec,
) -> CandidateUniverse:
    """仅移除当前参数条件，其余筛选和风险规则保持不变。"""

    candidate_selected = dict(selected)
    candidate_selected[spec.selected_key] = False
    floors: list[int] = []
    ceilings: list[int] = []
    log_returns: list[float] = []
    has_strict_return: list[bool] = []
    is_correct: list[bool] = []
    day_candidate_indexes: list[np.ndarray] = []
    kwargs = _evaluation_kwargs(settings)

    for signal_day in return_data.signal_dates:
        factors = pd.DataFrame(factors_by_day.get(signal_day, ()))
        if factors.empty:
            day_candidate_indexes.append(np.empty(0, dtype=np.int32))
            continue
        selection, _, _ = strategy_app.score_and_select(
            factors,
            candidate_selected,
            selected_risks=selected_risks,
            require_all=bool(settings.get("szse_quant_filter_require_all", True)),
            top_n=max(len(factors), 1),
            **kwargs,
        )
        start_index = len(floors)
        for _, row in selection.iterrows():
            value = _factor_value(row, spec)
            if (
                value is None
                or value < spec.minimum - EPSILON
                or value > spec.maximum + EPSILON
            ):
                continue
            code = _as_six_digit_code(row.get("股票代码"))
            if code is None:
                continue
            next_return = return_data.strict_returns.get((signal_day, code))
            valid_return = next_return is not None and math.isfinite(float(next_return))
            return_value = float(next_return) if valid_return else 0.0
            multiplier = 1.0 + return_value / 100.0
            if multiplier <= 0.0:
                valid_return = False
                return_value = 0.0
                multiplier = 1.0
            floors.append(_grid_floor(value, spec))
            ceilings.append(_grid_ceil(value, spec))
            log_returns.append(math.log(multiplier))
            has_strict_return.append(valid_return)
            is_correct.append(valid_return and return_value > 0.0)
        day_candidate_indexes.append(
            np.arange(start_index, len(floors), dtype=np.int32)
        )

    return CandidateUniverse(
        floors=np.asarray(floors, dtype=np.int32),
        ceilings=np.asarray(ceilings, dtype=np.int32),
        log_returns=np.asarray(log_returns, dtype=np.float64),
        has_strict_return=np.asarray(has_strict_return, dtype=bool),
        is_correct=np.asarray(is_correct, dtype=bool),
        day_candidate_indexes=tuple(day_candidate_indexes),
    )


def _candidate_range_pairs(
    universe: CandidateUniverse,
    spec: ParameterSpec,
    current_range: tuple[float | int, float | int],
) -> tuple[np.ndarray, np.ndarray]:
    """枚举会改变候选集合的页面网格端点，包含当前参数。"""

    lower_values = {
        spec.grid_minimum,
        _grid_floor(float(current_range[0]), spec),
    }
    upper_values = {
        spec.grid_maximum,
        _grid_ceil(float(current_range[1]), spec),
    }
    lower_values.update(
        int(value)
        for value in universe.floors
        if spec.grid_minimum <= int(value) <= spec.grid_maximum
    )
    upper_values.update(
        int(value)
        for value in universe.ceilings
        if spec.grid_minimum <= int(value) <= spec.grid_maximum
    )
    lower_grid, upper_grid = np.meshgrid(
        np.asarray(sorted(lower_values), dtype=np.int32),
        np.asarray(sorted(upper_values), dtype=np.int32),
        indexing="ij",
    )
    valid = lower_grid <= upper_grid
    return lower_grid[valid], upper_grid[valid]


def _range_candidate_key(candidate: RangeCandidate) -> tuple[float, int, float, float, float]:
    return (
        candidate.total_return_pct,
        candidate.prediction_days,
        candidate.accuracy_pct,
        -(float(candidate.maximum) - float(candidate.minimum)),
        -float(candidate.minimum),
    )


def _evaluate_range_candidates(
    universe: CandidateUniverse,
    lower_grid: np.ndarray,
    upper_grid: np.ndarray,
    spec: ParameterSpec,
    *,
    batch_size: int,
    keep: int,
    minimum_prediction_days: int,
) -> list[RangeCandidate]:
    """向量化评估一个坐标的所有闭区间，只保留待精算的前几名。"""

    best: list[RangeCandidate] = []
    for start in range(0, len(lower_grid), batch_size):
        stop = min(start + batch_size, len(lower_grid))
        minimum_batch = lower_grid[start:stop]
        maximum_batch = upper_grid[start:stop]
        size = len(minimum_batch)
        log_total = np.zeros(size, dtype=np.float64)
        predictions = np.zeros(size, dtype=np.int32)
        correct = np.zeros(size, dtype=np.int32)

        for indexes in universe.day_candidate_indexes:
            if not len(indexes):
                continue
            matched = (
                (minimum_batch[:, None] <= universe.floors[indexes])
                & (maximum_batch[:, None] >= universe.ceilings[indexes])
            )
            found = matched.any(axis=1)
            if not found.any():
                continue
            selected_indexes = indexes[matched.argmax(axis=1)]
            valid_prediction = found & universe.has_strict_return[selected_indexes]
            log_total += np.where(found, universe.log_returns[selected_indexes], 0.0)
            predictions += valid_prediction.astype(np.int32)
            correct += (
                valid_prediction & universe.is_correct[selected_indexes]
            ).astype(np.int32)

        total_returns = np.expm1(log_total) * 100.0
        for position in range(size):
            prediction_days = int(predictions[position])
            if prediction_days < minimum_prediction_days:
                continue
            correct_days = int(correct[position])
            best.append(
                RangeCandidate(
                    minimum=_grid_value(int(minimum_batch[position]), spec),
                    maximum=_grid_value(int(maximum_batch[position]), spec),
                    total_return_pct=float(total_returns[position]),
                    prediction_days=prediction_days,
                    correct_days=correct_days,
                    accuracy_pct=(
                        correct_days / prediction_days * 100.0
                        if prediction_days
                        else 0.0
                    ),
                )
            )
        if len(best) > keep * 12:
            best = heapq.nlargest(keep * 4, best, key=_range_candidate_key)
    return heapq.nlargest(keep, best, key=_range_candidate_key)


def _evaluate_exact(
    return_data: core.ReturnData,
    factors_by_day: Mapping[date, Sequence[Mapping[str, object]]],
    day_stats: Mapping[date, Mapping[str, int]],
    settings: Mapping[str, object],
    selected: Mapping[str, bool],
    selected_risks: Mapping[str, bool],
) -> tuple[OptimizationResult, pd.DataFrame, dict[str, object]]:
    """使用与浏览器回测相同的路径确认某组参数。"""

    daily_results, summary = core.evaluate_strategy(
        return_data,
        factors_by_day,
        day_stats,
        selected=selected,
        selected_risks=selected_risks,
        require_all=bool(settings.get("szse_quant_filter_require_all", True)),
        top_n=1,
        **_evaluation_kwargs(settings),
    )
    returns = pd.to_numeric(
        daily_results.get(
            "次日真实涨跌幅（%）",
            pd.Series(index=daily_results.index, dtype="float64"),
        ),
        errors="coerce",
    )
    prediction_days = int(returns.notna().sum())
    correct_days = int(returns.gt(0.0).sum())
    total_return_pct = float(
        np.prod(1.0 + returns.fillna(0.0).to_numpy(dtype="float64") / 100.0)
        - 1.0
    ) * 100.0
    return (
        OptimizationResult(
            settings=normalize_tunable_settings(settings),
            total_return_pct=total_return_pct,
            prediction_days=prediction_days,
            correct_days=correct_days,
            accuracy_pct=(
                correct_days / prediction_days * 100.0 if prediction_days else 0.0
            ),
        ),
        daily_results,
        dict(summary),
    )


def _coordinate_search(
    return_data: core.ReturnData,
    factors_by_day: Mapping[date, Sequence[Mapping[str, object]]],
    day_stats: Mapping[date, Mapping[str, int]],
    initial_settings: Mapping[str, object],
    selected: Mapping[str, bool],
    selected_risks: Mapping[str, bool],
    *,
    max_passes: int,
    confirm_top: int,
    batch_size: int,
    minimum_prediction_days: int,
) -> tuple[
    dict[str, object],
    OptimizationResult,
    OptimizationResult,
    pd.DataFrame,
    dict[str, object],
    list[dict[str, object]],
]:
    """从初始参数出发，对已启用参数逐坐标搜索并做精确确认。"""

    if not bool(initial_settings.get("szse_quant_filter_require_all", True)):
        raise ValueError("滚动参数优化当前要求启用“仅保留满足全部勾选条件”。")
    running_settings = deepcopy(dict(initial_settings))
    initial_tunable = normalize_tunable_settings(running_settings)
    baseline, _, _ = _evaluate_exact(
        return_data,
        factors_by_day,
        day_stats,
        running_settings,
        selected,
        selected_risks,
    )
    trace: list[dict[str, object]] = []
    active_specs = [spec for spec in PARAMETER_SPECS if selected.get(spec.selected_key)]

    for pass_number in range(1, max_passes + 1):
        changed = False
        for spec in active_specs:
            current_range = normalize_tunable_settings(running_settings)[spec.setting_key]
            universe = _build_candidate_universe(
                return_data,
                factors_by_day,
                running_settings,
                selected,
                selected_risks,
                spec,
            )
            if not len(universe.floors):
                trace.append(
                    {
                        "pass": pass_number,
                        "parameter": spec.name,
                        "before": list(current_range),
                        "status": "没有可用于该坐标的候选股票，保持初始值",
                    }
                )
                continue
            lower_grid, upper_grid = _candidate_range_pairs(
                universe,
                spec,
                current_range,
            )
            vector_candidates = _evaluate_range_candidates(
                universe,
                lower_grid,
                upper_grid,
                spec,
                batch_size=batch_size,
                keep=confirm_top,
                minimum_prediction_days=minimum_prediction_days,
            )
            current_result, _, _ = _evaluate_exact(
                return_data,
                factors_by_day,
                day_stats,
                running_settings,
                selected,
                selected_risks,
            )
            confirmed: list[OptimizationResult] = []
            if current_result.prediction_days >= minimum_prediction_days:
                confirmed.append(current_result)
            for candidate in vector_candidates:
                candidate_settings = deepcopy(running_settings)
                candidate_settings[spec.setting_key] = (
                    candidate.minimum,
                    candidate.maximum,
                )
                exact_result, _, _ = _evaluate_exact(
                    return_data,
                    factors_by_day,
                    day_stats,
                    candidate_settings,
                    selected,
                    selected_risks,
                )
                if exact_result.prediction_days >= minimum_prediction_days:
                    confirmed.append(exact_result)
            if not confirmed:
                trace.append(
                    {
                        "pass": pass_number,
                        "parameter": spec.name,
                        "before": list(current_range),
                        "candidate_intervals": int(len(lower_grid)),
                        "status": "没有满足最小预测天数的候选，保持初始值",
                    }
                )
                continue
            winner = best_result(confirmed, initial_tunable)
            after = winner.settings[spec.setting_key]
            running_settings.update(winner.settings)
            changed = changed or tuple(current_range) != tuple(after)
            trace.append(
                {
                    "pass": pass_number,
                    "parameter": spec.name,
                    "before": list(current_range),
                    "after": list(after),
                    "candidate_rows_after_other_rules": int(len(universe.floors)),
                    "candidate_intervals": int(len(lower_grid)),
                    "confirmed_result": asdict(winner),
                }
            )
        if not changed:
            break

    final_result, final_daily_results, final_summary = _evaluate_exact(
        return_data,
        factors_by_day,
        day_stats,
        running_settings,
        selected,
        selected_risks,
    )
    return (
        running_settings,
        baseline,
        final_result,
        final_daily_results,
        final_summary,
        trace,
    )


def _json_safe(value: object) -> object:
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if not isinstance(value, (str, bytes)):
        try:
            is_missing = pd.isna(value)
        except (TypeError, ValueError):
            is_missing = False
        if isinstance(is_missing, (bool, np.bool_)) and is_missing:
            return None
    return value


def _settings_rows(
    settings: Mapping[str, tuple[float | int, float | int]],
    label: str,
) -> list[dict[str, object]]:
    return [
        {
            "参数来源": label,
            "参数键": key,
            "最小值": values[0],
            "最大值": values[1],
        }
        for key, values in sorted(settings.items())
    ]


def _report_sheet(frame: pd.DataFrame) -> dict[str, object]:
    """将原 Excel 工作表的列与行完整地转换为 JSON 可表示的数据。"""

    return {
        "columns": [str(column) for column in frame.columns],
        "rows": _json_safe(frame.to_dict(orient="records")),
    }


def _summary_rows(
    result: OptimizationResult,
    date_label: str,
    baseline_result: OptimizationResult | None,
    metadata: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    summary_rows: list[dict[str, object]] = [
        {"项目": "运行日期", "数值": date_label},
        {"项目": "总收益率（%）", "数值": result.total_return_pct},
        {"项目": "预测天数", "数值": result.prediction_days},
        {"项目": "正确天数", "数值": result.correct_days},
        {"项目": "正确率（%）", "数值": result.accuracy_pct},
    ]
    if baseline_result is not None:
        summary_rows.extend(
            [
                {
                    "项目": "初始参数总收益率（%）",
                    "数值": baseline_result.total_return_pct,
                },
                {"项目": "初始参数预测天数", "数值": baseline_result.prediction_days},
                {
                    "项目": "初始参数正确率（%）",
                    "数值": baseline_result.accuracy_pct,
                },
            ]
        )
    if metadata:
        summary_rows.extend(
            {"项目": str(key), "数值": _json_safe(value)}
            for key, value in metadata.items()
        )
    return summary_rows


def _trace_rows(trace: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {str(key): _json_safe(value) for key, value in item.items()}
        for item in trace
    ]


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _backtest_report_payload(
    *,
    run_date: str,
    generated_at: str,
    worksheets: Mapping[str, Mapping[str, object]],
    source_workbook: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "report_type": BACKTEST_REPORT_TYPE,
        "run_date": run_date,
        "generated_at": generated_at,
        "worksheet_order": list(BACKTEST_WORKSHEET_NAMES),
        "worksheets": {
            sheet_name: dict(
                worksheets.get(sheet_name, {"columns": [], "rows": []})
            )
            for sheet_name in BACKTEST_WORKSHEET_NAMES
        },
    }
    if source_workbook is not None:
        payload["source_workbook"] = source_workbook
    return payload


def _decode_legacy_json_cell(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def convert_legacy_backtest_report(
    xlsx_path: Path,
    *,
    json_path: Path | None = None,
) -> Path:
    """将旧版 Excel 回测报告转换成当前 JSON 报告，不删除原文件。"""

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"未找到旧版回测报告：{xlsx_path}")
    match = re.fullmatch(
        rf"{re.escape(BACKTEST_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})", xlsx_path.stem
    )
    if match is None:
        raise ValueError(
            f"旧版回测报告文件名必须是 {BACKTEST_PREFIX}YYYY-MM-DD.xlsx：{xlsx_path.name}"
        )
    output_path = (
        Path(json_path)
        if json_path is not None
        else xlsx_path.with_suffix(".json")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    worksheets: dict[str, dict[str, object]] = {}
    json_columns = {
        "优化汇总": {"数值"},
        "优化路径": {"before", "after", "confirmed_result"},
    }
    with pd.ExcelFile(xlsx_path) as workbook:
        for sheet_name in BACKTEST_WORKSHEET_NAMES:
            if sheet_name not in workbook.sheet_names:
                continue
            frame = pd.read_excel(
                xlsx_path,
                sheet_name=sheet_name,
                dtype=object,
            )
            for column in json_columns.get(sheet_name, set()):
                if column in frame:
                    frame[column] = frame[column].map(_decode_legacy_json_cell)
            worksheets[sheet_name] = _report_sheet(frame)
    payload = _backtest_report_payload(
        run_date=match.group(1),
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        worksheets=worksheets,
        source_workbook=xlsx_path.name,
    )
    _write_json_atomically(output_path, payload)
    return output_path


def persist_optimization_result(
    result: OptimizationResult,
    output_dir: Path,
    *,
    as_of_date: date | datetime | str,
    daily_results: pd.DataFrame,
    baseline_result: OptimizationResult | None = None,
    trace: Sequence[Mapping[str, object]] = (),
    metadata: Mapping[str, object] | None = None,
    data_problems: pd.DataFrame | None = None,
) -> tuple[Path, Path]:
    """写入日期参数、固定当前参数和完整的回测报告 JSON。"""

    run_day = _as_date(as_of_date)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_label = run_day.isoformat()
    json_path = output_dir / f"{REPORT_PREFIX}{date_label}.json"
    current_json_path = output_dir / CURRENT_PARAMETER_FILENAME
    report_path = output_dir / f"{BACKTEST_PREFIX}{date_label}.json"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_date": date_label,
        "generated_at": generated_at,
        "best_settings": _json_safe(result.settings),
        "best_result": _json_safe(asdict(result)),
    }
    if baseline_result is not None:
        payload["baseline_result"] = _json_safe(asdict(baseline_result))
    if trace:
        payload["coordinate_search_trace"] = _json_safe(list(trace))
    if metadata:
        payload.update(_json_safe(dict(metadata)))

    _write_json_atomically(json_path, payload)
    _write_json_atomically(current_json_path, payload)

    parameter_rows = _settings_rows(result.settings, "最优参数")
    if baseline_result is not None:
        parameter_rows = _settings_rows(
            baseline_result.settings, "初始参数"
        ) + parameter_rows
    empty_problems = pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS)
    report_payload = _backtest_report_payload(
        run_date=date_label,
        generated_at=generated_at,
        worksheets={
            "优化汇总": _report_sheet(
                pd.DataFrame(
                    _summary_rows(result, date_label, baseline_result, metadata)
                )
            ),
            "参数对比": _report_sheet(pd.DataFrame(parameter_rows)),
            "每日回测": _report_sheet(daily_results),
            "优化路径": _report_sheet(pd.DataFrame(_trace_rows(trace))),
            "数据问题": _report_sheet(
                data_problems
                if isinstance(data_problems, pd.DataFrame)
                else empty_problems
            ),
        },
    )
    _write_json_atomically(report_path, report_payload)
    return json_path, report_path


def _merge_problem_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    usable = [
        frame.reindex(columns=core.HISTORY_ERROR_COLUMNS)
        for frame in frames
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if not usable:
        return pd.DataFrame(columns=core.HISTORY_ERROR_COLUMNS)
    return pd.concat(usable, ignore_index=True)


def _find_latest_returns_workbook() -> Path:
    """在项目常用目录中寻找最新的每日涨跌幅工作簿。"""

    rolling_workbook = MODULE_DIR / "outputs" / "input_data" / ROLLING_RETURNS_WORKBOOK_NAME
    if rolling_workbook.is_file():
        return rolling_workbook.resolve()

    candidates: dict[Path, tuple[date, int]] = {}
    for directory in (PROJECT_DIR, MODULE_DIR / "outputs" / "input_data"):
        if not directory.is_dir():
            continue
        for path in directory.glob("深市主板每日涨跌幅_*.xlsx"):
            if path.name.startswith("~$"):
                continue
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", path.stem)
            try:
                end_day = date.fromisoformat(dates[-1]) if dates else date.min
            except ValueError:
                end_day = date.min
            candidates[path.resolve()] = (end_day, path.stat().st_mtime_ns)
    if not candidates:
        raise FileNotFoundError(
            "未找到每日涨跌幅工作簿，请通过--returns-workbook明确指定。"
        )
    return max(candidates, key=candidates.__getitem__)


def _exclude_return_failure_companies(
    companies: pd.DataFrame,
    failed_return_codes: frozenset[str],
) -> pd.DataFrame:
    if not failed_return_codes:
        return companies.copy()
    if "股票代码" not in companies:
        raise core.BacktestDataError("股票池缺少“股票代码”列。")
    normalized_codes = companies["股票代码"].map(_as_six_digit_code)
    return companies.loc[~normalized_codes.isin(failed_return_codes)].copy()


def _progress(prefix: str):
    def callback(
        completed: int,
        total: int,
        code: str,
        cache_hits: int,
        succeeded: int,
        failed: int,
    ) -> None:
        if completed == total or completed % 50 == 0:
            print(
                f"{prefix} {completed}/{total}；当前{code}；缓存{cache_hits}；"
                f"成功{succeeded}；失败{failed}",
                flush=True,
            )

    return callback


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按最近实际交易日滚动优化已启用的默认区间参数。"
    )
    parser.add_argument("--returns-workbook", type=Path)
    parser.add_argument("--stock-pool", type=Path, default=DEFAULT_STOCK_POOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--starter-json",
        type=Path,
        help="可选的初始参数JSON；未指定时自动继承上一日期的优化结果。",
    )
    parser.add_argument(
        "--as-of-date",
        type=_as_date,
        help="优化截止日期，默认今天；只使用已有严格次日收益的信号日。",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="向前使用的实际可验证选股日数量，默认30。",
    )
    parser.add_argument(
        "--minimum-prediction-days",
        type=int,
        help="候选参数至少需要的有效预测天数；默认窗口的20 percent，且不少于3。",
    )
    parser.add_argument("--cache-hours", type=float, default=core.DEFAULT_CACHE_HOURS)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--workers", type=int, default=core.DEFAULT_WORKERS)
    parser.add_argument("--factor-workers", type=int, default=FACTOR_WORKERS)
    parser.add_argument(
        "--interval",
        type=float,
        default=core.DEFAULT_REQUEST_INTERVAL_SECONDS,
    )
    parser.add_argument("--timeout", type=float, default=core.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES)
    parser.add_argument("--confirm-top", type=int, default=DEFAULT_CONFIRM_TOP)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.lookback_days <= 0 or args.max_passes <= 0:
        raise ValueError("回看交易日数量和搜索轮数必须为正数。")
    if args.confirm_top <= 0 or args.batch_size <= 0:
        raise ValueError("精确确认数量和批大小必须为正数。")
    if args.minimum_prediction_days is not None and args.minimum_prediction_days < 0:
        raise ValueError("最小预测天数不能为负数。")
    if not 1 <= args.workers <= core.MAX_WORKERS:
        raise ValueError(f"workers必须在1至{core.MAX_WORKERS}之间。")
    if not 1 <= args.factor_workers <= core.MAX_FACTOR_WORKERS:
        raise ValueError(
            f"factor-workers必须在1至{core.MAX_FACTOR_WORKERS}之间。"
        )
    if args.cache_hours < 0 or args.interval < 0 or args.timeout <= 0:
        raise ValueError("缓存、请求间隔或超时参数无效。")

    as_of_day = args.as_of_date or date.today()
    returns_workbook = (
        Path(args.returns_workbook) if args.returns_workbook else _find_latest_returns_workbook()
    )
    if not returns_workbook.is_file():
        raise FileNotFoundError(f"未找到收益工作簿：{returns_workbook}")
    if not args.stock_pool.is_file():
        raise FileNotFoundError(f"未找到股票池：{args.stock_pool}")

    full_return_data = core.load_strict_next_day_returns(returns_workbook)
    return_data = select_recent_signal_dates(
        full_return_data,
        as_of_date=as_of_day,
        lookback_days=int(args.lookback_days),
    )
    minimum_prediction_days = (
        int(args.minimum_prediction_days)
        if args.minimum_prediction_days is not None
        else max(3, math.ceil(len(return_data.signal_dates) * 0.2))
    )
    if minimum_prediction_days > len(return_data.signal_dates):
        raise ValueError("最小预测天数不能大于实际回看交易日数量。")

    default_settings = strategy_app.default_screening_settings()
    starter_settings = load_previous_settings(
        Path(args.output_dir),
        default_settings,
        starter_json=args.starter_json,
        as_of_date=as_of_day,
    )
    normalize_tunable_settings(starter_settings)
    selected, selected_risks = _selected_and_risks(starter_settings)
    if not bool(starter_settings.get("szse_quant_filter_require_all", True)):
        raise ValueError("滚动参数优化要求勾选“仅保留满足全部勾选条件”。")

    companies = strategy_app.load_mainboard_companies(args.stock_pool)
    original_company_count = len(companies)
    companies = _exclude_return_failure_companies(
        companies, return_data.failed_return_codes
    )
    if companies.empty:
        raise core.BacktestDataError("收益文件失败明细已剔除全部股票池。")

    full_first_signal_day = full_return_data.signal_dates[0]
    full_last_market_day = full_return_data.next_trade_dates[
        full_return_data.signal_dates[-1]
    ]
    histories, history_errors, history_summary, cache_key = core.collect_full_histories(
        companies,
        first_signal_date=full_first_signal_day,
        end_date=full_last_market_day,
        cache_hours=float(args.cache_hours),
        force_refresh=bool(args.force_refresh),
        workers=int(args.workers),
        request_interval_seconds=float(args.interval),
        timeout_seconds=float(args.timeout),
        progress_callback=_progress("历史行情"),
    )
    factors_by_day, day_stats, factor_errors = core.collect_all_factor_rows_by_day(
        companies,
        histories,
        return_data.signal_dates,
        cache_key=cache_key,
        cache_hours=float(args.cache_hours),
        factor_workers=int(args.factor_workers),
        progress_callback=_progress("因子计算"),
    )

    (
        best_full_settings,
        baseline_result,
        final_result,
        daily_results,
        final_summary,
        trace,
    ) = _coordinate_search(
        return_data,
        factors_by_day,
        day_stats,
        starter_settings,
        selected,
        selected_risks,
        max_passes=int(args.max_passes),
        confirm_top=int(args.confirm_top),
        batch_size=int(args.batch_size),
        minimum_prediction_days=minimum_prediction_days,
    )
    data_problems = _merge_problem_frames((history_errors, factor_errors))
    metadata = {
        "data_window": {
            "return_workbook": str(returns_workbook.resolve()),
            "first_signal_date": return_data.signal_dates[0].isoformat(),
            "last_signal_date": return_data.signal_dates[-1].isoformat(),
            "last_verification_date": return_data.next_trade_dates[
                return_data.signal_dates[-1]
            ].isoformat(),
            "lookback_signal_days": len(return_data.signal_dates),
        },
        "starter_source": (
            str(args.starter_json.resolve())
            if args.starter_json is not None
            else "当前日期之前最近一次有效优化结果；没有时使用程序默认值"
        ),
        "initial_settings": normalize_tunable_settings(starter_settings),
        "screening_settings": best_full_settings,
        "fixed_selected_conditions": selected,
        "fixed_risk_filters": selected_risks,
        "minimum_prediction_days": minimum_prediction_days,
        "history_summary": dict(history_summary),
        "factor_error_count": int(len(factor_errors)),
        "factor_row_count": int(sum(len(rows) for rows in factors_by_day.values())),
        "stock_pool_rows_before_exclusion": original_company_count,
        "stock_pool_rows_used": len(companies),
        "return_failure_stocks_excluded": len(return_data.failed_return_codes),
        "backtest_summary": final_summary,
    }
    json_path, report_path = persist_optimization_result(
        final_result,
        Path(args.output_dir),
        as_of_date=as_of_day,
        daily_results=daily_results,
        baseline_result=baseline_result,
        trace=trace,
        metadata=metadata,
        data_problems=data_problems,
    )
    print(
        "最优参数："
        + "；".join(
            f"{key}={values[0]:g}-{values[1]:g}"
            for key, values in sorted(final_result.settings.items())
        ),
        flush=True,
    )
    print(
        f"结果：{final_result.correct_days}/{final_result.prediction_days}，"
        f"总收益率{final_result.total_return_pct:.4f}%，"
        f"正确率{final_result.accuracy_pct:.2f}%",
        flush=True,
    )
    print(f"参数文件：{json_path}", flush=True)
    print(
        f"当前参数文件：{Path(args.output_dir) / CURRENT_PARAMETER_FILENAME}",
        flush=True,
    )
    print(f"回测报告：{report_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, core.BacktestDataError) as exc:
        print(f"错误：{exc}")
        raise SystemExit(1) from exc
