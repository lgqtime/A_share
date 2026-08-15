"""Run the rolling optimizer for a range of lookback-day values."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Mapping, Sequence

from strategy_backtest import backtest_core as core
from strategy_backtest import rolling_parameter_optimizer as optimizer


PROJECT_DIR = Path(__file__).resolve().parent
RESULT_PATTERN = re.compile(
    r"结果：(?P<correct>\d+)/(?P<prediction>\d+)，总收益率"
    r"(?P<total_return>-?\d+(?:\.\d+)?)%，正确率"
    r"(?P<accuracy>-?\d+(?:\.\d+)?)%"
)
SUMMARY_FIELDS = [
    "lookback_days",
    "correct_days",
    "prediction_days",
    "total_return_pct",
    "accuracy_pct",
    "exit_code",
    "result_line",
    "log_file",
    "error",
]
MATRIX_DETAIL_FIELDS = ["as_of_date", *SUMMARY_FIELDS]
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def parse_result_line(output: str) -> dict[str, int | float | str] | None:
    """Extract the final optimizer result line from its combined output."""
    matches = list(RESULT_PATTERN.finditer(output))
    if not matches:
        return None

    match = matches[-1]
    return {
        "correct_days": int(match.group("correct")),
        "prediction_days": int(match.group("prediction")),
        "total_return_pct": float(match.group("total_return")),
        "accuracy_pct": float(match.group("accuracy")),
        "result_line": match.group(0),
    }


def build_optimizer_command(
    lookback_days: int,
    *,
    as_of_date: date | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "strategy_backtest.rolling_parameter_optimizer",
        "--lookback-days",
        str(lookback_days),
    ]
    if as_of_date is not None:
        command.extend(["--as-of-date", as_of_date.isoformat()])
    return command


def run_command(
    command: list[str], *, cwd: Path = PROJECT_DIR
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="gb18030",
        errors="replace",
        check=False,
    )


def _run_optimizer(command: list[str]) -> subprocess.CompletedProcess[str]:
    return run_command(command)


def _validate_range(start: int, end: int) -> None:
    if start < 1 or end < 1:
        raise ValueError("起止 lookback 天数必须为正整数。")
    if start > end:
        raise ValueError("起始 lookback 天数不能大于结束天数。")


def select_as_of_dates(
    next_trade_dates: Mapping[date, date], *, count: int
) -> tuple[date, ...]:
    """Return the latest verifiable end dates in newest-to-oldest order."""
    if count < 1:
        raise ValueError("num 必须为正整数。")
    available_dates = sorted(set(next_trade_dates.values()))
    if len(available_dates) < count:
        raise ValueError(f"可验证交易日只有 {len(available_dates)} 个，不能取 {count} 个。")
    return tuple(reversed(available_dates[-count:]))


def latest_as_of_dates(count: int) -> tuple[date, ...]:
    """Read the optimizer's current returns workbook to determine matrix columns."""
    workbook = optimizer._find_latest_returns_workbook()
    return_data = core.load_strict_next_day_returns(workbook)
    return select_as_of_dates(return_data.next_trade_dates, count=count)


def _execute_optimizer(
    *,
    lookback_days: int,
    as_of_date: date | None,
    run_dir: Path,
    runner: Runner,
) -> dict[str, int | float | str]:
    command = build_optimizer_command(lookback_days, as_of_date=as_of_date)
    description = f"--lookback-days {lookback_days}"
    if as_of_date is not None:
        description += f" --as-of-date {as_of_date.isoformat()}"
    print(f"运行 {description} ...", flush=True)
    error = ""
    try:
        completed = runner(command)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
    except OSError as exception:
        stdout = ""
        stderr = ""
        exit_code = -1
        error = str(exception)

    output = stdout
    if stdout and stderr:
        output += "\n"
    output += stderr
    suffix = f"_asof_{as_of_date.isoformat()}" if as_of_date is not None else ""
    log_path = run_dir / f"lookback_{lookback_days:02d}{suffix}.log"
    log_path.write_text(output, encoding="utf-8")
    parsed = parse_result_line(output)
    row: dict[str, int | float | str] = {
        "lookback_days": lookback_days,
        "correct_days": "",
        "prediction_days": "",
        "total_return_pct": "",
        "accuracy_pct": "",
        "exit_code": exit_code,
        "result_line": "",
        "log_file": log_path.name,
        "error": error,
    }
    if parsed is None:
        row["error"] = error or "未在优化器输出中找到结果行。"
        print(f"  未提取到结果，详见 {log_path}", flush=True)
    else:
        row.update(parsed)
        print(f"  {parsed['result_line']}", flush=True)
    return row


def run_sweep(
    *,
    start: int,
    end: int,
    output_dir: Path,
    runner: Runner = _run_optimizer,
    now: datetime | None = None,
) -> tuple[Path, list[dict[str, int | float | str]]]:
    _validate_range(start, end)

    run_time = now or datetime.now()
    run_dir = output_dir / run_time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, int | float | str]] = []

    for lookback_days in range(start, end + 1):
        rows.append(
            _execute_optimizer(
                lookback_days=lookback_days,
                as_of_date=None,
                run_dir=run_dir,
                runner=runner,
            )
        )

    summary_path = run_dir / "summary.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return summary_path, rows


def run_matrix(
    *,
    start: int,
    end: int,
    as_of_dates: Sequence[date],
    output_dir: Path,
    runner: Runner = _run_optimizer,
    now: datetime | None = None,
) -> tuple[Path, Path, list[dict[str, int | float | str]]]:
    """Run every window/date combination and write a total-return matrix."""
    _validate_range(start, end)
    if not as_of_dates:
        raise ValueError("没有可用于矩阵的截止交易日。")

    run_time = now or datetime.now()
    run_dir = output_dir / run_time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, int | float | str]] = []
    returns: dict[tuple[int, date], int | float | str] = {}

    for lookback_days in range(start, end + 1):
        for as_of_date in as_of_dates:
            row = _execute_optimizer(
                lookback_days=lookback_days,
                as_of_date=as_of_date,
                run_dir=run_dir,
                runner=runner,
            )
            row["as_of_date"] = as_of_date.isoformat()
            rows.append(row)
            returns[(lookback_days, as_of_date)] = row["total_return_pct"]

    detail_path = run_dir / "运行明细.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=MATRIX_DETAIL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    matrix_path = run_dir / "收益率矩阵.csv"
    with matrix_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["交易窗口（天）", *(day.isoformat() for day in as_of_dates)])
        for lookback_days in range(start, end + 1):
            writer.writerow(
                [
                    lookback_days,
                    *(returns[(lookback_days, day)] for day in as_of_dates),
                ]
            )
    return matrix_path, detail_path, rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="循环运行滚动参数优化器并汇总各 lookback 天数的结果。"
    )
    parser.add_argument("--start", type=int, default=7, help="起始 lookback 天数，默认 7")
    parser.add_argument("--end", type=int, default=30, help="结束 lookback 天数，默认 30")
    parser.add_argument(
        "--num",
        type=int,
        help="矩阵列数：从最新可验证交易日向前取的截止交易日数量",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_DIR / "optimizer_lookback_sweep",
        help="日志和汇总 CSV 的根目录",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.num is None:
            summary_path, rows = run_sweep(
                start=args.start,
                end=args.end,
                output_dir=args.output_dir,
            )
            print(f"汇总文件：{summary_path}")
        else:
            matrix_path, detail_path, rows = run_matrix(
                start=args.start,
                end=args.end,
                as_of_dates=latest_as_of_dates(args.num),
                output_dir=args.output_dir,
            )
            print(f"收益率矩阵：{matrix_path}")
            print(f"运行明细：{detail_path}")
    except ValueError as exception:
        print(f"参数错误：{exception}", file=sys.stderr)
        return 2

    failures = [row for row in rows if row["exit_code"] != 0 or row["error"]]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
