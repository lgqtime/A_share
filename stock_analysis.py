"""单股技术分析的数据获取与指标计算核心。

本模块与 Streamlit 页面解耦：负责获取较长的前复权日线、规范化 OHLCV 数据，
并计算 MA、MACD 和可配置的 KDJ（默认参数为 89,3,3）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from numbers import Integral
from typing import Any, Mapping

import pandas as pd
import requests


EASTMONEY_DAILY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_RICH_DAILY_KLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
)
TENCENT_LEGACY_DAILY_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

KDJ_RSV_PERIOD = 89
KDJ_K_SMOOTHING_PERIOD = 3
KDJ_D_SMOOTHING_PERIOD = 3
KDJ_DISPLAY_WARMUP_BARS = 40
DEFAULT_DISPLAY_BARS = 120

HISTORY_COLUMNS = (
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "amplitude",
    "pct_change",
    "turnover",
)


class StockHistoryError(RuntimeError):
    """公开行情接口或日线数据不满足分析条件时抛出。"""


@dataclass(frozen=True)
class KdjParameters:
    """KDJ 的 RSV 周期、K 线平滑周期和 D 线平滑周期。"""

    rsv_period: int = KDJ_RSV_PERIOD
    k_smoothing_period: int = KDJ_K_SMOOTHING_PERIOD
    d_smoothing_period: int = KDJ_D_SMOOTHING_PERIOD

    def __post_init__(self) -> None:
        """确保动态配置能用于滚动窗口和递推计算。"""

        parameter_names = (
            ("rsv_period", "RSV 周期"),
            ("k_smoothing_period", "K 线平滑周期"),
            ("d_smoothing_period", "D 线平滑周期"),
        )
        for attribute, label in parameter_names:
            value = getattr(self, attribute)
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError(f"KDJ 参数{label}必须是大于 0 的整数。")
            # 统一 numpy 整数等 Integral 子类的运行时类型，便于后续计算和展示。
            object.__setattr__(self, attribute, int(value))


DEFAULT_KDJ_PARAMETERS = KdjParameters()


def _resolve_kdj_parameters(
    kdj_parameters: KdjParameters | None,
) -> KdjParameters:
    """返回可用的 KDJ 配置，未传入时严格沿用既有默认参数。"""

    if kdj_parameters is None:
        return DEFAULT_KDJ_PARAMETERS
    if not isinstance(kdj_parameters, KdjParameters):
        raise TypeError("kdj_parameters 必须是 KdjParameters 或 None。")
    return kdj_parameters


def _format_kdj_parameters(kdj_parameters: KdjParameters) -> str:
    """生成用于错误提示的 KDJ 参数文本。"""

    return (
        f"KDJ({kdj_parameters.rsv_period},"
        f"{kdj_parameters.k_smoothing_period},"
        f"{kdj_parameters.d_smoothing_period})"
    )


def _kdj_warmup_bars(kdj_parameters: KdjParameters) -> int:
    """返回 KDJ 递推在显示前所需的最小预热根数。"""

    return max(
        KDJ_DISPLAY_WARMUP_BARS,
        5 * max(
            kdj_parameters.k_smoothing_period,
            kdj_parameters.d_smoothing_period,
        ),
    )


@dataclass(frozen=True)
class SecurityCode:
    """规范化后的沪深 A 股代码及东方财富 secid。"""

    code: str
    exchange: str
    secid: str


def normalize_stock_code(value: object) -> SecurityCode:
    """接受常见沪深代码格式，返回可用于东方财富日线接口的证券标识。"""

    raw_value = "" if value is None else str(value)
    raw = raw_value.strip().upper().replace(" ", "")
    matched = re.fullmatch(r"(?:(SZ|SH))?(\d{6})(?:\.(SZ|SH))?", raw)
    if matched is None:
        raise ValueError("股票代码必须是 6 位沪深 A 股代码，例如 000001 或 600000。")

    prefix_exchange, code, suffix_exchange = matched.groups()
    declared_exchange = prefix_exchange or suffix_exchange
    if prefix_exchange and suffix_exchange and prefix_exchange != suffix_exchange:
        raise ValueError("股票代码的前缀和后缀市场标识不一致。")

    if code.startswith(("0", "2", "3")):
        inferred_exchange = "SZ"
    elif code.startswith(("5", "6", "9")):
        inferred_exchange = "SH"
    else:
        raise ValueError("目前仅支持沪深 A 股代码。")
    if declared_exchange and declared_exchange != inferred_exchange:
        raise ValueError("股票代码与指定的沪深市场标识不一致。")

    return SecurityCode(
        code=code,
        exchange=inferred_exchange,
        secid=f"{0 if inferred_exchange == 'SZ' else 1}.{code}",
    )


def required_history_bars(
    display_bars: int,
    *,
    kdj_parameters: KdjParameters | None = None,
) -> int:
    """返回让显示窗口内 KDJ 都经过预热所需的最少原始日线根数。"""

    try:
        bars = int(display_bars)
    except (TypeError, ValueError) as exc:
        raise ValueError("显示交易日数必须是正整数。") from exc
    if bars <= 0:
        raise ValueError("显示交易日数必须大于 0。")
    parameters = _resolve_kdj_parameters(kdj_parameters)
    return bars + parameters.rsv_period + _kdj_warmup_bars(parameters)


def _as_of_day(value: date | datetime | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("截至日期必须是 date 或 datetime。")


def _coerce_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan", "NaN"}:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """规范化 OHLCV，并丢弃日期、价格或高低价关系不合法的日线。"""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("历史数据必须是 pandas.DataFrame。")

    normalized = frame.copy()
    for column in HISTORY_COLUMNS:
        if column not in normalized:
            normalized[column] = pd.NA
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in HISTORY_COLUMNS:
        if column != "date":
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized = normalized.dropna(subset=["date", "open", "close", "high", "low", "volume"])
    normalized = normalized.loc[
        (normalized["open"] > 0)
        & (normalized["close"] > 0)
        & (normalized["high"] > 0)
        & (normalized["low"] > 0)
        & (normalized["volume"] >= 0)
        & (normalized["high"] >= normalized["low"])
        & (normalized["high"] >= normalized[["open", "close"]].max(axis=1))
        & (normalized["low"] <= normalized[["open", "close"]].min(axis=1))
    ]
    normalized = normalized.sort_values("date", kind="stable")
    normalized = normalized.drop_duplicates(subset=["date"], keep="last")

    previous_close = normalized["close"].shift(1)
    derived_pct_change = (normalized["close"] / previous_close - 1.0) * 100.0
    derived_amplitude = (normalized["high"] - normalized["low"]) / previous_close * 100.0
    normalized["pct_change"] = normalized["pct_change"].where(
        normalized["pct_change"].notna(), derived_pct_change
    )
    normalized["amplitude"] = normalized["amplitude"].where(
        normalized["amplitude"].notna(), derived_amplitude
    )
    return normalized.loc[:, HISTORY_COLUMNS].reset_index(drop=True)


def parse_eastmoney_history(payload: Mapping[str, Any]) -> pd.DataFrame:
    """解析东方财富前复权日 K 响应，保留完整有效历史而不截断到 120 根。"""

    if str(payload.get("rc", "0")) not in {"0", "None"} and payload.get("rc") not in (0, None):
        raise StockHistoryError(f"东方财富接口返回 rc={payload.get('rc')!r}。")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise StockHistoryError("东方财富响应没有 data 对象。")
    raw_klines = data.get("klines")
    if not isinstance(raw_klines, list):
        raise StockHistoryError("东方财富响应没有日 K 列表。")

    rows: list[dict[str, object]] = []
    for raw_kline in raw_klines:
        if not isinstance(raw_kline, str):
            continue
        fields = raw_kline.split(",")
        if len(fields) < 11:
            continue
        rows.append(
            {
                "date": fields[0],
                "open": _coerce_number(fields[1]),
                "close": _coerce_number(fields[2]),
                "high": _coerce_number(fields[3]),
                "low": _coerce_number(fields[4]),
                "volume": _coerce_number(fields[5]),
                "amount": _coerce_number(fields[6]),
                "amplitude": _coerce_number(fields[7]),
                "pct_change": _coerce_number(fields[8]),
                "turnover": _coerce_number(fields[10]),
            }
        )
    history = normalize_ohlcv_frame(pd.DataFrame(rows))
    if history.empty:
        raise StockHistoryError("东方财富响应中没有可用的日 K 数据。")
    return history


def _extract_tencent_klines(
    payload: Mapping[str, Any],
    symbol: str,
    *,
    source_name: str,
) -> list[object]:
    """提取腾讯前复权日 K 数组，兼容接口暂时返回的 day 字段。"""

    if payload.get("code") not in (None, 0, "0"):
        raise StockHistoryError(f"{source_name}接口返回 code={payload.get('code')!r}。")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise StockHistoryError(f"{source_name}响应没有 data 对象。")
    security_data = data.get(symbol)
    if not isinstance(security_data, Mapping):
        raise StockHistoryError(f"{source_name}响应没有 {symbol} 的日 K 数据。")
    raw_klines = security_data.get("qfqday")
    if not isinstance(raw_klines, list):
        raw_klines = security_data.get("day")
    if not isinstance(raw_klines, list):
        raise StockHistoryError(f"{source_name}响应没有前复权日 K 列表。")
    return raw_klines


def parse_tencent_rich_history(raw_text: str, symbol: str) -> pd.DataFrame:
    """解析腾讯增强前复权日 K，并保留逐日成交额和换手率。"""

    json_text = raw_text.strip()
    if "=" in json_text:
        json_text = json_text.split("=", 1)[1].strip()
    json_text = json_text.rstrip(";").strip()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise StockHistoryError("腾讯增强日 K 响应不是有效 JSON。") from exc
    if not isinstance(payload, Mapping):
        raise StockHistoryError("腾讯增强日 K 响应不是对象。")

    rows: list[dict[str, object]] = []
    for raw_kline in _extract_tencent_klines(
        payload, symbol, source_name="腾讯增强日 K"
    ):
        if not isinstance(raw_kline, (list, tuple)) or len(raw_kline) < 9:
            continue
        amount_in_ten_thousand = _coerce_number(raw_kline[8])
        rows.append(
            {
                "date": raw_kline[0],
                "open": _coerce_number(raw_kline[1]),
                "close": _coerce_number(raw_kline[2]),
                "high": _coerce_number(raw_kline[3]),
                "low": _coerce_number(raw_kline[4]),
                "volume": _coerce_number(raw_kline[5]),
                # 增强接口第 7、8 位分别是换手率和万元口径的成交额。
                "amount": (
                    amount_in_ten_thousand * 10_000
                    if amount_in_ten_thousand is not None
                    else None
                ),
                "amplitude": None,
                "pct_change": None,
                "turnover": _coerce_number(raw_kline[7]),
            }
        )
    history = normalize_ohlcv_frame(pd.DataFrame(rows))
    if history.empty:
        raise StockHistoryError("腾讯增强日 K 响应中没有可用的日线数据。")
    return history


def parse_tencent_legacy_history(
    payload: Mapping[str, Any], symbol: str
) -> pd.DataFrame:
    """解析腾讯旧版前复权日 K；该接口不提供逐日成交额和换手率。"""

    rows: list[dict[str, object]] = []
    for raw_kline in _extract_tencent_klines(
        payload, symbol, source_name="腾讯旧版日 K"
    ):
        if not isinstance(raw_kline, (list, tuple)) or len(raw_kline) < 6:
            continue
        rows.append(
            {
                "date": raw_kline[0],
                "open": _coerce_number(raw_kline[1]),
                "close": _coerce_number(raw_kline[2]),
                "high": _coerce_number(raw_kline[3]),
                "low": _coerce_number(raw_kline[4]),
                "volume": _coerce_number(raw_kline[5]),
                "amount": None,
                "amplitude": None,
                "pct_change": None,
                "turnover": None,
            }
        )
    history = normalize_ohlcv_frame(pd.DataFrame(rows))
    if history.empty:
        raise StockHistoryError("腾讯旧版日 K 响应中没有可用的日线数据。")
    return history


def _limit_history_for_analysis(
    history: pd.DataFrame,
    *,
    target_day: date,
    history_bars: int,
) -> pd.DataFrame:
    """严格按截至日期过滤，再保留绘图与预热所需的最近日线。"""

    dates = pd.to_datetime(history["date"], errors="coerce").dt.normalize()
    filtered = history.loc[dates.le(pd.Timestamp(target_day))].copy()
    return filtered.tail(history_bars).reset_index(drop=True)


def _validate_analysis_history(
    history: pd.DataFrame,
    security: SecurityCode,
    *,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """校验日线长度，避免用不足 RSV 周期的数据伪造 KDJ。"""

    parameters = _resolve_kdj_parameters(kdj_parameters)
    if len(history) < parameters.rsv_period:
        raise StockHistoryError(
            f"{security.code} 仅有 {len(history)} 根有效日线，不足以计算 "
            f"{_format_kdj_parameters(parameters)}。"
        )
    return history


def _fetch_tencent_adjusted_daily_history(
    security: SecurityCode,
    *,
    target_day: date,
    start_day: date,
    history_bars: int,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """按增强接口、备用增强接口、旧版接口顺序获取腾讯前复权日 K。"""

    symbol = f"{security.exchange.lower()}{security.code}"
    kline_parameter = (
        f"{symbol},day,{start_day.isoformat()},{target_day.isoformat()},"
        f"{history_bars},qfq"
    )
    failures: list[str] = []
    last_error: Exception | None = None

    for endpoint in TENCENT_RICH_DAILY_KLINE_URLS:
        try:
            response = requests.get(
                endpoint,
                params={"_var": "kline_dayqfq", "param": kline_parameter},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            history = parse_tencent_rich_history(response.text, symbol)
            history = _limit_history_for_analysis(
                history, target_day=target_day, history_bars=history_bars
            )
            return _validate_analysis_history(
                history,
                security,
                kdj_parameters=kdj_parameters,
            )
        except (requests.RequestException, StockHistoryError, TypeError, ValueError, AttributeError) as exc:
            last_error = exc
            failures.append(f"增强接口 {endpoint}：{exc}")

    try:
        response = requests.get(
            TENCENT_LEGACY_DAILY_KLINE_URL,
            params={"param": kline_parameter},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()
        history = parse_tencent_legacy_history(response.json(), symbol)
        history = _limit_history_for_analysis(
            history, target_day=target_day, history_bars=history_bars
        )
        return _validate_analysis_history(
            history,
            security,
            kdj_parameters=kdj_parameters,
        )
    except (requests.RequestException, StockHistoryError, TypeError, ValueError, AttributeError) as exc:
        last_error = exc
        failures.append(f"旧版接口 {TENCENT_LEGACY_DAILY_KLINE_URL}：{exc}")

    raise StockHistoryError("腾讯前复权日 K 回退失败：" + "；".join(failures)) from last_error


def fetch_adjusted_daily_history(
    value: object,
    *,
    display_bars: int = DEFAULT_DISPLAY_BARS,
    as_of_date: date | datetime | None = None,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """获取足够长的前复权日线，让显示窗口内的 KDJ 有完整预热。"""

    security = normalize_stock_code(value)
    target_day = _as_of_day(as_of_date)
    kdj_config = _resolve_kdj_parameters(kdj_parameters)
    history_bars = required_history_bars(
        display_bars,
        kdj_parameters=kdj_config,
    )
    # 交易日与日历日并非一一对应，额外回溯保证节假日后仍可取得足量数据。
    start_day = target_day - timedelta(days=max(540, int(history_bars * 2.2)))
    request_parameters = {
        "secid": security.secid,
        "klt": "101",
        "fqt": "1",
        "beg": start_day.strftime("%Y%m%d"),
        "end": target_day.strftime("%Y%m%d"),
        "lmt": str(history_bars),
        "fields1": EASTMONEY_FIELDS1,
        "fields2": EASTMONEY_FIELDS2,
    }
    try:
        response = requests.get(
            EASTMONEY_DAILY_KLINE_URL,
            params=request_parameters,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        response.raise_for_status()
        history = parse_eastmoney_history(response.json())
        history = _limit_history_for_analysis(
            history, target_day=target_day, history_bars=history_bars
        )
        return _validate_analysis_history(
            history,
            security,
            kdj_parameters=kdj_config,
        )
    except (requests.RequestException, StockHistoryError, TypeError, ValueError, AttributeError) as primary_error:
        try:
            return _fetch_tencent_adjusted_daily_history(
                security,
                target_day=target_day,
                start_day=start_day,
                history_bars=history_bars,
                kdj_parameters=kdj_config,
            )
        except StockHistoryError as fallback_error:
            raise StockHistoryError(
                f"获取 {security.code} 历史日线失败：东方财富主源失败：{primary_error}；"
                f"腾讯前复权日 K 回退失败：{fallback_error}"
            ) from fallback_error


def calculate_kdj(
    history: pd.DataFrame,
    *,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """按国内常用 SMA 递推计算指定参数的 KDJ。"""

    parameters = _resolve_kdj_parameters(kdj_parameters)
    result = history.copy()
    rolling_low = result["low"].rolling(
        parameters.rsv_period,
        min_periods=parameters.rsv_period,
    ).min()
    rolling_high = result["high"].rolling(
        parameters.rsv_period,
        min_periods=parameters.rsv_period,
    ).max()
    price_range = rolling_high - rolling_low
    result["kdj_rsv"] = ((result["close"] - rolling_low) / price_range * 100.0).where(
        price_range.ne(0.0)
    )

    previous_k = 50.0
    previous_d = 50.0
    k_values: list[float] = []
    d_values: list[float] = []
    for rsv_value in result["kdj_rsv"]:
        if pd.isna(rsv_value):
            k_values.append(float("nan"))
            d_values.append(float("nan"))
            continue
        current_k = (
            (parameters.k_smoothing_period - 1) * previous_k + float(rsv_value)
        ) / parameters.k_smoothing_period
        current_d = (
            (parameters.d_smoothing_period - 1) * previous_d + current_k
        ) / parameters.d_smoothing_period
        k_values.append(current_k)
        d_values.append(current_d)
        previous_k = current_k
        previous_d = current_d

    result["kdj_k"] = pd.Series(k_values, index=result.index, dtype="float64")
    result["kdj_d"] = pd.Series(d_values, index=result.index, dtype="float64")
    result["kdj_j"] = 3.0 * result["kdj_k"] - 2.0 * result["kdj_d"]
    return result


def enrich_history_with_indicators(
    history: pd.DataFrame,
    *,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """在完整日线上计算 MA5、MA20、MACD 与 KDJ，避免展示切片重新预热。"""

    parameters = _resolve_kdj_parameters(kdj_parameters)
    result = normalize_ohlcv_frame(history)
    if len(result) < parameters.rsv_period:
        raise ValueError(
            f"可用于计算的日线不足 {parameters.rsv_period} 根，"
            f"无法计算 {_format_kdj_parameters(parameters)}。"
        )

    result["ma5"] = result["close"].rolling(5, min_periods=5).mean()
    result["ma20"] = result["close"].rolling(20, min_periods=20).mean()
    ema12 = result["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = result["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    result["macd_dif"] = ema12 - ema26
    result["macd_dea"] = result["macd_dif"].ewm(span=9, adjust=False, min_periods=9).mean()
    result["macd_histogram"] = 2.0 * (result["macd_dif"] - result["macd_dea"])
    return calculate_kdj(result, kdj_parameters=parameters)


def build_analysis_frame(
    history: pd.DataFrame,
    *,
    display_bars: int,
    kdj_parameters: KdjParameters | None = None,
) -> pd.DataFrame:
    """计算全量指标后截取显示窗口，确保展示的 KDJ 不因截断而重新预热。"""

    enriched = enrich_history_with_indicators(
        history,
        kdj_parameters=kdj_parameters,
    )
    try:
        bars = int(display_bars)
    except (TypeError, ValueError) as exc:
        raise ValueError("显示交易日数必须是正整数。") from exc
    if bars <= 0:
        raise ValueError("显示交易日数必须大于 0。")
    return enriched.tail(bars).reset_index(drop=True)
