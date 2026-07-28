"""预热深市主板选股应用的最近 120 个交易日行情缓存。

运行示例：

    uv run --locked python warm_szse_quant_cache.py --as-of-date 2026-07-25

该脚本将数据写入 szse_quant_app.py 实际读取的 data_cache/szse_quant 目录。
默认直接使用腾讯增强前复权日 K，避免当前不可用的东方财富接口在预热时反复超时。
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import pandas as pd

import szse_quant_app as app


@dataclass(frozen=True)
class WarmCacheOutcome:
    """单只股票的预热结果。"""

    code: str
    from_cache: bool
    error: str | None = None


def parse_iso_date(value: str) -> date:
    """解析命令行传入的截至日期。"""

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD 格式。") from exc


def _latest_liquidity_is_complete(history: pd.DataFrame) -> bool:
    """确认最新实际交易日拥有成交额和换手率，避免写入不可复用的缓存。"""

    if history.empty:
        return False
    latest = history.iloc[-1]
    return pd.notna(latest["amount"]) and pd.notna(latest["turnover"])


def _fetch_tencent_history(
    code: str,
    *,
    limiter: app.RequestRateLimiter,
    rich_breaker: app.SourceCircuitBreaker,
    legacy_breaker: app.SourceCircuitBreaker,
    as_of_date: date | datetime,
) -> tuple[pd.DataFrame, str]:
    """优先抓取腾讯增强日线，必要时回退旧接口。"""

    rich_failure: app.FetchFailure | None = None
    try:
        if not rich_breaker.allow_request():
            raise rich_breaker.unavailable_error()
        history = app._fetch_tencent_rich_history(code, limiter, as_of_date=as_of_date)
        rich_breaker.record_success()
        return history, "腾讯增强前复权日 K（缓存预热）"
    except app.FetchFailure as exc:
        rich_failure = exc
        if rich_failure.service_unavailable:
            rich_breaker.record_service_failure()

    try:
        if not legacy_breaker.allow_request():
            raise legacy_breaker.unavailable_error()
        history = app._fetch_tencent_legacy_history(code, limiter, as_of_date=as_of_date)
        legacy_breaker.record_success()
        return history, "腾讯前复权日 K 回退（缓存预热）"
    except app.FetchFailure as legacy_error:
        if legacy_error.service_unavailable:
            legacy_breaker.record_service_failure()
        raise app.FetchFailure(
            f"腾讯增强日 K 失败：{rich_failure}；腾讯旧版日 K 失败：{legacy_error}",
            service_unavailable=(
                rich_failure is not None
                and rich_failure.service_unavailable
                and legacy_error.service_unavailable
            ),
        ) from legacy_error


def warm_one_cache(
    code: str,
    *,
    cache_hours: float,
    force_refresh: bool,
    limiter: app.RequestRateLimiter,
    rich_breaker: app.SourceCircuitBreaker,
    legacy_breaker: app.SourceCircuitBreaker,
    as_of_date: date | datetime,
) -> WarmCacheOutcome:
    """预热单只股票，并保证写出的记录能被主应用直接命中。"""

    target_day = app._as_of_day(as_of_date)
    if not force_refresh:
        cached = app._read_fresh_cache(code, cache_hours, as_of_date=target_day)
        if cached is not None and cached.history is not None:
            if cached.factors is None:
                try:
                    # 日线缓存仍有效时，仅迁移失效的因子缓存，避免重复请求公开行情。
                    factors = app.calculate_factors(cached.history, as_of_date=target_day)
                    app._write_cache_record(
                        code,
                        source=cached.source,
                        history=cached.history,
                        error=None,
                        factors=factors,
                        as_of_date=target_day,
                    )
                except (OSError, ValueError, TypeError) as exc:
                    return WarmCacheOutcome(code=code, from_cache=False, error=str(exc))
            return WarmCacheOutcome(code=code, from_cache=True)

    try:
        history, source = _fetch_tencent_history(
            code,
            limiter=limiter,
            rich_breaker=rich_breaker,
            legacy_breaker=legacy_breaker,
            as_of_date=target_day,
        )
        history = app._limit_history_to_as_of_date(history, target_day)
        if len(history) < app.MIN_REQUIRED_BARS:
            raise ValueError(f"有效日线不足 {app.MIN_REQUIRED_BARS} 根。")
        if not _latest_liquidity_is_complete(history):
            raise ValueError("最新交易日缺少成交额或换手率，未写入不可复用缓存。")

        factors = app.calculate_factors(history, as_of_date=target_day)
        app._write_cache_record(
            code,
            source=source,
            history=history,
            error=None,
            factors=factors,
            as_of_date=target_day,
        )
        return WarmCacheOutcome(code=code, from_cache=False)
    except (OSError, ValueError, TypeError, app.FetchFailure) as exc:
        return WarmCacheOutcome(code=code, from_cache=False, error=str(exc))


def warm_cache(
    companies: pd.DataFrame,
    *,
    max_companies: int | None,
    cache_hours: float,
    force_refresh: bool,
    workers: int,
    request_interval_seconds: float,
    as_of_date: date | datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """并发预热股票池，并返回成功、失败和缓存命中统计。"""

    if max_companies is not None and max_companies <= 0:
        raise ValueError("--max-companies 必须为正数。")
    if not 1 <= workers <= app.MAX_WORKERS:
        raise ValueError(f"--workers 必须在 1 到 {app.MAX_WORKERS} 之间。")
    if request_interval_seconds < 0:
        raise ValueError("--interval 不能为负数。")
    if cache_hours < 0:
        raise ValueError("--cache-hours 不能为负数。")

    selected_companies = companies if max_companies is None else companies.head(max_companies)
    records = selected_companies.to_dict("records")
    limiter = app.RequestRateLimiter(request_interval_seconds)
    rich_breaker = app.SourceCircuitBreaker("腾讯增强日 K")
    legacy_breaker = app.SourceCircuitBreaker("腾讯旧版日 K")
    success_rows: list[dict[str, object]] = []
    error_rows: list[dict[str, object]] = []
    completed = 0
    cache_hits = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                warm_one_cache,
                str(record["股票代码"]),
                cache_hours=cache_hours,
                force_refresh=force_refresh,
                limiter=limiter,
                rich_breaker=rich_breaker,
                legacy_breaker=legacy_breaker,
                as_of_date=as_of_date,
            ): record
            for record in records
        }
        for future in as_completed(futures):
            company = futures[future]
            completed += 1
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = WarmCacheOutcome(
                    code=str(company["股票代码"]),
                    from_cache=False,
                    error=f"工作线程异常：{exc}",
                )

            if outcome.error is None:
                cache_hits += int(outcome.from_cache)
                success_rows.append(
                    {
                        "序号": company["序号"],
                        "股票代码": outcome.code,
                        "股票名称": company["股票名称"],
                        "缓存命中": outcome.from_cache,
                    }
                )
            else:
                error_rows.append(
                    {
                        "序号": company["序号"],
                        "股票代码": outcome.code,
                        "股票名称": company["股票名称"],
                        "失败原因": outcome.error,
                    }
                )

            if completed == len(records) or completed % 25 == 0:
                print(
                    f"已预热 {completed}/{len(records)}；成功 {len(success_rows)}；"
                    f"失败 {len(error_rows)}；缓存命中 {cache_hits}",
                    flush=True,
                )

    successes = pd.DataFrame(success_rows)
    errors = pd.DataFrame(error_rows)
    if not successes.empty:
        successes = successes.sort_values("序号", kind="stable").reset_index(drop=True)
    if not errors.empty:
        errors = errors.sort_values("序号", kind="stable").reset_index(drop=True)
    return successes, errors, {
        "总数": len(records),
        "成功": len(successes),
        "失败": len(errors),
        "缓存命中": cache_hits,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """定义并解析预热命令行参数。"""

    parser = argparse.ArgumentParser(
        description="预热深市主板选股应用所需的最近 120 个交易日行情缓存。"
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=app.DEFAULT_WORKBOOK_PATH,
        help="股票池 Excel 路径（默认：深交所数据.xlsx）。",
    )
    parser.add_argument(
        "--as-of-date",
        type=parse_iso_date,
        default=date.today(),
        help="缓存截至日期（默认：今天）。",
    )
    parser.add_argument("--max-companies", type=int, help="仅预热前 N 只股票，用于快速验证。")
    parser.add_argument(
        "--cache-hours",
        type=float,
        default=app.DEFAULT_CACHE_HOURS,
        help="复用已有缓存的有效小时数（默认：12）。",
    )
    parser.add_argument("--force-refresh", action="store_true", help="忽略已有缓存并重新下载。")
    parser.add_argument(
        "--workers",
        type=int,
        default=app.DEFAULT_WORKERS,
        help=f"并发请求数（默认：{app.DEFAULT_WORKERS}，最大：{app.MAX_WORKERS}）。",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=app.DEFAULT_REQUEST_INTERVAL_SECONDS,
        help="所有请求共享的最小间隔秒数（默认：0.25）。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行预热并输出缓存目录与统计。"""

    args = parse_args(argv)
    if args.max_companies is not None and args.max_companies <= 0:
        raise ValueError("--max-companies 必须为正数。")
    if not 1 <= args.workers <= app.MAX_WORKERS:
        raise ValueError(f"--workers 必须在 1 到 {app.MAX_WORKERS} 之间。")
    if args.interval < 0:
        raise ValueError("--interval 不能为负数。")
    if args.cache_hours < 0:
        raise ValueError("--cache-hours 不能为负数。")

    target_day = app._as_of_day(args.as_of_date)
    companies = app.load_mainboard_companies(args.workbook)
    company_count = len(companies) if args.max_companies is None else min(len(companies), args.max_companies)
    print(
        f"正在预热截至 {target_day} 的最近 {app.INDICATOR_WARMUP_BARS} 个交易日数据，"
        f"股票数：{company_count}。"
    )
    _, errors, summary = warm_cache(
        companies,
        max_companies=args.max_companies,
        cache_hours=args.cache_hours,
        force_refresh=args.force_refresh,
        workers=args.workers,
        request_interval_seconds=args.interval,
        as_of_date=target_day,
    )
    cache_path = app.CACHE_DIR / target_day.isoformat()
    print(f"缓存目录：{cache_path}")
    print(
        f"统计：总数={summary['总数']}，成功={summary['成功']}，失败={summary['失败']}，"
        f"缓存命中={summary['缓存命中']}"
    )
    failure_path = cache_path / "预热失败明细.csv"
    if not errors.empty:
        cache_path.mkdir(parents=True, exist_ok=True)
        errors.to_csv(failure_path, index=False, encoding="utf-8-sig")
        print(f"失败明细：{failure_path}", file=sys.stderr)
    elif failure_path.is_file():
        failure_path.unlink()
    return 0 if summary["成功"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
