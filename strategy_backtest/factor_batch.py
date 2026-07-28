"""严格等价的多日期滚动窗口因子计算。

页面的因子函数会在每一个截至日截取固定预热窗口，并从窗口开头重新
初始化 RSI、MACD 与 KDJ。这里将同一股票的多个窗口转置为
``预热行数 x 日期列`` 的 DataFrame。Pandas 对每列独立执行 ``rolling`` 和
``ewm``，因而保留原来的初始化语义，同时避免为每一个日期重复构造和计算
完整因子表。

这个模块不依赖 ``backtest_core``，由调用方传入页面常量和最终因子字典的
构造函数，以避免循环导入。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


FactorValueBuilder = Callable[[pd.DataFrame, int], dict[str, object]]


def _as_day(value: date | datetime | pd.Timestamp) -> date:
    """将日期输入统一为不含时区的 ``date``。"""

    return pd.Timestamp(value).date()


def _window_columns(
    history: pd.DataFrame,
    positions: Sequence[int],
    *,
    window_size: int,
) -> tuple[np.ndarray, dict[str, pd.DataFrame]]:
    """返回窗口在原历史中的位置和按日期分列的原始日线数据。"""

    position_array = np.asarray(positions, dtype=np.int64)
    starts = position_array - window_size + 1
    offsets = np.arange(window_size, dtype=np.int64)
    row_positions = starts[:, None] + offsets[None, :]

    columns: dict[str, pd.DataFrame] = {}
    for name in history.columns:
        values = history[name].to_numpy(copy=False)
        # 行表示窗口内的时间顺序，列表示各个信号日期。
        columns[name] = pd.DataFrame(values[row_positions].T)
    return row_positions, columns


def _kdj_lines_for_windows(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    *,
    rsv_period: int,
    k_smoothing_period: int,
    d_smoothing_period: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按页面的逐值 SMA 规则，平行计算每个窗口的 KDJ 序列。"""

    rolling_low = low.rolling(rsv_period, min_periods=rsv_period).min()
    rolling_high = high.rolling(rsv_period, min_periods=rsv_period).max()
    price_range = rolling_high - rolling_low
    rsv = ((close - rolling_low) / price_range * 100.0).where(price_range.ne(0.0))

    # 不能把 K/D 改成连续长历史序列：每一列都必须在窗口起点以 50 重新播种。
    k_values = np.full(rsv.shape, np.nan, dtype="float64")
    d_values = np.full(rsv.shape, np.nan, dtype="float64")
    for column_position, column_name in enumerate(rsv.columns):
        previous_k = 50.0
        previous_d = 50.0
        for row_position, rsv_value in enumerate(rsv[column_name]):
            if pd.isna(rsv_value):
                continue
            current_k = (
                (k_smoothing_period - 1) * previous_k + float(rsv_value)
            ) / k_smoothing_period
            current_d = (
                (d_smoothing_period - 1) * previous_d + current_k
            ) / d_smoothing_period
            k_values[row_position, column_position] = current_k
            d_values[row_position, column_position] = current_d
            previous_k = current_k
            previous_d = current_d

    k_line = pd.DataFrame(k_values, index=rsv.index, columns=rsv.columns)
    d_line = pd.DataFrame(d_values, index=rsv.index, columns=rsv.columns)
    j_line = 3.0 * k_line - 2.0 * d_line
    return rsv, k_line, d_line, j_line


def _global_candlestick_flags(history: pd.DataFrame, strategy_app: Any) -> dict[str, pd.Series]:
    """预计算无递推状态的单日 K 线风险标记。

    所有风险规则最长只回看 20 日，而因子窗口更长，故窗口末尾
    三日的结果与在完整历史上计算完全一致。
    """

    body = (history["close"] - history["open"]).abs()
    intraday_range = history["high"] - history["low"]
    body_top = pd.concat([history["open"], history["close"]], axis=1).max(axis=1)
    body_bottom = pd.concat([history["open"], history["close"]], axis=1).min(axis=1)
    upper_shadow = history["high"] - body_top
    lower_shadow = body_bottom - history["low"]
    valid_candle = (
        intraday_range.gt(0)
        & history["high"].ge(body_top)
        & history["low"].le(body_bottom)
        & upper_shadow.ge(0)
        & lower_shadow.ge(0)
    ).fillna(False)
    valid_range = intraday_range.where(valid_candle)
    body_ratio = body.div(valid_range)
    upper_shadow_ratio = upper_shadow.div(valid_range)
    lower_shadow_ratio = lower_shadow.div(valid_range)

    doji = valid_candle & body_ratio.le(
        strategy_app.CANDLESTICK_DOJI_MAX_BODY_RATIO
        + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
    ).fillna(False)
    inverted_t_doji = (
        doji
        & lower_shadow_ratio.le(
            strategy_app.CANDLESTICK_INVERTED_T_MAX_LOWER_SHADOW_RATIO
            + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & upper_shadow_ratio.ge(
            strategy_app.CANDLESTICK_INVERTED_T_MIN_UPPER_SHADOW_RATIO
            - strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )
    prior_high = history["high"].shift(1).rolling(
        strategy_app.CANDLESTICK_HANGING_MAN_LOOKBACK_BARS,
        min_periods=strategy_app.CANDLESTICK_HANGING_MAN_LOOKBACK_BARS,
    ).max()
    hanging_man = (
        valid_candle
        & prior_high.notna()
        & history["high"].ge(
            prior_high * strategy_app.CANDLESTICK_HANGING_MAN_HIGH_ZONE_RATIO
            - strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        )
        & body_ratio.le(
            strategy_app.CANDLESTICK_HANGING_MAN_MAX_BODY_RATIO
            + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & lower_shadow_ratio.ge(
            strategy_app.CANDLESTICK_HANGING_MAN_MIN_LOWER_SHADOW_RATIO
            - strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & upper_shadow_ratio.le(
            strategy_app.CANDLESTICK_HANGING_MAN_MAX_UPPER_SHADOW_RATIO
            + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )
    long_upper_shadow_bullish = (
        valid_candle
        & history["close"].gt(history["open"])
        & upper_shadow.gt(body + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE)
    ).fillna(False)
    daily_close_change = history["close"].pct_change(fill_method=None).mul(100.0)
    extreme_bullish = (
        valid_candle
        & history["close"].gt(history["open"])
        & daily_close_change.gt(
            strategy_app.CANDLESTICK_EXTREME_BULLISH_MIN_PCT_CHANGE
            + strategy_app.CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )
    return {
        "risk_doji_3d": doji,
        "risk_inverted_t_doji_3d": inverted_t_doji,
        "risk_hanging_man_3d": hanging_man,
        "risk_long_upper_shadow_bullish_3d": long_upper_shadow_bullish,
        "risk_extreme_bullish_3d": extreme_bullish,
    }


def _numeric_or_none(value: object) -> float | None:
    """与页面因子函数相同地序列化数值。"""

    return None if pd.isna(value) else float(value)


def _has_kdj_top_divergence(
    highs: np.ndarray,
    j_values: np.ndarray,
) -> bool:
    """逐项复现页面的最近两个确认高点顶背离判断。"""

    valid = ~(pd.isna(highs) | pd.isna(j_values))
    valid_highs = highs[valid].tolist()
    valid_j_values = j_values[valid].tolist()
    if len(valid_highs) < 3:
        return False

    peak_positions: list[int] = []
    for position in range(1, len(valid_highs) - 1):
        if (
            valid_highs[position] >= valid_highs[position - 1]
            and valid_highs[position] > valid_highs[position + 1]
        ):
            peak_positions.append(position)
    if len(peak_positions) < 2:
        return False

    previous_peak, latest_peak = peak_positions[-2:]
    return bool(
        valid_highs[latest_peak] > valid_highs[previous_peak]
        and valid_j_values[latest_peak] < valid_j_values[previous_peak]
    )


def _fast_factor_values_from_column(
    *,
    signal_day: date,
    column_position: int,
    row_positions: np.ndarray,
    windows: dict[str, pd.DataFrame],
    calculated_columns: dict[str, pd.DataFrame],
    candlestick_flags: dict[str, np.ndarray],
    strategy_app: Any,
) -> dict[str, object]:
    """从批量矩阵的一列构造与页面完全相同的因子字典。"""

    last_row = len(windows["close"]) - 1
    previous_row = last_row - 1

    def source(name: str, row: int = last_row) -> object:
        return windows[name].iat[row, column_position]

    def calculated(name: str, row: int = last_row) -> object:
        return calculated_columns[name].iat[row, column_position]

    latest_close = source("close")
    latest_open = source("open")
    latest_high = source("high")
    latest_low = source("low")
    latest_volume = source("volume")
    latest_amount = source("amount")
    latest_turnover = source("turnover")
    latest_ma5 = calculated("ma5")
    latest_ma20 = calculated("ma20")
    latest_volume_ma5 = calculated("volume_ma5")
    latest_rsi14 = calculated("rsi14")
    latest_macd_dif = calculated("macd_dif")
    latest_macd_dea = calculated("macd_dea")
    latest_macd_histogram = calculated("macd_histogram")
    latest_kdj_k = calculated("kdj_k")
    latest_kdj_d = calculated("kdj_d")
    latest_kdj_j = calculated("kdj_j")
    previous_ma5 = calculated("ma5", previous_row)

    if pd.isna(latest_ma20) or pd.isna(latest_rsi14) or pd.isna(latest_macd_dea):
        raise ValueError("技术指标预热不足。")

    prior_platform_high = calculated("prior_platform_high")
    platform_breakout = bool(
        pd.notna(prior_platform_high)
        and pd.notna(latest_close)
        and latest_close > prior_platform_high
    )
    platform_breakout_pct = (
        (latest_close - prior_platform_high) / prior_platform_high * 100.0
        if pd.notna(prior_platform_high) and prior_platform_high > 0
        else None
    )
    ma5_rising = bool(latest_ma5 > previous_ma5)
    volume_ratio = (
        latest_volume / latest_volume_ma5 if latest_volume_ma5 > 0 else pd.NA
    )
    macd_bullish = bool(latest_macd_dif > latest_macd_dea)
    macd_bearish = bool(latest_macd_dif < latest_macd_dea)
    macd_golden_cross = bool(calculated("macd_golden_cross"))
    macd_dead_cross = bool(calculated("macd_dead_cross"))

    macd_golden_cross_date: str | None = None
    macd_golden_cross_age: int | None = None
    completed_macd_row = last_row - int(strategy_app.MACD_GOLDEN_CROSS_OFFSET_BARS)
    minimum_macd_row = max(
        1,
        completed_macd_row
        - int(strategy_app.MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS)
        + 1,
    )
    has_macd_dead_cross_after = False
    for cross_row in range(completed_macd_row, minimum_macd_row - 1, -1):
        if bool(calculated("macd_dead_cross", cross_row)):
            has_macd_dead_cross_after = True
            continue
        if has_macd_dead_cross_after or not bool(
            calculated("macd_golden_cross", cross_row)
        ):
            continue
        macd_golden_cross_date = pd.Timestamp(
            source("date", cross_row)
        ).date().isoformat()
        macd_golden_cross_age = completed_macd_row - cross_row + 1
        break

    # 当前信号日计入回看窗口。若某次金叉之后已经出现死叉，则更早的金叉
    # 均不再有效；反向扫描的第一个满足健康条件的金叉即为最近有效信号。
    kdj_healthy_golden_cross_date: str | None = None
    kdj_healthy_golden_cross_age: int | None = None
    completed_row = last_row - int(strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_OFFSET_BARS)
    minimum_row = max(
        1,
        completed_row
        - int(strategy_app.MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE),
    )
    has_kdj_dead_cross_after = False
    for cross_row in range(completed_row, minimum_row - 1, -1):
        if bool(calculated("kdj_dead_cross", cross_row)):
            has_kdj_dead_cross_after = True
            continue
        if has_kdj_dead_cross_after or not bool(
            calculated("kdj_golden_cross", cross_row)
        ):
            continue
        prior_cross_row = cross_row - 1
        kdj_golden_cross_in_oversold = bool(
            calculated("kdj_k", cross_row) < strategy_app.KDJ_OVERSOLD_THRESHOLD
            and calculated("kdj_d", cross_row) < strategy_app.KDJ_OVERSOLD_THRESHOLD
            and calculated("kdj_j", cross_row) < strategy_app.KDJ_OVERSOLD_THRESHOLD
        )
        kdj_j_rising = bool(
            pd.notna(calculated("kdj_j", cross_row))
            and pd.notna(calculated("kdj_j", prior_cross_row))
            and calculated("kdj_j", cross_row)
            > calculated("kdj_j", prior_cross_row)
        )
        divergence_start = max(
            0,
            cross_row - int(strategy_app.KDJ_DIVERGENCE_LOOKBACK_BARS) + 1,
        )
        kdj_top_divergence = _has_kdj_top_divergence(
            windows["high"].iloc[divergence_start : cross_row + 1, column_position]
            .to_numpy(copy=False),
            calculated_columns["kdj_j"].iloc[
                divergence_start : cross_row + 1, column_position
            ].to_numpy(copy=False),
        )
        if (
            kdj_golden_cross_in_oversold
            and kdj_j_rising
            and not kdj_top_divergence
        ):
            kdj_healthy_golden_cross_date = pd.Timestamp(
                source("date", cross_row)
            ).date().isoformat()
            kdj_healthy_golden_cross_age = completed_row - cross_row
            break

    float_market_cap_yi = strategy_app.estimated_float_market_cap_yi(
        latest_amount,
        latest_turnover,
    )
    bias20 = (latest_close - latest_ma20) / latest_ma20 * 100.0
    intraday_range = latest_high - latest_low
    body_top = max(latest_open, latest_close)
    upper_shadow = max(0.0, latest_high - body_top)
    upper_shadow_ratio = (
        0.0
        if intraday_range <= 0
        else upper_shadow / intraday_range * 100.0
    )
    if pd.isna(intraday_range) or pd.isna(latest_close) or pd.isna(latest_low):
        close_position = None
    elif intraday_range > 0:
        close_position = (latest_close - latest_low) / intraday_range * 100.0
    elif latest_close == latest_high:
        close_position = 100.0
    else:
        close_position = None
    close_near_daily_high = bool(
        close_position is not None
        and close_position >= strategy_app.CLOSE_NEAR_DAILY_HIGH_THRESHOLD
    )
    rolling_high_60 = calculated("rolling_high_60")
    touches_60_day_resistance = bool(latest_close >= rolling_high_60 * 0.98)
    risk_start = max(
        0,
        last_row - int(strategy_app.CANDLESTICK_RISK_LOOKBACK_BARS) + 1,
    )
    recent_history_positions = row_positions[column_position, risk_start : last_row + 1]
    candlestick_risk_flags = {
        risk_key: bool(candlestick_flags[f"risk_{risk_key}"][recent_history_positions].any())
        for risk_key in strategy_app.CANDLESTICK_RISK_PATTERN_KEYS
    }

    return {
        "\u6570\u636e\u65e5\u671f": signal_day.isoformat(),
        "\u6536\u76d8\u4ef7": _numeric_or_none(latest_close),
        "MA5": _numeric_or_none(latest_ma5),
        "MA20": _numeric_or_none(latest_ma20),
        "\u7ad9\u4e0aMA5": bool(latest_close > latest_ma5),
        "\u7ad9\u4e0aMA20": bool(latest_close > latest_ma20),
        "MA5\u9ad8\u4e8eMA20": bool(latest_ma5 > latest_ma20),
        "MA5\u4e0a\u884c": ma5_rising,
        "\u524d20\u65e5\u5e73\u53f0\u6700\u9ad8\u4ef7": _numeric_or_none(prior_platform_high),
        "\u5e73\u53f0\u7a81\u7834\u5e45\u5ea6\uff08%\uff09": _numeric_or_none(
            platform_breakout_pct
        ),
        "\u6536\u76d8\u7a81\u7834\u524d20\u65e5\u5e73\u53f0": platform_breakout,
        "RSI14": _numeric_or_none(latest_rsi14),
        "MACD_DIF": _numeric_or_none(latest_macd_dif),
        "MACD_DEA": _numeric_or_none(latest_macd_dea),
        "MACD\u67f1": _numeric_or_none(latest_macd_histogram),
        "MACD\u591a\u5934": macd_bullish,
        "MACD\u7a7a\u5934": macd_bearish,
        "MACD\u91d1\u53c9": macd_golden_cross,
        "MACD\u6b7b\u53c9": macd_dead_cross,
        strategy_app.MACD_GOLDEN_CROSS_DATE_COLUMN: macd_golden_cross_date,
        strategy_app.MACD_GOLDEN_CROSS_AGE_COLUMN: macd_golden_cross_age,
        "KDJ_K(89,3,3)": _numeric_or_none(latest_kdj_k),
        "KDJ_D(89,3,3)": _numeric_or_none(latest_kdj_d),
        "KDJ_J(89,3,3)": _numeric_or_none(latest_kdj_j),
        "KDJ\u91d1\u53c9": bool(calculated("kdj_golden_cross")),
        "KDJ\u6b7b\u53c9": bool(calculated("kdj_dead_cross")),
        strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN: kdj_healthy_golden_cross_date,
        strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: kdj_healthy_golden_cross_age,
        strategy_app.KDJ_HEALTHY_GOLDEN_CROSS_NAME: (
            kdj_healthy_golden_cross_age is not None
        ),
        "\u5f53\u65e5\u6210\u4ea4\u91cf": _numeric_or_none(latest_volume),
        "5\u65e5\u5747\u91cf": _numeric_or_none(latest_volume_ma5),
        "\u91cf\u6bd4": _numeric_or_none(volume_ratio),
        "\u653e\u91cf": bool(
            not pd.isna(volume_ratio)
            and strategy_app.DEFAULT_VOLUME_RATIO_RANGE[0]
            <= volume_ratio
            <= strategy_app.DEFAULT_VOLUME_RATIO_RANGE[1]
        ),
        "\u5f53\u65e5\u6210\u4ea4\u989d": _numeric_or_none(latest_amount),
        "\u6362\u624b\u7387": _numeric_or_none(latest_turnover),
        "\u4f30\u7b97\u6d41\u901a\u5e02\u503c\uff08\u4ebf\u5143\uff09": float_market_cap_yi,
        "\u5f53\u65e5\u6da8\u8dcc\u5e45": _numeric_or_none(source("pct_change")),
        "\u632f\u5e45": _numeric_or_none(source("amplitude")),
        "BIAS20": _numeric_or_none(bias20),
        "\u4e0a\u5f71\u7ebf\u6bd4\u4f8b": _numeric_or_none(upper_shadow_ratio),
        "\u6536\u76d8\u65e5\u5185\u4f4d\u7f6e\uff08%\uff09": _numeric_or_none(
            close_position
        ),
        "\u6536\u76d8\u4f4d\u4e8e\u65e5\u5185\u9ad8\u4f4d": close_near_daily_high,
        "60\u65e5\u6700\u9ad8\u4ef7": _numeric_or_none(rolling_high_60),
        "\u89e6\u53ca60\u65e5\u9ad8\u70b9\u538b\u529b": touches_60_day_resistance,
        **{
            strategy_app.CANDLESTICK_RISK_FACTOR_COLUMNS[key]: hit
            for key, hit in candlestick_risk_flags.items()
        },
    }


def calculate_factor_values_for_dates(
    history: pd.DataFrame,
    signal_dates: Sequence[date | datetime | pd.Timestamp],
    *,
    strategy_app: Any,
    value_builder: FactorValueBuilder | None = None,
) -> dict[str, dict[str, object]]:
    """计算一只股票多个截至日的完整因子，严格匹配页面的预热窗口语义。"""

    window_size = int(strategy_app.INDICATOR_WARMUP_BARS)
    min_required_bars = int(strategy_app.MIN_REQUIRED_BARS)
    date_positions = {
        _as_day(value): position for position, value in enumerate(history["date"])
    }
    selected_days: list[date] = []
    selected_positions: list[int] = []
    seen_days: set[date] = set()
    for raw_day in signal_dates:
        signal_day = _as_day(raw_day)
        if signal_day in seen_days:
            continue
        seen_days.add(signal_day)
        position = date_positions.get(signal_day)
        if position is None or position + 1 < min_required_bars:
            continue
        selected_days.append(signal_day)
        selected_positions.append(position)
    if not selected_positions:
        return {}

    row_positions, windows = _window_columns(
        history,
        selected_positions,
        window_size=window_size,
    )
    close = windows["close"]
    high = windows["high"]
    low = windows["low"]
    volume = windows["volume"]

    ma5 = close.rolling(5, min_periods=5).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    volume_ma5 = volume.shift(1).rolling(5, min_periods=5).mean()

    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    average_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = average_gain / average_loss
    rsi14 = 100.0 - 100.0 / (1.0 + relative_strength)
    rsi14 = rsi14.mask((average_loss == 0) & (average_gain > 0), 100.0)
    rsi14 = rsi14.mask((average_gain == 0) & (average_loss > 0), 0.0)

    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_dif = ema12 - ema26
    macd_dea = macd_dif.ewm(span=9, adjust=False, min_periods=9).mean()
    macd_histogram = 2.0 * (macd_dif - macd_dea)
    macd_golden_cross = (
        macd_dif.gt(macd_dea) & macd_dif.shift(1).le(macd_dea.shift(1))
    ).fillna(False)
    macd_dead_cross = (
        macd_dif.lt(macd_dea) & macd_dif.shift(1).ge(macd_dea.shift(1))
    ).fillna(False)

    kdj_rsv, kdj_k, kdj_d, kdj_j = _kdj_lines_for_windows(
        close,
        high,
        low,
        rsv_period=int(strategy_app.KDJ_RSV_PERIOD),
        k_smoothing_period=int(strategy_app.KDJ_K_SMOOTHING_PERIOD),
        d_smoothing_period=int(strategy_app.KDJ_D_SMOOTHING_PERIOD),
    )
    kdj_golden_cross = (
        kdj_k.gt(kdj_d) & kdj_k.shift(1).le(kdj_d.shift(1))
    ).fillna(False)
    kdj_dead_cross = (
        kdj_k.lt(kdj_d) & kdj_k.shift(1).ge(kdj_d.shift(1))
    ).fillna(False)
    prior_platform_high = high.shift(1).rolling(
        int(strategy_app.PLATFORM_BREAKOUT_LOOKBACK_BARS),
        min_periods=int(strategy_app.PLATFORM_BREAKOUT_LOOKBACK_BARS),
    ).max()
    rolling_high_60 = high.shift(1).rolling(60, min_periods=60).max()
    candlestick_flags = _global_candlestick_flags(history, strategy_app)

    calculated_columns = {
        "ma5": ma5,
        "ma20": ma20,
        "volume_ma5": volume_ma5,
        "rsi14": rsi14,
        "macd_dif": macd_dif,
        "macd_dea": macd_dea,
        "macd_histogram": macd_histogram,
        "macd_golden_cross": macd_golden_cross,
        "macd_dead_cross": macd_dead_cross,
        "kdj_rsv": kdj_rsv,
        "kdj_k": kdj_k,
        "kdj_d": kdj_d,
        "kdj_j": kdj_j,
        "kdj_golden_cross": kdj_golden_cross,
        "kdj_dead_cross": kdj_dead_cross,
        "prior_platform_high": prior_platform_high,
        "rolling_high_60": rolling_high_60,
    }

    candlestick_flag_arrays = {
        name: values.to_numpy(copy=False) for name, values in candlestick_flags.items()
    }
    values_by_date: dict[str, dict[str, object]] = {}
    for column_position, signal_day in enumerate(selected_days):
        if value_builder is None:
            values_by_date[signal_day.isoformat()] = _fast_factor_values_from_column(
                signal_day=signal_day,
                column_position=column_position,
                row_positions=row_positions,
                windows=windows,
                calculated_columns=calculated_columns,
                candlestick_flags=candlestick_flag_arrays,
                strategy_app=strategy_app,
            )
            continue

        # 这个可选路径供测试或外部比对时复用既有字典构造器。
        window = history.iloc[row_positions[column_position]].reset_index(drop=True).copy()
        for name, values in calculated_columns.items():
            window[name] = values.iloc[:, column_position].to_numpy(copy=False)
        for name, values in candlestick_flags.items():
            window[name] = values.iloc[row_positions[column_position]].to_numpy(copy=False)
        values_by_date[signal_day.isoformat()] = value_builder(window, window_size - 1)
    return values_by_date
