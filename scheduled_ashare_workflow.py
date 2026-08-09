"""Coordinate scheduled A-share screening preparation and PushPlus summaries.

The script is run with ``A_Share_investment_Agent/.venv``.  It starts the
indicator project's own Python environment only for the existing daily
screening pipeline.  The optional ``analyze`` mode remains available for
manual use, but no scheduled task invokes it.  The workflow never submits a
broker order.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import html
import json
import logging
import math
from numbers import Number
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import akshare as ak
except ImportError:  # The scheduled task uses the AI project's environment.
    ak = None


SCRIPT_DIR = Path(__file__).resolve().parent
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
DEFAULT_ANALYSIS_CASH = 100_000.0
MAX_STOCKS = 10
CALENDAR_LOOKBACK_DAYS = 31
COLLECT_TIMEOUT_SECONDS = 6 * 60 * 60
ANALYSIS_TIMEOUT_SECONDS = 8 * 60 * 60
PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
PUSHPLUS_MAX_ATTEMPTS = 3
PUSHPLUS_RETRY_DELAY_SECONDS = 5.0
TOP_FIFTY_FILE_NAME = "前 50 名（含所属行业）.csv"
RISK_FILTERED_TOP_TEN_FILE_NAME = "风险过滤后得分前10.csv"
STATE_FILE_NAME = "运行状态.json"


class ScheduledWorkflowError(RuntimeError):
    """A scheduled stage could not produce a trustworthy result."""


def china_now() -> datetime:
    return datetime.now(CHINA_TIMEZONE)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD。") from exc


def _as_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_safe(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, Number):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        if isinstance(value, int):
            return int(value)
        return numeric
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:  # noqa: BLE001
            pass
    return value if isinstance(value, str) else str(value)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _as_mapping(value)


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.stem}.{uuid4().hex}.tmp{path.suffix}")
    try:
        temporary_path.write_text(
            json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_env_value(path: Path, key: str) -> Optional[str]:
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


def configure_logger(output_root: Path, now: datetime) -> logging.Logger:
    logger = logging.getLogger("scheduled_ashare_workflow")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "logs").mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(
        output_root / "logs" / f"scheduled_workflow_{now:%Y-%m-%d}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.__stdout__)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def _normalise_code(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def output_root(indicator_project: Path) -> Path:
    return indicator_project / "daily_trading_outputs" / "scheduled_analysis"


def day_output_dir(indicator_project: Path, trade_day: date) -> Path:
    return output_root(indicator_project) / trade_day.isoformat()


def calendar_cache_path(indicator_project: Path) -> Path:
    return output_root(indicator_project) / "ashare_trading_calendar.json"


def _parse_calendar_dates(value: object) -> set[date]:
    if not isinstance(value, list):
        return set()
    result: set[date] = set()
    for item in value:
        try:
            result.add(date.fromisoformat(str(item)))
        except (TypeError, ValueError):
            continue
    return result


def _fetch_trade_dates(cache_path: Path) -> set[date]:
    if ak is None:
        raise ScheduledWorkflowError(
            "当前 Python 环境未安装 AkShare；请通过 A_Share_investment_Agent 的任务解释器运行。"
        )
    try:
        calendar = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # noqa: BLE001
        raise ScheduledWorkflowError(f"无法获取 A 股交易日历：{exc}") from exc
    if not isinstance(calendar, pd.DataFrame) or "trade_date" not in calendar.columns:
        raise ScheduledWorkflowError("A 股交易日历接口没有返回 trade_date 字段。")

    parsed = pd.to_datetime(calendar["trade_date"], errors="coerce").dropna()
    dates = {timestamp.date() for timestamp in parsed}
    if not dates:
        raise ScheduledWorkflowError("A 股交易日历接口返回为空。")
    write_json(
        cache_path,
        {
            "schema_version": SCHEMA_VERSION,
            "source": "akshare.tool_trade_date_hist_sina",
            "refreshed_at": china_now().isoformat(),
            "trade_dates": sorted(day.isoformat() for day in dates),
        },
    )
    return dates


def load_trade_calendar(indicator_project: Path, required_day: date) -> set[date]:
    """Load a calendar that is known to cover today and the prior month.

    A missing or stale calendar is an error rather than a weekday fallback.
    That makes holidays fail closed instead of running the wrong workflow.
    """

    cache_path = calendar_cache_path(indicator_project)
    cached = _parse_calendar_dates(read_json(cache_path).get("trade_dates"))
    lower_bound = required_day - timedelta(days=CALENDAR_LOOKBACK_DAYS)
    if cached and min(cached) <= lower_bound and max(cached) >= required_day:
        return cached

    try:
        fetched = _fetch_trade_dates(cache_path)
    except ScheduledWorkflowError:
        if cached and min(cached) <= lower_bound and max(cached) >= required_day:
            return cached
        raise
    if min(fetched) > lower_bound or max(fetched) < required_day:
        raise ScheduledWorkflowError(
            f"交易日历未覆盖 {required_day.isoformat()}，拒绝按工作日猜测。"
        )
    return fetched


def previous_trading_day(trade_dates: set[date], scheduled_day: date) -> date:
    candidate = scheduled_day - timedelta(days=1)
    limit = scheduled_day - timedelta(days=CALENDAR_LOOKBACK_DAYS)
    while candidate >= limit:
        if candidate in trade_dates:
            return candidate
        candidate -= timedelta(days=1)
    raise ScheduledWorkflowError(
        f"在 {scheduled_day.isoformat()} 前未找到实际 A 股交易日。"
    )


def scheduled_target_day(
    indicator_project: Path,
    scheduled_day: date,
    logger: logging.Logger,
) -> Optional[date]:
    trade_dates = load_trade_calendar(indicator_project, scheduled_day)
    if scheduled_day not in trade_dates:
        logger.info("%s 不是 A 股交易日，跳过本次计划流程。", scheduled_day.isoformat())
        return None
    target = previous_trading_day(trade_dates, scheduled_day)
    logger.info(
        "Scheduled day %s is a trading day; using prior actual trading day %s.",
        scheduled_day.isoformat(),
        target.isoformat(),
    )
    return target


def _require_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise ScheduledWorkflowError(f"找不到{description}：{path}")
    return path


def _process_log_path(indicator_project: Path, trade_day: date, stage: str) -> Path:
    return day_output_dir(indicator_project, trade_day) / "logs" / f"{stage}.log"


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: int,
    logger: logging.Logger,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Starting %s; output: %s", Path(command[1]).name, log_path)
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- started {china_now().isoformat()} ---\n")
            process = subprocess.run(
                command,
                cwd=str(cwd),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )
            handle.write(
                f"--- finished {china_now().isoformat()} exit={process.returncode} ---\n"
            )
    except subprocess.TimeoutExpired as exc:
        raise ScheduledWorkflowError(
            f"后台子流程超时（>{timeout_seconds // 3600} 小时）：{log_path}"
        ) from exc
    return process.returncode


def _after_close_completed(archive_dir: Path) -> bool:
    state = read_json(archive_dir / STATE_FILE_NAME)
    stages = _as_mapping(state.get("stages"))
    after_close = _as_mapping(stages.get("after_close"))
    return after_close.get("status") == "completed"


def run_collect(
    *,
    indicator_project: Path,
    agent_project: Path,
    scheduled_day: date,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    del agent_project  # Kept in the public signature for uniform task actions.
    target_day = scheduled_target_day(indicator_project, scheduled_day, logger)
    if target_day is None:
        return 0

    output_dir = day_output_dir(indicator_project, target_day)
    state_path = output_dir / "data_preparation.json"
    archive_dir = (
        indicator_project / "daily_trading_outputs" / "archive" / target_day.isoformat()
    )
    source_csv = archive_dir / TOP_FIFTY_FILE_NAME
    risk_filtered_top_ten_csv = archive_dir / RISK_FILTERED_TOP_TEN_FILE_NAME
    if dry_run:
        logger.info("Dry run: would prepare screening data for %s.", target_day.isoformat())
        return 0

    indicator_python = _require_file(
        indicator_project / ".venv" / "Scripts" / "python.exe",
        "指标项目 Python 解释器",
    )
    runner = _require_file(indicator_project / "daily_trading_runner.py", "日常选股脚本")
    command = [
        str(indicator_python),
        str(runner),
        "--project-dir",
        str(indicator_project),
        "--mode",
        "after-close",
        "--as-of-date",
        target_day.isoformat(),
        "--no-push",
    ]
    return_code = _run_process(
        command,
        cwd=indicator_project,
        log_path=_process_log_path(indicator_project, target_day, "data_preparation"),
        timeout_seconds=COLLECT_TIMEOUT_SECONDS,
        logger=logger,
    )

    completed = (
        return_code == 0
        and source_csv.is_file()
        and risk_filtered_top_ten_csv.is_file()
        and _after_close_completed(archive_dir)
    )
    candidate_count: Optional[int] = None
    candidate_validation_error: Optional[str] = None
    if completed:
        try:
            candidate_count = len(read_risk_filtered_candidates(risk_filtered_top_ten_csv))
        except ScheduledWorkflowError as exc:
            completed = False
            candidate_validation_error = str(exc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": "data_preparation",
        "scheduled_run_date": scheduled_day.isoformat(),
        "trade_date": target_day.isoformat(),
        "status": "completed" if completed else "failed",
        "completed_at": china_now().isoformat(),
        "source_csv": str(source_csv),
        "risk_filtered_top_ten_csv": str(risk_filtered_top_ten_csv),
        "candidate_count": candidate_count,
        "risk_filter_enabled": True,
        "require_all_selected_conditions": False,
        "archive_dir": str(archive_dir),
        "runner_log": str(_process_log_path(indicator_project, target_day, "data_preparation")),
        "message_sent": False,
    }
    write_json(state_path, payload)
    if not completed:
        validation_suffix = (
            f"；风险过滤候选文件无效：{candidate_validation_error}"
            if candidate_validation_error
            else ""
        )
        raise ScheduledWorkflowError(
            f"凌晨数据任务未完成（exit={return_code} 或归档不完整）：{target_day.isoformat()}。"
            f"{validation_suffix}"
        )
    logger.info("Data preparation completed for %s.", target_day.isoformat())
    return 0


def _read_csv(path: Path, *, allow_empty: bool = False) -> pd.DataFrame:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            frame = pd.read_csv(path, encoding=encoding)
        except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
            errors.append(f"{encoding}: {exc}")
            continue
        if frame.empty and not allow_empty:
            raise ScheduledWorkflowError(f"候选 CSV 为空：{path}")
        return frame
    raise ScheduledWorkflowError(f"无法读取候选 CSV：{' | '.join(errors)}")


def _csv_value(row: Mapping[str, object], key: str, default: object = None) -> object:
    value = row.get(key, default)
    return _json_safe(value)


def read_top_ten(source_csv: Path) -> list[dict[str, object]]:
    frame = _read_csv(source_csv)
    if "股票代码" not in frame.columns:
        raise ScheduledWorkflowError(f"候选 CSV 缺少“股票代码”列：{source_csv}")

    stocks: list[dict[str, object]] = []
    for index, (_, row) in enumerate(frame.head(MAX_STOCKS).iterrows(), start=1):
        source_row = {str(key): _json_safe(value) for key, value in row.to_dict().items()}
        ticker = _normalise_code(source_row.get("股票代码"))
        if len(ticker) != 6 or not ticker.isdigit():
            raise ScheduledWorkflowError(f"候选 CSV 中有无效股票代码：{ticker!r}")
        stocks.append(
            {
                "ticker": ticker,
                "name": _csv_value(source_row, "股票名称", ""),
                "industry": _csv_value(source_row, "所属行业", ""),
                "screening": {
                    "rank": _csv_value(source_row, "评分排名", index),
                    "score": _csv_value(source_row, "得分"),
                    "screen_date": _csv_value(source_row, "数据日期"),
                    "close": _csv_value(source_row, "收盘价"),
                    "turnover": _csv_value(source_row, "换手率"),
                    "amount": _csv_value(source_row, "当日成交额"),
                    "change_pct": _csv_value(source_row, "当日涨跌幅"),
                },
                "source_row": source_row,
            }
        )
    if len(stocks) != MAX_STOCKS:
        raise ScheduledWorkflowError(
            f"候选 CSV 只有 {len(stocks)} 只有效股票，无法完成前 {MAX_STOCKS} 名分析。"
        )
    return stocks


def _candidate_text(value: object, default: str) -> str:
    value = _json_safe(value)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def read_risk_filtered_candidates(source_csv: Path) -> list[dict[str, str]]:
    """Read up to ten risk-filtered candidates, treating a header-only CSV as valid."""

    frame = _read_csv(source_csv, allow_empty=True)
    required_columns = ("股票代码", "股票名称", "未满足条件（扣分项）")
    missing_columns = [column for column in required_columns if column not in frame.columns]
    if missing_columns:
        joined = "、".join(missing_columns)
        raise ScheduledWorkflowError(f"风险过滤候选 CSV 缺少字段“{joined}”：{source_csv}")

    candidates: list[dict[str, str]] = []
    for _, row in frame.head(MAX_STOCKS).iterrows():
        ticker = _normalise_code(row.get("股票代码", ""))
        if len(ticker) != 6 or not ticker.isdigit():
            raise ScheduledWorkflowError(f"风险过滤候选 CSV 中有无效股票代码：{ticker!r}")
        candidates.append(
            {
                "ticker": ticker,
                "name": _candidate_text(row.get("股票名称"), "-"),
                "deduction": _candidate_text(row.get("未满足条件（扣分项）"), "无"),
            }
        )
    return candidates


def _default_holdings() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_cash": DEFAULT_ANALYSIS_CASH,
        "positions": {},
        "note": "positions 为空时，持仓状态会标为未配置；analysis_cash 仅用于单股情景分析，不代表实际账户资金。",
    }


def load_holdings(indicator_project: Path) -> tuple[dict[str, Any], Path]:
    path = output_root(indicator_project) / "holdings.json"
    payload = read_json(path)
    if not payload:
        payload = _default_holdings()
        write_json(path, payload)
    positions = payload.get("positions")
    if not isinstance(positions, Mapping):
        payload["positions"] = {}
    return payload, path


def _as_nonnegative_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result >= 0 else default


def apply_holding_context(stocks: list[dict[str, object]], holdings: Mapping[str, Any]) -> None:
    positions = _as_mapping(holdings.get("positions"))
    normalised_positions = {
        _normalise_code(code): _as_mapping(value) for code, value in positions.items()
    }
    configured_cash = holdings.get("analysis_cash")
    cash_source = "持仓配置" if configured_cash is not None else "模拟值"
    analysis_cash = _as_nonnegative_float(configured_cash, DEFAULT_ANALYSIS_CASH)

    for stock in stocks:
        ticker = _normalise_code(stock.get("ticker"))
        position = normalised_positions.get(ticker)
        if not position:
            holding = {
                "status": "未配置",
                "shares": None,
                "average_cost": None,
                "updated_at": None,
            }
            shares = 0
        else:
            shares = int(_as_nonnegative_float(position.get("shares"), 0.0))
            holding = {
                "status": "已持有" if shares > 0 else "已配置但未持有",
                "shares": shares,
                "average_cost": _json_safe(position.get("average_cost")),
                "updated_at": _json_safe(position.get("updated_at")),
            }
        stock["holding"] = holding
        stock["analysis_portfolio"] = {
            "cash": analysis_cash,
            "stock": shares,
            "cash_source": cash_source,
            "note": "单股分析情景资金；非真实账户下单金额。",
        }


def run_analyze(
    *,
    indicator_project: Path,
    agent_project: Path,
    scheduled_day: date,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    target_day = scheduled_target_day(indicator_project, scheduled_day, logger)
    if target_day is None:
        return 0

    output_dir = day_output_dir(indicator_project, target_day)
    preparation = read_json(output_dir / "data_preparation.json")
    if preparation.get("status") != "completed" or preparation.get("trade_date") != target_day.isoformat():
        raise ScheduledWorkflowError(
            f"缺少 {target_day.isoformat()} 的凌晨数据结果，04:00 分析将等待任务重试。"
        )
    source_csv = Path(str(preparation.get("source_csv", "")))
    _require_file(source_csv, "前 50 名候选 CSV")
    stocks = read_top_ten(source_csv)
    holdings, holdings_path = load_holdings(indicator_project)
    apply_holding_context(stocks, holdings)

    if dry_run:
        logger.info("Dry run: %s top-10 stocks are ready for analysis.", len(stocks))
        return 0

    input_path = output_dir / "analysis_input.json"
    agent_output_path = output_dir / "agent_batch_results.json"
    analysis_path = output_dir / "analysis.json"
    write_json(
        input_path,
        {
            "schema_version": SCHEMA_VERSION,
            "trade_date": target_day.isoformat(),
            "source_csv": str(source_csv),
            "analysis_basis": "previous_trading_day_close",
            "stocks": stocks,
        },
    )

    agent_python = _require_file(
        agent_project / ".venv" / "Scripts" / "python.exe",
        "A 股 AI 项目 Python 解释器",
    )
    batch_runner = _require_file(
        agent_project / "src" / "scheduled_batch_analysis.py",
        "批量 AI 分析脚本",
    )
    return_code = _run_process(
        [
            str(agent_python),
            str(batch_runner),
            "--input-file",
            str(input_path),
            "--output-file",
            str(agent_output_path),
        ],
        cwd=agent_project,
        log_path=_process_log_path(indicator_project, target_day, "top_ten_analysis"),
        timeout_seconds=ANALYSIS_TIMEOUT_SECONDS,
        logger=logger,
    )
    agent_results = read_json(agent_output_path)
    agent_status = agent_results.get("status")
    completed = return_code == 0 and agent_status == "completed"
    analysis_payload = {
        "schema_version": SCHEMA_VERSION,
        "stage": "top_ten_analysis",
        "scheduled_run_date": scheduled_day.isoformat(),
        "trade_date": target_day.isoformat(),
        "status": "completed" if completed else "partial",
        "completed_at": china_now().isoformat(),
        "source_csv": str(source_csv),
        "holdings_file": str(holdings_path),
        "analysis_basis": "previous_trading_day_close",
        "realtime_execution_note": "此结果基于前一交易日收盘数据，不是盘中实时下单信号。",
        "agent_result_file": str(agent_output_path),
        "runner_log": str(_process_log_path(indicator_project, target_day, "top_ten_analysis")),
        "stocks": agent_results.get("stocks", []),
        "summary": _as_mapping(agent_results.get("summary")),
    }
    write_json(analysis_path, analysis_payload)
    if not completed:
        raise ScheduledWorkflowError(
            f"前 10 名 AI 分析未完全完成（exit={return_code}，status={agent_status!r}）；结果已保留，计划任务会重试。"
        )
    logger.info("Top-10 AI analysis completed for %s.", target_day.isoformat())
    return 0


def _format_number(value: object, digits: int = 2) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(numeric):
        return "-"
    return f"{numeric:.{digits}f}"


def _decision_label(value: object) -> str:
    labels = {"buy": "买入候选", "sell": "卖出候选", "reduce": "减仓候选", "hold": "观望"}
    return labels.get(str(value).lower(), str(value) or "-" )


def _truncate(value: object, limit: int = 150) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def build_pushplus_html(preparation: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    stocks = analysis.get("stocks")
    rows: list[str] = []
    if isinstance(stocks, list):
        for item in stocks:
            stock = _as_mapping(item)
            screening = _as_mapping(stock.get("screening"))
            holding = _as_mapping(stock.get("holding"))
            decision = _as_mapping(stock.get("decision"))
            risk = _as_mapping(stock.get("risk"))
            market = _as_mapping(stock.get("market_snapshot"))
            decision_reason = decision.get("reasoning_snippet") or decision.get("reasoning")
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(screening.get('rank', '-')))}</td>"
                f"<td>{html.escape(str(stock.get('ticker', '-')))}</td>"
                f"<td>{html.escape(str(stock.get('name', '-')))}</td>"
                f"<td>{html.escape(str(stock.get('industry', '-')))}</td>"
                f"<td>{html.escape(_format_number(screening.get('score'), 1))}</td>"
                f"<td>{html.escape(_format_number(screening.get('close'), 2))}</td>"
                f"<td>{html.escape(str(holding.get('status', '未配置')))}</td>"
                f"<td>{html.escape(_decision_label(decision.get('action') or risk.get('trading_action')))}</td>"
                f"<td>{html.escape(_format_number(decision.get('confidence'), 2))}</td>"
                f"<td>{html.escape(str(risk.get('risk_score', '-')))}</td>"
                f"<td>{html.escape(_truncate(decision_reason))}</td>"
                f"<td>{html.escape(str(market.get('quote_source', '-')))}</td>"
                "</tr>"
            )
    trade_date = html.escape(str(analysis.get("trade_date", "")))
    source_csv = html.escape(str(preparation.get("source_csv", "")))
    summary = _as_mapping(analysis.get("summary"))
    return "".join(
        [
            f"<h3>A 股轮动分析：{trade_date}</h3>",
            "<p>分析基准：前一实际交易日收盘数据；仅供研究，不构成实时下单指令或投资建议。</p>",
            f"<p>候选来源：{source_csv}</p>",
            f"<p>完成：{html.escape(str(summary.get('completed', '-')))} / "
            f"{html.escape(str(summary.get('requested', '-')))}；失败："
            f"{html.escape(str(summary.get('failed', '-')))}</p>",
            "<table border='1' cellspacing='0' cellpadding='5'>",
            "<tr><th>排名</th><th>代码</th><th>名称</th><th>行业</th><th>筛选分</th>"
            "<th>前收</th><th>持仓</th><th>结论</th><th>置信度</th><th>风险分</th>"
            "<th>结论摘要</th><th>行情来源</th></tr>",
            "".join(rows),
            "</table>",
            "<p>“未配置”表示本地持仓文件尚未记录该股票，不能解释为未持有。"
            "盘中实际交易前仍应核对实时价格、涨跌停、流动性和个人仓位。</p>",
        ]
    )


def build_screening_pushplus_html(
    trade_date: str,
    candidates: list[Mapping[str, str]],
) -> str:
    escaped_trade_date = html.escape(trade_date)
    if not candidates:
        return "".join(
            [
                f"<h3>A 股风险过滤候选：{escaped_trade_date}</h3>",
                "<p>风险过滤后无候选。</p>",
            ]
        )

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(candidate.get('ticker', '-')))}</td>"
        f"<td>{html.escape(str(candidate.get('name', '-')))}</td>"
        f"<td>{html.escape(str(candidate.get('deduction', '无')))}</td>"
        "</tr>"
        for candidate in candidates
    )
    return "".join(
        [
            f"<h3>A 股风险过滤候选：{escaped_trade_date}</h3>",
            "<table border='1' cellspacing='0' cellpadding='5'>",
            "<tr><th>股票代码</th><th>股票名称</th><th>未满足条件（扣分项）</th></tr>",
            rows,
            "</table>",
        ]
    )


def send_pushplus(
    *,
    env_file: Path,
    title: str,
    content: str,
) -> None:
    token = os.environ.get("PushPlusapi") or os.environ.get("PushPlus_token")
    token = token or read_env_value(env_file, "PushPlusapi") or read_env_value(env_file, "PushPlus_token")
    if not token:
        raise ScheduledWorkflowError(".env 中缺少 PushPlusapi 或 PushPlus_token。")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(
            PUSHPLUS_ENDPOINT,
            data={"token": token, "title": title, "content": content, "template": "html"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ScheduledWorkflowError(f"PushPlus 发送失败：{exc}") from exc
    if not isinstance(payload, Mapping) or int(payload.get("code", -1)) != 200:
        message = payload.get("msg", "未知响应") if isinstance(payload, Mapping) else "非 JSON 响应"
        raise ScheduledWorkflowError(f"PushPlus 未接受消息：{message}")


def send_pushplus_with_retry(
    *,
    env_file: Path,
    title: str,
    content: str,
    logger: logging.Logger,
) -> None:
    for attempt in range(1, PUSHPLUS_MAX_ATTEMPTS + 1):
        try:
            send_pushplus(env_file=env_file, title=title, content=content)
            return
        except ScheduledWorkflowError as exc:
            logger.warning(
                "PushPlus send attempt %d/%d failed: %s",
                attempt,
                PUSHPLUS_MAX_ATTEMPTS,
                exc,
            )
            if attempt == PUSHPLUS_MAX_ATTEMPTS:
                logger.error(
                    "PushPlus summary delivery failed after %d attempts.",
                    PUSHPLUS_MAX_ATTEMPTS,
                )
                raise
            logger.info(
                "Retrying PushPlus summary in %.1f seconds (attempt %d/%d).",
                PUSHPLUS_RETRY_DELAY_SECONDS,
                attempt + 1,
                PUSHPLUS_MAX_ATTEMPTS,
            )
            time.sleep(PUSHPLUS_RETRY_DELAY_SECONDS)


def run_send(
    *,
    indicator_project: Path,
    agent_project: Path,
    scheduled_day: date,
    dry_run: bool,
    force: bool,
    logger: logging.Logger,
) -> int:
    del agent_project  # Kept in the public signature for uniform task actions.
    target_day = scheduled_target_day(indicator_project, scheduled_day, logger)
    if target_day is None:
        return 0

    output_dir = day_output_dir(indicator_project, target_day)
    preparation = read_json(output_dir / "data_preparation.json")
    if (
        preparation.get("status") != "completed"
        or preparation.get("trade_date") != target_day.isoformat()
    ):
        raise ScheduledWorkflowError(f"缺少 {target_day.isoformat()} 的凌晨数据结果，不能发送。")
    candidate_csv_value = str(preparation.get("risk_filtered_top_ten_csv", "")).strip()
    if not candidate_csv_value:
        raise ScheduledWorkflowError(
            f"凌晨数据结果缺少 {target_day.isoformat()} 的风险过滤候选文件路径，不能发送。"
        )
    candidate_csv = _require_file(Path(candidate_csv_value), "风险过滤后前 10 候选 CSV")
    candidates = read_risk_filtered_candidates(candidate_csv)

    title = f"A 股风险过滤候选 {target_day.isoformat()}（前 10 名）"
    content = build_screening_pushplus_html(target_day.isoformat(), candidates)
    message_path = output_dir / "pushplus_message.html"
    message_path.write_text(content, encoding="utf-8")
    send_state_path = output_dir / "pushplus_send_state.json"
    prior_state = read_json(send_state_path)
    if prior_state.get("status") == "sent" and not force:
        logger.info("PushPlus summary was already sent for %s.", target_day.isoformat())
        return 0
    if dry_run:
        logger.info("Dry run: generated PushPlus payload for %s at %s.", target_day.isoformat(), message_path)
        return 0

    send_pushplus_with_retry(
        env_file=indicator_project / ".env",
        title=title,
        content=content,
        logger=logger,
    )
    write_json(
        send_state_path,
        {
            "schema_version": SCHEMA_VERSION,
            "trade_date": target_day.isoformat(),
            "status": "sent",
            "sent_at": china_now().isoformat(),
            "title": title,
            "message_file": str(message_path),
        },
    )
    logger.info("PushPlus summary sent for %s.", target_day.isoformat())
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("collect", "analyze", "send"))
    parser.add_argument("--indicator-project-dir", type=Path, default=SCRIPT_DIR)
    parser.add_argument(
        "--agent-project-dir",
        type=Path,
        default=SCRIPT_DIR.parent / "A_Share_investment_Agent",
    )
    parser.add_argument(
        "--run-date",
        type=parse_date,
        help="Manual replay only: the scheduled trading day; defaults to today in China.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs without collecting, analyzing, or sending.")
    parser.add_argument("--force", action="store_true", help="Allow a previously sent 09:00 summary to be sent again.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    indicator_project = args.indicator_project_dir.resolve()
    agent_project = args.agent_project_dir.resolve()
    if not indicator_project.is_dir():
        raise ScheduledWorkflowError(f"指标项目目录不存在：{indicator_project}")
    if not agent_project.is_dir():
        raise ScheduledWorkflowError(f"A 股 AI 项目目录不存在：{agent_project}")
    now = china_now()
    scheduled_day = args.run_date or now.date()
    logger = configure_logger(output_root(indicator_project), now)
    if args.mode == "collect":
        return run_collect(
            indicator_project=indicator_project,
            agent_project=agent_project,
            scheduled_day=scheduled_day,
            dry_run=bool(args.dry_run),
            logger=logger,
        )
    if args.mode == "analyze":
        return run_analyze(
            indicator_project=indicator_project,
            agent_project=agent_project,
            scheduled_day=scheduled_day,
            dry_run=bool(args.dry_run),
            logger=logger,
        )
    return run_send(
        indicator_project=indicator_project,
        agent_project=agent_project,
        scheduled_day=scheduled_day,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        logger=logger,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScheduledWorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
