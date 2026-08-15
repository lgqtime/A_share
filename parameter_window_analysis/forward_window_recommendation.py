"""Forward-test daily parameter-update windows without changing production files.

Each lookback window forms an independent chronological parameter chain. A
daily result is optimized using only returns settled by that date, then scored
against the next trading day's realized return. All files stay below this
directory and can be resumed after interruption.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from dataclasses import asdict
from datetime import date, datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


ANALYSIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ANALYSIS_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from strategy_backtest import rolling_parameter_optimizer as optimizer


DEFAULT_START_DATE = date(2026, 7, 6)
DEFAULT_END_DATE = date(2026, 8, 6)
DEFAULT_LOOKBACK_START = 30
DEFAULT_LOOKBACK_END = 5
DEFAULT_RETURNS_WORKBOOK = (
    PROJECT_DIR
    / "strategy_backtest"
    / "outputs"
    / "input_data"
    / "深市主板每日涨跌幅_滚动更新.xlsx"
)
DEFAULT_STOCK_POOL = PROJECT_DIR / "深交所数据.xlsx"
DEFAULT_OUTPUT_DIR = ANALYSIS_DIR / "forward_recommendation_results"
RESULT_PREFIX = "forward_parameter_optimization_"


def _as_date(value: str) -> date:
    return date.fromisoformat(value)


def lookback_windows(start: int, end: int) -> list[int]:
    if start < end or end <= 0:
        raise ValueError("窗口范围必须从较大的正整数递减到较小的正整数。")
    return list(range(start, end - 1, -1))


def settled_update_days(
    next_trade_dates: Mapping[date, date], *, start_date: date, end_date: date
) -> tuple[date, ...]:
    """Return signal dates that have a known strict next-day settlement."""

    return tuple(
        signal_day
        for signal_day in sorted(next_trade_dates)
        if start_date <= signal_day <= end_date
    )


def _update_days(
    next_trade_dates: Mapping[date, date], *, start_date: date, end_date: date
) -> tuple[date, ...]:
    """Return actual update dates, including the final date awaiting settlement."""

    market_days = sorted(set(next_trade_dates.values()))
    return tuple(day for day in market_days if start_date <= day <= end_date)


def _safe_number(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def rank_windows(daily_rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Rank windows by compounded daily return, counting no-signal days as zero."""

    grouped: dict[int, list[Mapping[str, object]]] = {}
    for row in daily_rows:
        if row.get("settlement_status") != "settled":
            continue
        try:
            lookback_days = int(row["lookback_days"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(lookback_days, []).append(row)

    ranked: list[dict[str, object]] = []
    for lookback_days, rows in grouped.items():
        returns = [_safe_number(row.get("daily_return_pct")) for row in rows]
        multiplier = 1.0
        for value in returns:
            multiplier *= max(0.0, 1.0 + value / 100.0)
        settled_days = len(returns)
        total_return_pct = (multiplier - 1.0) * 100.0
        average_daily_return_pct = (
            (math.pow(multiplier, 1.0 / settled_days) - 1.0) * 100.0
            if settled_days
            else 0.0
        )
        prediction_days = sum(int(_safe_number(row.get("prediction_days"))) for row in rows)
        correct_days = sum(int(_safe_number(row.get("correct_days"))) for row in rows)
        ranked.append(
            {
                "lookback_days": lookback_days,
                "settled_days": settled_days,
                "prediction_days": prediction_days,
                "correct_days": correct_days,
                "accuracy_pct": (
                    correct_days / prediction_days * 100.0 if prediction_days else 0.0
                ),
                "total_return_pct": total_return_pct,
                "average_daily_return_pct": average_daily_return_pct,
                "arithmetic_average_daily_return_pct": sum(returns) / settled_days,
                "positive_return_days": sum(value > 0.0 for value in returns),
                "negative_return_days": sum(value < 0.0 for value in returns),
                "no_signal_days": sum(
                    int(_safe_number(row.get("prediction_days"))) == 0 for row in rows
                ),
            }
        )
    return sorted(
        ranked,
        key=lambda row: (
            -float(row["average_daily_return_pct"]),
            -float(row["total_return_pct"]),
            -int(row["prediction_days"]),
            int(row["lookback_days"]),
        ),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _write_csv(path: Path, rows: Iterable[Mapping[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


def _load_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"结果不是 JSON 对象：{path}")
    return payload


def _result_path(output_dir: Path, lookback_days: int, update_day: date) -> Path:
    return (
        output_dir
        / "optimization_runs"
        / f"lookback_{lookback_days:02d}_days"
        / f"{RESULT_PREFIX}{update_day.isoformat()}.json"
    )


def _optimization_days(
    full_return_data: optimizer.core.ReturnData,
    update_days: Sequence[date],
    maximum_lookback_days: int,
) -> tuple[date, ...]:
    first_training = optimizer.select_recent_signal_dates(
        full_return_data,
        as_of_date=update_days[0],
        lookback_days=maximum_lookback_days,
    ).signal_dates[0]
    last_training = optimizer.select_recent_signal_dates(
        full_return_data,
        as_of_date=update_days[-1],
        lookback_days=maximum_lookback_days,
    ).signal_dates[-1]
    update_day_set = set(update_days)
    return tuple(
        signal_day
        for signal_day in full_return_data.signal_dates
        if first_training <= signal_day <= last_training or signal_day in update_day_set
    )


def _prepare_shared_context(
    *,
    start_date: date,
    end_date: date,
    maximum_lookback_days: int,
    returns_workbook: Path,
    stock_pool: Path,
) -> dict[str, object]:
    full_return_data = optimizer.core.load_strict_next_day_returns(returns_workbook)
    update_days = _update_days(
        full_return_data.next_trade_dates,
        start_date=start_date,
        end_date=end_date,
    )
    if not update_days:
        raise optimizer.core.BacktestDataError("指定区间没有可用交易日。")
    factor_days = _optimization_days(full_return_data, update_days, maximum_lookback_days)
    if not factor_days:
        raise optimizer.core.BacktestDataError("无法确定前向分析所需的因子日期。")

    companies = optimizer.strategy_app.load_mainboard_companies(stock_pool)
    original_company_count = len(companies)
    companies = optimizer._exclude_return_failure_companies(
        companies, full_return_data.failed_return_codes
    )
    if companies.empty:
        raise optimizer.core.BacktestDataError("收益文件失败明细已排除全部股票池。")

    last_market_day = max(full_return_data.next_trade_dates.values())
    histories, history_errors, history_summary, cache_key = optimizer.core.collect_full_histories(
        companies,
        first_signal_date=factor_days[0],
        end_date=last_market_day,
        cache_hours=optimizer.core.DEFAULT_CACHE_HOURS,
        force_refresh=False,
        workers=optimizer.core.DEFAULT_WORKERS,
        request_interval_seconds=optimizer.core.DEFAULT_REQUEST_INTERVAL_SECONDS,
        timeout_seconds=optimizer.core.DEFAULT_TIMEOUT_SECONDS,
        progress_callback=None,
    )
    factors_by_day, day_stats, factor_errors = optimizer.core.collect_all_factor_rows_by_day(
        companies,
        histories,
        factor_days,
        cache_key=cache_key,
        cache_hours=optimizer.core.DEFAULT_CACHE_HOURS,
        factor_workers=optimizer.FACTOR_WORKERS,
        progress_callback=None,
    )
    return {
        "full_return_data": full_return_data,
        "update_days": update_days,
        "factors_by_day": factors_by_day,
        "day_stats": day_stats,
        "history_errors": history_errors,
        "factor_errors": factor_errors,
        "history_summary": history_summary,
        "original_company_count": original_company_count,
        "stock_pool_rows_used": len(companies),
    }


def _window_factor_data(
    context: Mapping[str, object], return_data: optimizer.core.ReturnData
) -> tuple[dict[date, object], dict[date, object]]:
    all_factors = context["factors_by_day"]
    all_stats = context["day_stats"]
    if not isinstance(all_factors, Mapping) or not isinstance(all_stats, Mapping):
        raise ValueError("共享因子数据无效。")
    return (
        {day: all_factors.get(day, ()) for day in return_data.signal_dates},
        {day: all_stats.get(day, {}) for day in return_data.signal_dates},
    )


def _out_of_sample_result(
    *,
    update_day: date,
    full_return_data: optimizer.core.ReturnData,
    context: Mapping[str, object],
    settings: Mapping[str, object],
    selected: Mapping[str, bool],
    selected_risks: Mapping[str, bool],
) -> dict[str, object]:
    if update_day not in full_return_data.next_trade_dates:
        return {
            "settlement_status": "awaiting_settlement",
            "next_trade_date": None,
            "daily_return_pct": None,
            "prediction_days": 0,
            "correct_days": 0,
            "accuracy_pct": None,
        }

    one_day_data = optimizer.core.ReturnData(
        signal_dates=(update_day,),
        next_trade_dates={update_day: full_return_data.next_trade_dates[update_day]},
        strict_returns={
            (signal_day, code): change_pct
            for (signal_day, code), change_pct in full_return_data.strict_returns.items()
            if signal_day == update_day
        },
        failed_return_codes=full_return_data.failed_return_codes,
    )
    factors_by_day, day_stats = _window_factor_data(context, one_day_data)
    result, daily_results, _ = optimizer._evaluate_exact(
        one_day_data,
        factors_by_day,
        day_stats,
        settings,
        selected,
        selected_risks,
    )
    selected_code = None
    if not daily_results.empty and "选中股票代码" in daily_results:
        selected_code = daily_results.iloc[0].get("选中股票代码")
    return {
        "settlement_status": "settled",
        "next_trade_date": full_return_data.next_trade_dates[update_day].isoformat(),
        "daily_return_pct": result.total_return_pct,
        "prediction_days": result.prediction_days,
        "correct_days": result.correct_days,
        "accuracy_pct": result.accuracy_pct,
        "selected_stock_code": selected_code,
    }


def _run_window_chain(
    *,
    lookback_days: int,
    output_dir: Path,
    returns_workbook: Path,
    context: Mapping[str, object],
) -> list[dict[str, object]]:
    full_return_data = context["full_return_data"]
    update_days = context["update_days"]
    if not isinstance(full_return_data, optimizer.core.ReturnData):
        raise ValueError("共享收益数据无效。")
    if not isinstance(update_days, Sequence):
        raise ValueError("更新日期无效。")

    starter_settings = optimizer.strategy_app.default_screening_settings()
    failures: list[dict[str, object]] = []
    for update_day in update_days:
        if not isinstance(update_day, date):
            raise ValueError("更新日期不是 date。")
        result_path = _result_path(output_dir, lookback_days, update_day)
        if result_path.is_file():
            existing = _load_json(result_path)
            full_settings = existing.get("full_settings")
            if not isinstance(full_settings, Mapping):
                raise ValueError(f"已保存结果缺少 full_settings：{result_path}")
            starter_settings = deepcopy(dict(full_settings))
            continue

        try:
            return_data = optimizer.select_recent_signal_dates(
                full_return_data,
                as_of_date=update_day,
                lookback_days=lookback_days,
            )
            factors_by_day, day_stats = _window_factor_data(context, return_data)
            selected, selected_risks = optimizer._selected_and_risks(starter_settings)
            minimum_prediction_days = max(3, math.ceil(len(return_data.signal_dates) * 0.2))
            (
                best_full_settings,
                baseline_result,
                final_result,
                _,
                _,
                _,
            ) = optimizer._coordinate_search(
                return_data,
                factors_by_day,
                day_stats,
                starter_settings,
                selected,
                selected_risks,
                max_passes=optimizer.DEFAULT_MAX_PASSES,
                confirm_top=optimizer.DEFAULT_CONFIRM_TOP,
                batch_size=optimizer.DEFAULT_BATCH_SIZE,
                minimum_prediction_days=minimum_prediction_days,
            )
            outcome = _out_of_sample_result(
                update_day=update_day,
                full_return_data=full_return_data,
                context=context,
                settings=best_full_settings,
                selected=selected,
                selected_risks=selected_risks,
            )
            _write_json(
                result_path,
                {
                    "schema_version": 1,
                    "run_date": update_day.isoformat(),
                    "lookback_days": lookback_days,
                    "training_window": {
                        "first_signal_date": return_data.signal_dates[0].isoformat(),
                        "last_signal_date": return_data.signal_dates[-1].isoformat(),
                        "last_verification_date": full_return_data.next_trade_dates[
                            return_data.signal_dates[-1]
                        ].isoformat(),
                        "lookback_signal_days": len(return_data.signal_dates),
                    },
                    "starter_settings": optimizer._json_safe(starter_settings),
                    "full_settings": optimizer._json_safe(best_full_settings),
                    "best_settings": optimizer._json_safe(final_result.settings),
                    "best_result": optimizer._json_safe(asdict(final_result)),
                    "baseline_result": optimizer._json_safe(asdict(baseline_result)),
                    "out_of_sample": optimizer._json_safe(outcome),
                    "returns_workbook": str(returns_workbook.resolve()),
                    "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
                },
            )
            starter_settings = deepcopy(dict(best_full_settings))
            print(
                f"完成：窗口={lookback_days}，更新日={update_day.isoformat()}，"
                f"样本外状态={outcome['settlement_status']}",
                flush=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(
                {
                    "lookback_days": lookback_days,
                    "update_date": update_day.isoformat(),
                    "reason": str(exc),
                }
            )
            print(
                f"失败：窗口={lookback_days}，更新日={update_day.isoformat()}，原因={exc}",
                file=sys.stderr,
                flush=True,
            )
    return failures


def _worker_entry(payload: Mapping[str, object]) -> list[dict[str, object]]:
    lookbacks = [int(value) for value in payload["lookbacks"]]
    output_dir = Path(str(payload["output_dir"]))
    returns_workbook = Path(str(payload["returns_workbook"]))
    context = _prepare_shared_context(
        start_date=date.fromisoformat(str(payload["start_date"])),
        end_date=date.fromisoformat(str(payload["end_date"])),
        maximum_lookback_days=int(payload["maximum_lookback_days"]),
        returns_workbook=returns_workbook,
        stock_pool=Path(str(payload["stock_pool"])),
    )
    failures: list[dict[str, object]] = []
    for lookback_days in lookbacks:
        failures.extend(
            _run_window_chain(
                lookback_days=lookback_days,
                output_dir=output_dir,
                returns_workbook=returns_workbook,
                context=context,
            )
        )
    return failures


def _daily_rows_from_results(
    *,
    output_dir: Path,
    lookbacks: Sequence[int],
    update_days: Sequence[date],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for lookback_days in lookbacks:
        for update_day in update_days:
            result_path = _result_path(output_dir, lookback_days, update_day)
            if not result_path.is_file():
                failures.append(
                    {
                        "lookback_days": lookback_days,
                        "update_date": update_day.isoformat(),
                        "reason": "缺少优化结果文件",
                    }
                )
                continue
            try:
                payload = _load_json(result_path)
                outcome = payload.get("out_of_sample")
                training_window = payload.get("training_window")
                if not isinstance(outcome, Mapping):
                    raise ValueError("结果缺少 out_of_sample")
                if not isinstance(training_window, Mapping):
                    raise ValueError("结果缺少 training_window")
                rows.append(
                    {
                        "lookback_days": lookback_days,
                        "update_date": update_day.isoformat(),
                        "training_first_signal_date": training_window.get("first_signal_date", ""),
                        "training_last_signal_date": training_window.get("last_signal_date", ""),
                        "settlement_status": outcome.get("settlement_status", "unknown"),
                        "next_trade_date": outcome.get("next_trade_date", ""),
                        "daily_return_pct": outcome.get("daily_return_pct", ""),
                        "prediction_days": outcome.get("prediction_days", 0),
                        "correct_days": outcome.get("correct_days", 0),
                        "selected_stock_code": outcome.get("selected_stock_code", ""),
                        "best_settings_json": json.dumps(
                            payload.get("best_settings", {}), ensure_ascii=False, sort_keys=True
                        ),
                    }
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                failures.append(
                    {
                        "lookback_days": lookback_days,
                        "update_date": update_day.isoformat(),
                        "reason": str(exc),
                    }
                )
    return rows, failures


def settled_update_days_for_rows(rows: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(row["update_date"])
                for row in rows
                if row.get("settlement_status") == "settled"
            }
        )
    )


def _write_summary_outputs(
    *,
    output_dir: Path,
    lookbacks: Sequence[int],
    update_days: Sequence[date],
    start_date: date,
    end_date: date,
    returns_workbook: Path,
    run_failures: Sequence[Mapping[str, object]] = (),
) -> int:
    daily_rows, read_failures = _daily_rows_from_results(
        output_dir=output_dir,
        lookbacks=lookbacks,
        update_days=update_days,
    )
    failures = list(run_failures) + read_failures
    settled_days = set(
        row["update_date"]
        for row in daily_rows
        if row.get("settlement_status") == "settled"
    )
    expected_settled_days = len(settled_update_days_for_rows(daily_rows))
    ranked = rank_windows(daily_rows)
    complete_ranked = [
        row for row in ranked if int(row["settled_days"]) == expected_settled_days
    ]
    if failures:
        complete_ranked = []

    ranking_rows = [
        {
            "排名": index,
            "回看交易日数": row["lookback_days"],
            "可结算更新日数": row["settled_days"],
            "有效预测日数": row["prediction_days"],
            "正确预测日数": row["correct_days"],
            "正确率（%）": row["accuracy_pct"],
            "总收益率（%）": row["total_return_pct"],
            "平均每日收益率（%）": row["average_daily_return_pct"],
            "算术平均每日收益率（%）": row["arithmetic_average_daily_return_pct"],
            "正收益日数": row["positive_return_days"],
            "负收益日数": row["negative_return_days"],
            "无信号日数": row["no_signal_days"],
        }
        for index, row in enumerate(ranked, start=1)
    ]
    daily_output_rows = [
        {
            "回看交易日数": row["lookback_days"],
            "参数更新日": row["update_date"],
            "训练起始选股日": row["training_first_signal_date"],
            "训练末尾选股日": row["training_last_signal_date"],
            "结算状态": row["settlement_status"],
            "下一交易日": row["next_trade_date"],
            "样本外日收益率（%）": row["daily_return_pct"],
            "有效预测日数": row["prediction_days"],
            "正确预测日数": row["correct_days"],
            "选中股票代码": row["selected_stock_code"],
            "最佳参数JSON": row["best_settings_json"],
        }
        for row in sorted(
            daily_rows, key=lambda row: (int(row["lookback_days"]), str(row["update_date"]))
        )
    ]
    ranking_fields = [
        "排名",
        "回看交易日数",
        "可结算更新日数",
        "有效预测日数",
        "正确预测日数",
        "正确率（%）",
        "总收益率（%）",
        "平均每日收益率（%）",
        "算术平均每日收益率（%）",
        "正收益日数",
        "负收益日数",
        "无信号日数",
    ]
    daily_fields = [
        "回看交易日数",
        "参数更新日",
        "训练起始选股日",
        "训练末尾选股日",
        "结算状态",
        "下一交易日",
        "样本外日收益率（%）",
        "有效预测日数",
        "正确预测日数",
        "选中股票代码",
        "最佳参数JSON",
    ]
    _write_csv(output_dir / "时间窗口前向收益排名.csv", ranking_rows, ranking_fields)
    _write_csv(output_dir / "每日参数与样本外收益.csv", daily_output_rows, daily_fields)
    _write_csv(output_dir / "前三名时间窗口.csv", ranking_rows[:3], ranking_fields)
    _write_csv(
        output_dir / "失败任务.csv",
        failures,
        ["lookback_days", "update_date", "reason"],
    )
    recommendation = {
        "analysis_start_date": start_date.isoformat(),
        "analysis_end_date": end_date.isoformat(),
        "settled_update_days": sorted(settled_days),
        "expected_settled_day_count": expected_settled_days,
        "recommended_lookback_days": (
            complete_ranked[0]["lookback_days"] if complete_ranked else None
        ),
        "top_three_lookbacks": [row["lookback_days"] for row in complete_ranked[:3]],
        "ranking_basis": "完整可结算日期上的复利平均每日收益率；无信号日按0%收益计入。",
        "returns_workbook": str(returns_workbook.resolve()),
        "failed_run_count": len(failures),
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
    }
    _write_json(output_dir / "每日参数窗口建议.json", recommendation)
    return 1 if failures or len(complete_ranked) != len(lookbacks) else 0


def run_analysis(
    *,
    start_date: date,
    end_date: date,
    lookbacks: Sequence[int],
    returns_workbook: Path,
    stock_pool: Path,
    output_dir: Path,
    parallel_workers: int,
    summarize_only: bool = False,
) -> int:
    if not returns_workbook.is_file():
        raise FileNotFoundError(f"未找到收益工作簿：{returns_workbook}")
    if not stock_pool.is_file():
        raise FileNotFoundError(f"未找到股票池：{stock_pool}")
    if parallel_workers <= 0:
        raise ValueError("并行工作数必须为正整数。")
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期。")

    output_dir.mkdir(parents=True, exist_ok=True)
    full_return_data = optimizer.core.load_strict_next_day_returns(returns_workbook)
    update_days = _update_days(
        full_return_data.next_trade_dates,
        start_date=start_date,
        end_date=end_date,
    )
    if not update_days:
        raise optimizer.core.BacktestDataError("指定区间没有可用交易日。")
    if summarize_only:
        return _write_summary_outputs(
            output_dir=output_dir,
            lookbacks=lookbacks,
            update_days=update_days,
            start_date=start_date,
            end_date=end_date,
            returns_workbook=returns_workbook,
        )

    worker_count = min(parallel_workers, len(lookbacks))
    worker_payload = {
        "output_dir": str(output_dir.resolve()),
        "returns_workbook": str(returns_workbook.resolve()),
        "stock_pool": str(stock_pool.resolve()),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "maximum_lookback_days": max(lookbacks),
    }
    print("预热共享历史与因子缓存。", flush=True)
    _prepare_shared_context(
        start_date=start_date,
        end_date=end_date,
        maximum_lookback_days=max(lookbacks),
        returns_workbook=returns_workbook,
        stock_pool=stock_pool,
    )

    batches = [list(lookbacks[index::worker_count]) for index in range(worker_count)]
    failures: list[dict[str, object]] = []
    if worker_count == 1:
        failures.extend(_worker_entry(dict(worker_payload, lookbacks=batches[0])))
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(_worker_entry, dict(worker_payload, lookbacks=batch))
                for batch in batches
                if batch
            ]
            for future in as_completed(futures):
                failures.extend(future.result())
    return _write_summary_outputs(
        output_dir=output_dir,
        lookbacks=lookbacks,
        update_days=update_days,
        start_date=start_date,
        end_date=end_date,
        returns_workbook=returns_workbook,
        run_failures=failures,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="前向评估每日参数更新的5至30个交易日窗口。"
    )
    parser.add_argument("--start-date", type=_as_date, default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", type=_as_date, default=DEFAULT_END_DATE)
    parser.add_argument("--lookback-start", type=int, default=DEFAULT_LOOKBACK_START)
    parser.add_argument("--lookback-end", type=int, default=DEFAULT_LOOKBACK_END)
    parser.add_argument("--returns-workbook", type=Path, default=DEFAULT_RETURNS_WORKBOOK)
    parser.add_argument("--stock-pool", type=Path, default=DEFAULT_STOCK_POOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--parallel-workers", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lookbacks = lookback_windows(args.lookback_start, args.lookback_end)
    if args.dry_run:
        return_data = optimizer.core.load_strict_next_day_returns(args.returns_workbook)
        update_days = _update_days(
            return_data.next_trade_dates,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        for update_day in update_days:
            status = (
                "settled" if update_day in return_data.next_trade_dates else "awaiting_settlement"
            )
            print(f"update_date={update_day.isoformat()} status={status}")
        for lookback_days in lookbacks:
            print(f"lookback_days={lookback_days}")
        return 0
    return run_analysis(
        start_date=args.start_date,
        end_date=args.end_date,
        lookbacks=lookbacks,
        returns_workbook=args.returns_workbook,
        stock_pool=args.stock_pool,
        output_dir=args.output_dir,
        parallel_workers=args.parallel_workers,
        summarize_only=args.summarize_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
