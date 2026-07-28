"""预热交互回测页所需的全量历史因子缓存。

示例：
    uv run --locked python strategy_backtest/warm_factor_cache.py

脚本优先读取 ``strategy_backtest/data_cache/long_history`` 中已经下载的长历史，
只为缺失的股票联网。因子缓存按股票和选股日期保存，浏览器回测页可直接复用。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

try:  # 支持包模块和脚本直接执行两种方式。
    from . import backtest_core as core
    from . import szse_quant_app as strategy_app
except ImportError:  # pragma: no cover - 直接运行脚本时使用。
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
    """解析预热所需的运行参数。"""

    parser = argparse.ArgumentParser(description="预热深市主板交互回测的全量因子缓存。")
    parser.add_argument("--returns-workbook", type=Path, default=DEFAULT_RETURNS_WORKBOOK)
    parser.add_argument("--stock-pool", type=Path, default=DEFAULT_STOCK_POOL)
    parser.add_argument("--max-companies", type=int, help="仅处理前 N 只股票，用于快速验证。")
    parser.add_argument("--cache-hours", type=float, default=core.DEFAULT_CACHE_HOURS)
    parser.add_argument("--force-refresh", action="store_true", help="忽略已有长历史缓存。")
    parser.add_argument("--workers", type=int, default=core.DEFAULT_WORKERS)
    parser.add_argument(
        "--factor-workers",
        type=int,
        default=core.DEFAULT_FACTOR_WORKERS,
        help=f"本地因子计算进程数，范围 1-{core.MAX_FACTOR_WORKERS}。",
    )
    parser.add_argument("--interval", type=float, default=core.DEFAULT_REQUEST_INTERVAL_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """预热完整股票池，并显示可复用缓存的覆盖情况。"""

    args = parse_args(argv)
    if args.max_companies is not None and args.max_companies <= 0:
        raise ValueError("--max-companies 必须为正数。")
    if not 1 <= args.workers <= core.MAX_WORKERS:
        raise ValueError(f"--workers 必须在 1 至 {core.MAX_WORKERS} 之间。")
    if not 1 <= args.factor_workers <= core.MAX_FACTOR_WORKERS:
        raise ValueError(
            f"--factor-workers 必须在 1 至 {core.MAX_FACTOR_WORKERS} 之间。"
        )
    if args.cache_hours < 0:
        raise ValueError("--cache-hours 不能为负数。")
    if args.interval < 0:
        raise ValueError("--interval 不能为负数。")

    return_data = core.load_strict_next_day_returns(args.returns_workbook)
    companies = strategy_app.load_mainboard_companies(args.stock_pool)
    if args.max_companies is not None:
        companies = companies.head(args.max_companies).copy()
    if companies.empty:
        raise core.BacktestDataError("股票池为空。")

    first_signal_day = return_data.signal_dates[0]
    last_signal_day = return_data.signal_dates[-1]
    last_market_day = return_data.next_trade_dates[last_signal_day]
    print(
        f"预热区间：{first_signal_day.isoformat()} 至 {last_signal_day.isoformat()}；"
        f"选股日 {len(return_data.signal_dates)} 个，股票 {len(companies)} 只。",
        flush=True,
    )

    def update_history(
        completed: int,
        total: int,
        code: str,
        cache_hits: int,
        succeeded: int,
        failed: int,
    ) -> None:
        if completed == total or completed % 50 == 0:
            print(
                f"历史行情 {completed}/{total}：{code}；缓存命中 {cache_hits}，"
                f"成功 {succeeded}，失败 {failed}",
                flush=True,
            )

    histories, history_errors, history_summary, cache_key = core.collect_full_histories(
        companies,
        first_signal_date=first_signal_day,
        end_date=last_market_day,
        cache_hours=float(args.cache_hours),
        force_refresh=bool(args.force_refresh),
        workers=int(args.workers),
        request_interval_seconds=float(args.interval),
        timeout_seconds=core.DEFAULT_TIMEOUT_SECONDS,
        progress_callback=update_history,
    )
    if int(history_summary.get("历史失败", 0)):
        raise core.BacktestDataError(
            f"有 {history_summary['历史失败']} 只股票未获得完整长历史，未继续预热因子；"
            "成功下载的历史已保留，可稍后重试。"
        )

    def update_factors(
        completed: int,
        total: int,
        code: str,
        cache_hits: int,
        succeeded: int,
        failed: int,
    ) -> None:
        if completed == total or completed % 50 == 0:
            print(
                f"全量因子 {completed}/{total}：{code}；缓存命中 {cache_hits}，"
                f"成功 {succeeded}，失败 {failed}",
                flush=True,
            )

    factors_by_day, day_stats, factor_errors = core.collect_all_factor_rows_by_day(
        companies,
        histories,
        return_data.signal_dates,
        cache_key=cache_key,
        cache_hours=float(args.cache_hours),
        progress_callback=update_factors,
        factor_workers=int(args.factor_workers),
    )
    factor_rows = sum(len(rows) for rows in factors_by_day.values())
    cache_hits = sum(stats["因子缓存命中数"] for stats in day_stats.values())
    print(
        f"预热完成：因子行 {factor_rows}，缓存命中 {cache_hits}，"
        f"因子问题 {len(factor_errors)}。",
        flush=True,
    )
    if not history_errors.empty:
        print(f"历史问题 {len(history_errors)} 条。", file=sys.stderr)
    if not factor_errors.empty:
        print(f"因子问题明细 {len(factor_errors)} 条。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, core.BacktestDataError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
