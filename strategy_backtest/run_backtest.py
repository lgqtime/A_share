"""运行深市主板固定策略的严格下一交易日回测。

示例：

    uv run --locked python strategy_backtest/run_backtest.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

import pandas as pd

try:  # 支持直接执行和包导入两种方式。
    from . import backtest_core as core
    from . import szse_quant_app as strategy_app
except ImportError:  # pragma: no cover - `python strategy_backtest/run_backtest.py`。
    import backtest_core as core
    import szse_quant_app as strategy_app


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RETURNS_WORKBOOK = (
    PROJECT_DIR
    / "strategy_backtest"
    / "outputs"
    / "input_data"
    / "深市主板每日涨跌幅_2025-10-29_2026-07-27.xlsx"
)
DEFAULT_STOCK_POOL = PROJECT_DIR / "深交所数据.xlsx"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """定义回测命令行参数。"""

    parser = argparse.ArgumentParser(
        description="使用固定选股参数，统计严格下一交易日收益。"
    )
    parser.add_argument(
        "--returns-workbook",
        type=Path,
        default=DEFAULT_RETURNS_WORKBOOK,
        help="含“每日涨跌幅明细”工作表的收益文件。",
    )
    parser.add_argument(
        "--stock-pool",
        type=Path,
        default=DEFAULT_STOCK_POOL,
        help="含“主板公司”工作表的股票池文件。",
    )
    parser.add_argument("--output", type=Path, help="输出 Excel 报表路径。")
    parser.add_argument(
        "--max-companies",
        type=int,
        help="仅处理前 N 只股票，用于快速验证。",
    )
    parser.add_argument(
        "--cache-hours",
        type=float,
        default=core.DEFAULT_CACHE_HOURS,
        help="长历史和精算因子缓存有效小时数（默认 720）。设为 0 禁用缓存。",
    )
    parser.add_argument("--force-refresh", action="store_true", help="忽略长历史缓存并重新联网。")
    parser.add_argument(
        "--workers",
        type=int,
        default=core.DEFAULT_WORKERS,
        help=f"并发请求数（默认 {core.DEFAULT_WORKERS}，最大 {core.MAX_WORKERS}）。",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=core.DEFAULT_REQUEST_INTERVAL_SECONDS,
        help="全部线程共享的最小请求间隔秒数（默认 0.25）。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=core.DEFAULT_TIMEOUT_SECONDS,
        help="单次网络请求超时秒数（默认 15）。",
    )
    return parser.parse_args(argv)


def _progress(completed: int, total: int, code: str, cache_hits: int, succeeded: int, failed: int) -> None:
    """在首次下载较慢时持续输出可读的进度。"""

    if completed == total or completed % 10 == 0:
        print(
            f"历史行情：{completed}/{total}，当前 {code}；"
            f"缓存命中 {cache_hits}，成功 {succeeded}，失败 {failed}",
            flush=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """执行回测并输出用户要求的最终统计。"""

    args = parse_args(argv)
    if args.max_companies is not None and args.max_companies <= 0:
        raise ValueError("--max-companies 必须为正数。")
    if args.cache_hours < 0:
        raise ValueError("--cache-hours 不能为负数。")
    if not 1 <= args.workers <= core.MAX_WORKERS:
        raise ValueError(f"--workers 必须在 1 至 {core.MAX_WORKERS} 之间。")
    if args.interval < 0:
        raise ValueError("--interval 不能为负数。")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须为正数。")

    return_data = core.load_strict_next_day_returns(args.returns_workbook)
    companies = strategy_app.load_mainboard_companies(args.stock_pool)
    if args.max_companies is not None:
        companies = companies.head(args.max_companies).copy()
    if companies.empty:
        raise core.BacktestDataError("股票池为空。")

    first_signal_date = return_data.signal_dates[0]
    last_signal_date = return_data.signal_dates[-1]
    last_market_date = return_data.next_trade_dates[last_signal_date]
    print(
        f"回测区间：{first_signal_date.isoformat()} 至 {last_signal_date.isoformat()}；"
        f"共 {len(return_data.signal_dates)} 个选股日，股票池 {len(companies)} 只。",
        flush=True,
    )
    print(
        "策略：按基础条件筛选并执行风险过滤，每个选股日仅持有得分最高的一只。",
        flush=True,
    )

    histories, history_errors, history_summary, cache_key = core.collect_full_histories(
        companies,
        first_signal_date=first_signal_date,
        end_date=last_market_date,
        cache_hours=float(args.cache_hours),
        force_refresh=bool(args.force_refresh),
        workers=int(args.workers),
        request_interval_seconds=float(args.interval),
        timeout_seconds=float(args.timeout),
        progress_callback=_progress,
    )
    history_success_count = int(history_summary.get("历史成功", 0))
    history_failed_count = int(
        history_summary.get("历史获取失败股票数", history_summary.get("历史失败", 0))
    )
    history_warmup_insufficient_count = int(
        history_summary.get("历史预热不足股票数", 0)
    )
    history_excluded_count = int(
        history_summary.get(
            "历史自动剔除股票数",
            history_failed_count + history_warmup_insufficient_count,
        )
    )
    history_valid_count = int(
        history_summary.get(
            "历史有效股票数",
            history_success_count - history_warmup_insufficient_count,
        )
    )
    print(
        f"历史完成：成功 {history_success_count}，真实失败 {history_failed_count}，"
        f"预热不足 {history_warmup_insufficient_count}，自动剔除 {history_excluded_count}，"
        f"有效 {history_valid_count}，缓存命中 {history_summary.get('历史缓存命中', 0)}。开始本地精算因子。",
        flush=True,
    )
    factors_by_day, day_stats, factor_errors = core.collect_factor_rows_by_day(
        companies,
        histories,
        return_data.signal_dates,
        cache_key=cache_key,
        cache_hours=float(args.cache_hours),
    )
    daily_results, summary = core.evaluate_fixed_strategy(
        return_data,
        factors_by_day,
        day_stats,
    )
    actual_factor_error_count = int(
        factor_errors["问题类型"].isin({"精算因子失败", "精算因子日期不匹配"}).sum()
    )
    summary = {
        **summary,
        "历史成功股票数": history_success_count,
        "历史失败股票数": history_failed_count,
        "历史预热不足股票数": history_warmup_insufficient_count,
        "历史缓存命中数": history_summary.get("历史缓存命中", 0),
        "精算因子错误数": actual_factor_error_count,
        "策略预热不足候选数": int(
            sum(stats.get("策略预热不足候选数", 0) for stats in day_stats.values())
        ),
    }
    summary.update(
        {
            "历史获取失败股票数": history_failed_count,
            "历史自动剔除股票数": history_excluded_count,
            "历史有效股票数": history_valid_count,
        }
    )
    missing_return_problems = daily_results.loc[
        daily_results["状态"].eq("次日收益缺失"),
        ["选股日期", "选中股票代码", "选中股票名称", "说明"],
    ].copy()
    if not missing_return_problems.empty:
        missing_return_problems.insert(0, "序号", pd.NA)
        missing_return_problems = missing_return_problems.rename(
            columns={
                "选中股票代码": "股票代码",
                "选中股票名称": "股票名称",
                "说明": "失败原因",
            }
        )
        missing_return_problems.insert(4, "问题类型", "严格次日收益缺失")
    data_problems = pd.concat(
        [history_errors, factor_errors, missing_return_problems],
        ignore_index=True,
    )
    if not data_problems.empty:
        data_problems = data_problems.sort_values(
            ["选股日期", "序号", "问题类型"],
            kind="stable",
            na_position="last",
        ).reset_index(drop=True)
    output_path = args.output or core.default_output_path(first_signal_date, last_signal_date)
    core.write_backtest_workbook(daily_results, summary, data_problems, output_path)

    print(f"最终统计：{summary['最终统计']}", flush=True)
    print(f"回测报表：{output_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, core.BacktestDataError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
