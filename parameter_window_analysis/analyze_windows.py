"""Compare rolling-optimizer results across 5-30 signal-day windows.

All generated files stay beneath this directory so the production daily
parameter files and existing strategy code are left untouched.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


ANALYSIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ANALYSIS_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from strategy_backtest import rolling_parameter_optimizer as optimizer


DEFAULT_AS_OF_DATE = date(2026, 8, 6)
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
DEFAULT_OUTPUT_DIR = ANALYSIS_DIR / "results"


def _as_date(value: str) -> date:
    return date.fromisoformat(value)


def lookback_windows(start: int, end: int) -> list[int]:
    if start < end or end <= 0:
        raise ValueError("窗口范围必须是从较大正整数递减到较小正整数。")
    return list(range(start, end - 1, -1))


def geometric_daily_return_pct(total_return_pct: object, day_count: object) -> float | None:
    """Convert a cumulative percent return to a compounded daily percent return."""

    try:
        total_return = float(total_return_pct)
        days = int(day_count)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(total_return) or days <= 0 or total_return < -100.0:
        return None
    return (math.pow(1.0 + total_return / 100.0, 1.0 / days) - 1.0) * 100.0


def _write_csv(path: Path, rows: Iterable[Mapping[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _load_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"优化结果不是 JSON 对象：{path}")
    return payload


def _summary_row(lookback_days: int, payload: Mapping[str, Any]) -> dict[str, object]:
    result = payload.get("best_result")
    data_window = payload.get("data_window")
    best_settings = payload.get("best_settings")
    if not isinstance(result, Mapping):
        raise ValueError("优化结果缺少 best_result。")
    if not isinstance(data_window, Mapping):
        raise ValueError("优化结果缺少 data_window。")
    if not isinstance(best_settings, Mapping):
        raise ValueError("优化结果缺少 best_settings。")

    window_days = int(data_window.get("lookback_signal_days", lookback_days))
    total_return_pct = result.get("total_return_pct")
    prediction_days = result.get("prediction_days")
    return {
        "回看交易日数": lookback_days,
        "实际窗口交易日数": window_days,
        "起始选股日": data_window.get("first_signal_date", ""),
        "末尾选股日": data_window.get("last_signal_date", ""),
        "验证截止日": data_window.get("last_verification_date", ""),
        "有效预测天数": prediction_days,
        "正确预测天数": result.get("correct_days", ""),
        "正确率（%）": result.get("accuracy_pct", ""),
        "总收益率（%）": total_return_pct,
        "平均每日收益率（%）": geometric_daily_return_pct(total_return_pct, window_days),
        "有效预测日平均收益率（%）": geometric_daily_return_pct(
            total_return_pct, prediction_days
        ),
        "最佳参数JSON": json.dumps(best_settings, ensure_ascii=False, sort_keys=True),
    }


def _parameter_rows(lookback_days: int, payload: Mapping[str, Any]) -> list[dict[str, object]]:
    best_settings = payload.get("best_settings")
    if not isinstance(best_settings, Mapping):
        raise ValueError("优化结果缺少 best_settings。")
    rows: list[dict[str, object]] = []
    for key, value in sorted(best_settings.items()):
        lower = upper = ""
        if isinstance(value, (list, tuple)) and len(value) == 2:
            lower, upper = value
        rows.append(
            {
                "回看交易日数": lookback_days,
                "参数": key,
                "下限": lower,
                "上限": upper,
            }
        )
    return rows


def _result_path(run_directory: Path, as_of_date: date) -> Path:
    return run_directory / f"{optimizer.REPORT_PREFIX}{as_of_date.isoformat()}.json"


def _prepare_shared_context(
    *,
    as_of_date: date,
    maximum_lookback_days: int,
    returns_workbook: Path,
    stock_pool: Path,
) -> dict[str, object]:
    """Prepare the costly history and factor inputs once for all windows."""

    full_return_data = optimizer.core.load_strict_next_day_returns(returns_workbook)
    maximum_return_data = optimizer.select_recent_signal_dates(
        full_return_data,
        as_of_date=as_of_date,
        lookback_days=maximum_lookback_days,
    )
    companies = optimizer.strategy_app.load_mainboard_companies(stock_pool)
    original_company_count = len(companies)
    companies = optimizer._exclude_return_failure_companies(
        companies, maximum_return_data.failed_return_codes
    )
    if companies.empty:
        raise optimizer.core.BacktestDataError("收益文件失败明细已剔除全部股票池。")

    first_signal_day = full_return_data.signal_dates[0]
    last_market_day = full_return_data.next_trade_dates[full_return_data.signal_dates[-1]]
    histories, history_errors, history_summary, cache_key = optimizer.core.collect_full_histories(
        companies,
        first_signal_date=first_signal_day,
        end_date=last_market_day,
        cache_hours=optimizer.core.DEFAULT_CACHE_HOURS,
        force_refresh=False,
        workers=optimizer.core.DEFAULT_WORKERS,
        request_interval_seconds=optimizer.core.DEFAULT_REQUEST_INTERVAL_SECONDS,
        timeout_seconds=optimizer.core.DEFAULT_TIMEOUT_SECONDS,
        progress_callback=optimizer._progress("历史行情"),
    )
    factors_by_day, day_stats, factor_errors = optimizer.core.collect_all_factor_rows_by_day(
        companies,
        histories,
        maximum_return_data.signal_dates,
        cache_key=cache_key,
        cache_hours=optimizer.core.DEFAULT_CACHE_HOURS,
        factor_workers=optimizer.FACTOR_WORKERS,
        progress_callback=optimizer._progress("因子计算"),
    )
    return {
        "full_return_data": full_return_data,
        "companies": companies,
        "original_company_count": original_company_count,
        "history_errors": history_errors,
        "history_summary": history_summary,
        "factors_by_day": factors_by_day,
        "day_stats": day_stats,
        "factor_errors": factor_errors,
    }


def _optimize_window(
    *,
    lookback_days: int,
    as_of_date: date,
    returns_workbook: Path,
    output_dir: Path,
    context: Mapping[str, object],
) -> Mapping[str, Any]:
    full_return_data = context["full_return_data"]
    if not isinstance(full_return_data, optimizer.core.ReturnData):
        raise ValueError("共享收益数据无效。")
    return_data = optimizer.select_recent_signal_dates(
        full_return_data,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
    )
    all_factors_by_day = context["factors_by_day"]
    all_day_stats = context["day_stats"]
    if not isinstance(all_factors_by_day, Mapping) or not isinstance(all_day_stats, Mapping):
        raise ValueError("共享因子数据无效。")
    factors_by_day = {
        signal_day: all_factors_by_day.get(signal_day, ())
        for signal_day in return_data.signal_dates
    }
    day_stats = {
        signal_day: all_day_stats.get(signal_day, {})
        for signal_day in return_data.signal_dates
    }

    starter_settings = optimizer.strategy_app.default_screening_settings()
    starter_settings.update(optimizer.normalize_tunable_settings(starter_settings))
    selected, selected_risks = optimizer._selected_and_risks(starter_settings)
    minimum_prediction_days = max(3, math.ceil(len(return_data.signal_dates) * 0.2))
    (
        best_full_settings,
        baseline_result,
        final_result,
        daily_results,
        final_summary,
        trace,
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

    history_errors = context["history_errors"]
    factor_errors = context["factor_errors"]
    data_problems = optimizer._merge_problem_frames((history_errors, factor_errors))
    companies = context["companies"]
    original_company_count = context["original_company_count"]
    if not hasattr(companies, "__len__"):
        raise ValueError("共享股票池无效。")
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
        "starter_source": "程序默认值（独立窗口分析）",
        "initial_settings": optimizer.normalize_tunable_settings(starter_settings),
        "screening_settings": best_full_settings,
        "fixed_selected_conditions": selected,
        "fixed_risk_filters": selected_risks,
        "minimum_prediction_days": minimum_prediction_days,
        "history_summary": dict(context["history_summary"]),
        "factor_error_count": int(len(factor_errors)),
        "factor_row_count": int(sum(len(rows) for rows in factors_by_day.values())),
        "stock_pool_rows_before_exclusion": original_company_count,
        "stock_pool_rows_used": len(companies),
        "return_failure_stocks_excluded": len(return_data.failed_return_codes),
        "backtest_summary": final_summary,
    }
    optimizer.persist_optimization_result(
        final_result,
        output_dir,
        as_of_date=as_of_date,
        daily_results=daily_results,
        baseline_result=baseline_result,
        trace=trace,
        metadata=metadata,
        data_problems=data_problems,
    )
    return _load_json(_result_path(output_dir, as_of_date))


def run_analysis(
    *,
    as_of_date: date,
    lookbacks: Iterable[int],
    returns_workbook: Path,
    stock_pool: Path,
    output_dir: Path,
    summarize_only: bool = False,
) -> int:
    if not summarize_only and not returns_workbook.is_file():
        raise FileNotFoundError(f"未找到收益工作簿：{returns_workbook}")
    if not summarize_only and not stock_pool.is_file():
        raise FileNotFoundError(f"未找到股票池：{stock_pool}")

    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, object]] = []
    parameter_rows: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    shared_context: dict[str, object] | None = None

    for lookback_days in lookbacks:
        run_directory = output_dir / "optimization_runs" / f"lookback_{lookback_days:02d}_days"
        try:
            result_path = _result_path(run_directory, as_of_date)
            if result_path.is_file():
                payload = _load_json(result_path)
                summaries.append(_summary_row(lookback_days, payload))
                parameter_rows.extend(_parameter_rows(lookback_days, payload))
                print(f"复用已完成窗口：lookback_days={lookback_days}", flush=True)
                continue
            if summarize_only:
                raise FileNotFoundError(f"缺少已完成窗口结果：{result_path}")

            print(f"开始优化：lookback_days={lookback_days}", flush=True)
            if shared_context is None:
                shared_context = _prepare_shared_context(
                    as_of_date=as_of_date,
                    maximum_lookback_days=max(lookbacks),
                    returns_workbook=returns_workbook,
                    stock_pool=stock_pool,
                )
            payload = _optimize_window(
                lookback_days=lookback_days,
                as_of_date=as_of_date,
                returns_workbook=returns_workbook,
                output_dir=run_directory,
                context=shared_context,
            )
            summaries.append(_summary_row(lookback_days, payload))
            parameter_rows.extend(_parameter_rows(lookback_days, payload))
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append({"回看交易日数": lookback_days, "失败原因": str(exc)})
            print(f"窗口 {lookback_days} 失败：{exc}", file=sys.stderr, flush=True)

    summaries.sort(key=lambda row: int(row["回看交易日数"]), reverse=True)
    parameter_rows.sort(key=lambda row: (int(row["回看交易日数"]), str(row["参数"])), reverse=True)
    _write_csv(
        output_dir / "窗口收益汇总.csv",
        summaries,
        [
            "回看交易日数",
            "实际窗口交易日数",
            "起始选股日",
            "末尾选股日",
            "验证截止日",
            "有效预测天数",
            "正确预测天数",
            "正确率（%）",
            "总收益率（%）",
            "平均每日收益率（%）",
            "有效预测日平均收益率（%）",
            "最佳参数JSON",
        ],
    )
    _write_csv(
        output_dir / "最佳参数明细.csv",
        parameter_rows,
        ["回看交易日数", "参数", "下限", "上限"],
    )
    _write_csv(
        output_dir / "失败窗口.csv",
        failures,
        ["回看交易日数", "失败原因"],
    )
    _write_json(
        output_dir / "运行清单.json",
        {
            "as_of_date": as_of_date.isoformat(),
            "lookback_days": list(lookbacks),
            "returns_workbook": str(returns_workbook.resolve()),
            "stock_pool": str(stock_pool.resolve()),
            "completed_windows": [row["回看交易日数"] for row in summaries],
            "failed_windows": failures,
            "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        },
    )
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="比较 2026-08-06 前不同交易日窗口的滚动最优参数。"
    )
    parser.add_argument("--as-of-date", type=_as_date, default=DEFAULT_AS_OF_DATE)
    parser.add_argument("--lookback-start", type=int, default=DEFAULT_LOOKBACK_START)
    parser.add_argument("--lookback-end", type=int, default=DEFAULT_LOOKBACK_END)
    parser.add_argument("--returns-workbook", type=Path, default=DEFAULT_RETURNS_WORKBOOK)
    parser.add_argument("--stock-pool", type=Path, default=DEFAULT_STOCK_POOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lookbacks = lookback_windows(args.lookback_start, args.lookback_end)
    if args.dry_run:
        for lookback_days in lookbacks:
            print(f"lookback_days={lookback_days}")
        return 0
    return run_analysis(
        as_of_date=args.as_of_date,
        lookbacks=lookbacks,
        returns_workbook=args.returns_workbook,
        stock_pool=args.stock_pool,
        output_dir=args.output_dir,
        summarize_only=args.summarize_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
