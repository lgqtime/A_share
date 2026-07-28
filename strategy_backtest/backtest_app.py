"""深市主板策略回测的 Streamlit 交互页面。

运行示例：
    uv run --locked streamlit run strategy_backtest/backtest_app.py
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import pandas as pd
import streamlit as st


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:  # 支持直接运行和包导入两种方式。
    from strategy_backtest import backtest_core as core
    from strategy_backtest import szse_quant_app as strategy_app
except ImportError:  # pragma: no cover - `streamlit run strategy_backtest/backtest_app.py`。
    import backtest_core as core
    import szse_quant_app as strategy_app


DEFAULT_RETURNS_WORKBOOK = (
    PROJECT_DIR
    / "strategy_backtest"
    / "outputs"
    / "input_data"
    / "深市主板每日涨跌幅_2025-10-29_2026-07-27.xlsx"
)
DEFAULT_STOCK_POOL = PROJECT_DIR / "深交所数据.xlsx"

RESULT_STATE_KEY = "backtest_results"
ERROR_STATE_KEY = "backtest_error"
DATE_RANGE_STATE_KEY = "backtest_date_range"
CACHE_HOURS_STATE_KEY = "backtest_cache_hours"
FORCE_REFRESH_STATE_KEY = "backtest_force_refresh"
WORKERS_STATE_KEY = "backtest_workers"
INTERVAL_STATE_KEY = "backtest_request_interval"
FILTER_DEFAULTS_VERSION_STATE_KEY = "backtest_filter_defaults_version"
FILTER_DEFAULTS_VERSION = 8
RETURN_DATA_CACHE_VERSION = 2

# Existing browser sessions retain widget values. Only migrate former default
# ranges, so values the user adjusted manually are left untouched.
LEGACY_FILTER_DEFAULT_RANGES: dict[str, tuple[tuple[float, float], ...]] = {
    "szse_quant_filter_rsi_range": ((45.0, 75.0), (47.5, 61.6), (49.1, 62.6)),
    "szse_quant_filter_turnover_range": ((5.2, 10.8), (5.4, 10.7), (5.6, 10.7)),
    "szse_quant_filter_volume_ratio_range": ((1.5, 8.0), (1.8, 3.8), (1.8, 4.1)),
    "szse_quant_filter_pct_change_range": ((3.0, 5.0), (-2.7, 10.1), (-1.0, 10.1)),
    "szse_quant_filter_kdj_healthy_golden_cross_age_range": ((2, 3),),
}
LEGACY_FILTER_DEFAULT_BOOLEANS: dict[str, bool] = {
    "szse_quant_filter_positive_change": True,
    "szse_quant_filter_pct_change_in_range": False,
}

DISPLAY_RESULT_COLUMNS = (
    "预测日期",
    "预测结果",
    "涨跌幅（%）",
    "总收益率（%）",
    "公司名称",
    "股票代码",
)
AUTO_EXCLUDED_PROBLEM_TYPES = frozenset(
    {
        "长历史获取失败",
        "历史预热不足",
        "因子预热不足",
        "策略预热不足",
    }
)


def _backtest_key(snapshot_key: str) -> str:
    """将快照应用的控件键映射为本页面专用的会话键。"""

    prefix = "szse_quant_"
    if not snapshot_key.startswith(prefix):
        raise ValueError(f"无法映射快照控件键：{snapshot_key}")
    return f"backtest_{snapshot_key.removeprefix(prefix)}"


def _clear_results() -> None:
    st.session_state.pop(RESULT_STATE_KEY, None)
    st.session_state.pop(ERROR_STATE_KEY, None)


def _apply_preset(preset_name: str) -> None:
    """以快照定义的配置更新本页面专用控件状态。"""

    if preset_name == "platform_breakout":
        source_settings = strategy_app.platform_breakout_screening_settings()
    elif preset_name == "default":
        source_settings = strategy_app.default_screening_settings()
    else:
        raise ValueError(f"未知回测预设：{preset_name}")

    for snapshot_key, value in source_settings.items():
        st.session_state[_backtest_key(snapshot_key)] = deepcopy(value)
    _clear_results()


def _apply_optimized_parameter_overrides() -> None:
    """Apply optimizer-owned ranges without changing this page's other controls."""

    for snapshot_key, value in strategy_app.load_optimized_parameter_overrides().items():
        st.session_state[_backtest_key(snapshot_key)] = deepcopy(value)
    _clear_results()


def _migrate_legacy_filter_defaults() -> None:
    """Upgrade former range defaults in an existing browser session once."""

    if st.session_state.get(FILTER_DEFAULTS_VERSION_STATE_KEY) == FILTER_DEFAULTS_VERSION:
        return

    current_defaults = strategy_app.default_screening_settings()
    for snapshot_key, legacy_values in LEGACY_FILTER_DEFAULT_RANGES.items():
        widget_key = _backtest_key(snapshot_key)
        current_value = st.session_state.get(widget_key)
        if (
            isinstance(current_value, (tuple, list))
            and tuple(current_value) in legacy_values
        ):
            st.session_state[widget_key] = deepcopy(current_defaults[snapshot_key])

    for snapshot_key, legacy_value in LEGACY_FILTER_DEFAULT_BOOLEANS.items():
        widget_key = _backtest_key(snapshot_key)
        if st.session_state.get(widget_key) == legacy_value:
            st.session_state[widget_key] = bool(current_defaults[snapshot_key])

    st.session_state[FILTER_DEFAULTS_VERSION_STATE_KEY] = FILTER_DEFAULTS_VERSION


def _backtest_date_options(return_data: core.ReturnData) -> tuple[date, ...]:
    """Expose the final validation date so the full signal window is selectable."""

    signal_dates = return_data.signal_dates
    if not signal_dates:
        return ()
    final_validation_date = return_data.next_trade_dates[signal_dates[-1]]
    return tuple((*signal_dates, final_validation_date))


def _initialize_widget_state(return_data: core.ReturnData) -> None:
    """初始化独立回测页面的控件状态，不影响主应用。"""

    signal_dates = return_data.signal_dates
    if not signal_dates:
        raise core.BacktestDataError("收益文件中没有可用的选股日期。")
    for snapshot_key, value in strategy_app.default_screening_settings().items():
        st.session_state.setdefault(_backtest_key(snapshot_key), deepcopy(value))
    _migrate_legacy_filter_defaults()

    date_options = _backtest_date_options(return_data)
    first_available_date = signal_dates[0]
    last_available_date = date_options[-1]
    default_range = (first_available_date, last_available_date)
    selected_range = st.session_state.get(DATE_RANGE_STATE_KEY)
    if (
        not isinstance(selected_range, (tuple, list))
        or len(selected_range) != 2
        or not all(isinstance(value, date) for value in selected_range)
        or selected_range[0] < first_available_date
        or selected_range[1] > last_available_date
        or selected_range[0] > selected_range[1]
    ):
        st.session_state[DATE_RANGE_STATE_KEY] = default_range

    st.session_state.setdefault(CACHE_HOURS_STATE_KEY, float(core.DEFAULT_CACHE_HOURS))
    st.session_state.setdefault(FORCE_REFRESH_STATE_KEY, False)
    st.session_state.setdefault(WORKERS_STATE_KEY, int(core.DEFAULT_WORKERS))
    st.session_state.setdefault(
        INTERVAL_STATE_KEY, float(core.DEFAULT_REQUEST_INTERVAL_SECONDS)
    )
    st.session_state.setdefault(
        _backtest_key("szse_quant_filter_kdj_healthy_golden_cross_age_range"),
        tuple(strategy_app.DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE),
    )


@st.cache_data(show_spinner=False, max_entries=4)
def _load_return_data(
    workbook_text: str,
    modified_ns: int,
    cache_version: int,
) -> core.ReturnData:
    """缓存小型且可序列化的严格次日收益映射。"""

    del modified_ns, cache_version
    return core.load_strict_next_day_returns(Path(workbook_text))


@st.cache_data(show_spinner=False, max_entries=4)
def _load_companies(workbook_text: str, modified_ns: int) -> pd.DataFrame:
    """缓存股票池表；历史与因子仍由磁盘缓存负责。"""

    del modified_ns
    return strategy_app.load_mainboard_companies(Path(workbook_text))


def _selected_return_data(
    return_data: core.ReturnData,
    start_date: date,
    end_date: date,
) -> core.ReturnData:
    """构建首日选股、末日验证的严格次日收益视图。"""

    signal_dates = tuple(
        day
        for day in return_data.signal_dates
        if start_date <= day and return_data.next_trade_dates[day] <= end_date
    )
    if not signal_dates:
        raise core.BacktestDataError("所选日期范围没有可用的选股日及对应验证日。")
    selected_days = set(signal_dates)
    strict_returns = {
        (signal_day, code): change_pct
        for (signal_day, code), change_pct in return_data.strict_returns.items()
        if signal_day in selected_days
    }
    return core.ReturnData(
        signal_dates=signal_dates,
        next_trade_dates={day: return_data.next_trade_dates[day] for day in signal_dates},
        strict_returns=strict_returns,
        failed_return_codes=return_data.failed_return_codes,
    )


def _exclude_return_failure_companies(
    companies: pd.DataFrame,
    failed_return_codes: frozenset[str],
) -> pd.DataFrame:
    """从回测股票池剔除收益文件已明确记录为失败的股票。"""

    if not failed_return_codes:
        return companies
    if "股票代码" not in companies.columns:
        raise core.BacktestDataError("股票池缺少“股票代码”列。")
    normalized_codes = companies["股票代码"].map(core._as_six_digit_code)
    return companies.loc[~normalized_codes.isin(failed_return_codes)].copy()


def _render_settings_form(
    signal_dates: Sequence[date],
) -> tuple[bool, dict[str, object]]:
    """渲染与快照应用一致的策略和风险控件。"""

    with st.form("backtest_settings_form", border=True):
        st.subheader("回测设置")
        selected_date_range = st.date_input(
            "回测选股日期范围",
            min_value=signal_dates[0],
            max_value=signal_dates[-1],
            format="YYYY-MM-DD",
            key=DATE_RANGE_STATE_KEY,
        )
        date_range_complete = (
            isinstance(selected_date_range, tuple)
            and len(selected_date_range) == 2
        )
        if date_range_complete:
            start_date, end_date = selected_date_range
        else:
            start_date = end_date = signal_dates[0]

        st.divider()
        st.header("策略预设")
        preset_columns = st.columns([3, 1, 2], vertical_alignment="center")
        with preset_columns[0]:
            st.form_submit_button(
                "低位企稳后的平台突破",
                type="primary",
                width="stretch",
                on_click=_apply_preset,
                args=("platform_breakout",),
            )
        with preset_columns[1]:
            st.form_submit_button(
                "默认",
                width="stretch",
                on_click=_apply_preset,
                args=("default",),
            )
        with preset_columns[2]:
            st.form_submit_button(
                "加载优化参数",
                width="stretch",
                on_click=_apply_optimized_parameter_overrides,
            )

        st.divider()
        st.header("趋势因子")
        selected: dict[str, bool] = {
            "above_ma5": st.checkbox(
                "站上5日线", key=_backtest_key("szse_quant_filter_above_ma5")
            ),
            "above_ma20": st.checkbox(
                "站上20日线", key=_backtest_key("szse_quant_filter_above_ma20")
            ),
            "ma5_above_ma20": st.checkbox(
                "MA5高于MA20", key=_backtest_key("szse_quant_filter_ma5_above_ma20")
            ),
        }

        st.header("平台突破因子")
        selected.update(
            {
                "platform_breakout_20d": st.checkbox(
                    "收盘突破前20日平台",
                    key=_backtest_key("szse_quant_filter_platform_breakout_20d"),
                    help="收盘价严格高于此前20个实际交易日的最高价，不将当天高点计入平台。",
                ),
                "ma5_rising": st.checkbox(
                    "MA5上行", key=_backtest_key("szse_quant_filter_ma5_rising")
                ),
                "close_near_daily_high": st.checkbox(
                    "收盘位于日内高位（上30%）",
                    key=_backtest_key("szse_quant_filter_close_near_daily_high"),
                ),
            }
        )

        st.header("动能因子")
        selected.update(
            {
                "rsi_in_range": st.checkbox(
                    "RSI区间", key=_backtest_key("szse_quant_filter_rsi_in_range")
                ),
                "macd_bullish": st.checkbox(
                    "MACD多头（DIF>DEA）",
                    key=_backtest_key("szse_quant_filter_macd_bullish"),
                ),
                "macd_golden_cross": st.checkbox(
                    "最近N日MACD金叉（1分）",
                    key=_backtest_key("szse_quant_filter_macd_golden_cross"),
                    help="仅检查当前选股日前已完成的交易日；金叉后在该窗口内出现死叉时失效。",
                ),
                "macd_bearish": st.checkbox(
                    "MACD空头（DIF<DEA）",
                    key=_backtest_key("szse_quant_filter_macd_bearish"),
                ),
                "macd_dead_cross": st.checkbox(
                    "当日MACD死叉",
                    key=_backtest_key("szse_quant_filter_macd_dead_cross"),
                ),
                "macd_dea_minus_dif_high": st.checkbox(
                    "MACD红线-蓝线区间（DEA-DIF）",
                    key=_backtest_key(
                        "szse_quant_filter_macd_dea_minus_dif_high"
                    ),
                    help="当日 DEA-DIF 位于所设区间内（含边界）时，作为一项 1 分的得分条件。",
                ),
                "kdj_healthy_golden_cross_3d": st.checkbox(
                    "最近N日 KDJ 金叉且状态良好（1.5分）",
                    key=_backtest_key(
                        "szse_quant_filter_kdj_healthy_golden_cross_3d"
                    ),
                    help=(
                        "按距今交易日区间筛选，0 表示当前选股日，边界包含在内。金叉日 K、D、J "
                        "均低于 20、J 线上行、无 KDJ 顶背离，且金叉后未出现死叉。"
                    ),
                ),
            }
        )
        rsi_range = tuple(
            float(value)
            for value in st.slider(
                "RSI区间",
                min_value=strategy_app.RSI_MIN_VALUE,
                max_value=strategy_app.RSI_MAX_VALUE,
                step=0.1,
                key=_backtest_key("szse_quant_filter_rsi_range"),
                help=(
                    "当日 RSI14 位于该区间内（含边界）时满足该得分条件；"
                    f"默认值为 {strategy_app.DEFAULT_RSI_RANGE[0]:g} 至 "
                    f"{strategy_app.DEFAULT_RSI_RANGE[1]:g}。"
                ),
            )
        )
        kdj_healthy_golden_cross_age_range = tuple(
            int(value)
            for value in st.slider(
                "KDJ金叉出现时间（距今交易日）",
                min_value=strategy_app.MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE,
                max_value=strategy_app.MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE,
                step=1,
                key=_backtest_key(
                    "szse_quant_filter_kdj_healthy_golden_cross_age_range"
                ),
                help="0 表示当前选股日，区间包含边界；默认值为 1 至 3。",
            )
        )
        macd_golden_cross_lookback_days = int(
            st.number_input(
                "MACD金叉回看天数（交易日）",
                min_value=1,
                max_value=strategy_app.MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS,
                step=1,
                key=_backtest_key("szse_quant_filter_macd_golden_cross_lookback_days"),
                help="不包含当前选股日；N=1 表示仅检查昨日，默认 3。",
            )
        )
        macd_dea_minus_dif_range = tuple(
            float(value)
            for value in st.slider(
                "MACD红线-蓝线区间（DEA-DIF）",
                min_value=strategy_app.MACD_DEA_MINUS_DIF_MIN_THRESHOLD,
                max_value=strategy_app.MACD_DEA_MINUS_DIF_MAX_THRESHOLD,
                step=0.01,
                key=_backtest_key("szse_quant_filter_macd_dea_minus_dif_range"),
                help="DEA-DIF 位于区间内（含边界）时满足本计分因子；默认值为 0.1 至 0.2。",
            )
        )

        st.header("量能因子")
        selected.update(
            {
                "volume_breakout": st.checkbox(
                    "放量（量比区间）",
                    key=_backtest_key("szse_quant_filter_volume_breakout"),
                ),
                "amount_at_least_100m": st.checkbox(
                    "日成交额≥1亿元",
                    key=_backtest_key("szse_quant_filter_amount_at_least_100m"),
                ),
                "turnover_in_range": st.checkbox(
                    "换手率区间",
                    key=_backtest_key("szse_quant_filter_turnover_in_range"),
                ),
            }
        )
        volume_ratio_range = tuple(
            float(value)
            for value in st.slider(
                "量比区间",
                min_value=strategy_app.VOLUME_RATIO_MIN_THRESHOLD,
                max_value=strategy_app.VOLUME_RATIO_MAX_THRESHOLD,
                step=0.1,
                key=_backtest_key("szse_quant_filter_volume_ratio_range"),
                help=(
                    "量比位于区间内（含边界）时满足放量条件；"
                    f"默认值为 {strategy_app.DEFAULT_VOLUME_RATIO_RANGE[0]:g} 至 "
                    f"{strategy_app.DEFAULT_VOLUME_RATIO_RANGE[1]:g}。"
                ),
            )
        )
        turnover_range = tuple(
            float(value)
            for value in st.slider(
                "换手率区间（%）",
                min_value=0.0,
                max_value=100.0,
                step=0.1,
                key=_backtest_key("szse_quant_filter_turnover_range"),
            )
        )

        st.header("市值因子")
        selected.update(
            {
                "float_market_cap_in_range": st.checkbox(
                    "流通市值区间",
                    key=_backtest_key(
                        "szse_quant_filter_float_market_cap_in_range"
                    ),
                    help="按同日成交额和换手率换算为成交均价口径；缺少任一数据时不满足此条件。",
                ),
            }
        )
        float_market_cap_range_yi = tuple(
            float(value)
            for value in st.slider(
                "流通市值区间（亿元）",
                min_value=strategy_app.FLOAT_MARKET_CAP_MIN_YI,
                max_value=strategy_app.FLOAT_MARKET_CAP_MAX_YI,
                step=1.0,
                key=_backtest_key("szse_quant_filter_float_market_cap_range_yi"),
            )
        )

        st.header("波动因子")
        selected.update(
            {
                "positive_change": st.checkbox(
                    "当日上涨", key=_backtest_key("szse_quant_filter_positive_change")
                ),
                "pct_change_in_range": st.checkbox(
                    "涨幅区间",
                    key=_backtest_key("szse_quant_filter_pct_change_in_range"),
                ),
                "amplitude_high": st.checkbox(
                    "振幅高于阈值",
                    key=_backtest_key("szse_quant_filter_amplitude_high"),
                ),
            }
        )
        pct_change_range = tuple(
            float(value)
            for value in st.slider(
                "涨幅区间（%）",
                min_value=-20.0,
                max_value=20.0,
                step=0.1,
                key=_backtest_key("szse_quant_filter_pct_change_range"),
            )
        )
        amplitude_threshold = float(
            st.number_input(
                "振幅阈值（%）",
                min_value=0.0,
                max_value=30.0,
                step=0.1,
                key=_backtest_key("szse_quant_filter_amplitude_threshold"),
            )
        )

        max_score = strategy_app.maximum_score(selected)
        selected_score_count = sum(
            bool(selected.get(key)) for key in strategy_app.SCORING_INDICATOR_KEYS
        )
        st.caption(f"当前勾选 {selected_score_count} 项得分指标，满分 {max_score:g} 分。")
        require_all = st.checkbox(
            "仅保留满足全部勾选条件的股票",
            key=_backtest_key("szse_quant_filter_require_all"),
            help="默认模式为至少满足一个条件即可参与排名；普通条件每项 1 分，KDJ 健康金叉为 1.5 分。",
        )

        st.divider()
        st.header("风险过滤（命中即剔除）")
        candlestick_patterns = st.multiselect(
            "K线形状",
            options=strategy_app.CANDLESTICK_RISK_PATTERN_KEYS,
            format_func=strategy_app.CANDLESTICK_RISK_PATTERN_LABELS.__getitem__,
            key=_backtest_key("szse_quant_risk_candlestick_patterns"),
            help=(
                "检查最近 3 个实际交易日，包含当天。十字星：实体不超过振幅 10%；"
                "倒T字星：上影至少占振幅 60%、下影不超过 10%；"
                "吊颈线：处于前20日高位且下影至少占振幅 50%；"
                "长上影阳线：上影超过实体；极端大阳线：收盘上涨超过 8%。"
            ),
        )
        selected_risks = {
            "bias_high": st.checkbox(
                "剔除 BIAS>10%（偏离20日线过远）",
                key=_backtest_key("szse_quant_risk_bias_high"),
            ),
            "upper_shadow": st.checkbox(
                "剔除上影线>30%且涨幅<3%（冲高回落）",
                key=_backtest_key("szse_quant_risk_upper_shadow"),
            ),
            "resistance_60_day": st.checkbox(
                "剔除触及60日高点压力区（距最高价2%内）",
                key=_backtest_key("szse_quant_risk_resistance_60_day"),
            ),
            **{
                risk_key: risk_key in candlestick_patterns
                for risk_key in strategy_app.CANDLESTICK_RISK_PATTERN_KEYS
            },
        }

        st.divider()
        st.header("数据与缓存")
        cache_hours = float(
            st.number_input(
                "行情缓存有效小时数",
                min_value=0.0,
                max_value=720.0,
                step=1.0,
                key=CACHE_HOURS_STATE_KEY,
            )
        )
        force_refresh = st.checkbox(
            "忽略缓存，重新请求行情", key=FORCE_REFRESH_STATE_KEY
        )
        workers = int(
            st.slider(
                "并发请求数",
                min_value=1,
                max_value=core.MAX_WORKERS,
                key=WORKERS_STATE_KEY,
            )
        )
        request_interval = float(
            st.number_input(
                "请求最小间隔（秒）",
                min_value=0.25,
                max_value=3.0,
                step=0.05,
                key=INTERVAL_STATE_KEY,
                help="所有工作线程共用此限速。较低的值会加快首次扫描，但可能触发公开接口风控。",
            )
        )
        run_clicked = st.form_submit_button("运行回测", type="primary", width="stretch")
        if run_clicked and not date_range_complete:
            st.error("请选择完整的回测起止日期。")

    settings: dict[str, object] = {
        "start_date": start_date,
        "end_date": end_date,
        "selected": selected,
        "selected_risks": selected_risks,
        "turnover_range": turnover_range,
        "float_market_cap_range_yi": float_market_cap_range_yi,
        "pct_change_range": pct_change_range,
        "amplitude_threshold": amplitude_threshold,
        "rsi_range": rsi_range,
        "macd_golden_cross_lookback_days": macd_golden_cross_lookback_days,
        "kdj_healthy_golden_cross_age_range": kdj_healthy_golden_cross_age_range,
        "macd_dea_minus_dif_range": macd_dea_minus_dif_range,
        "volume_ratio_range": volume_ratio_range,
        "require_all": require_all,
        "cache_hours": cache_hours,
        "force_refresh": force_refresh,
        "workers": workers,
        "request_interval": request_interval,
    }
    return run_clicked and date_range_complete, settings


def _build_data_problems(
    history_errors: pd.DataFrame,
    factor_errors: pd.DataFrame,
    daily_results: pd.DataFrame,
) -> pd.DataFrame:
    """合并历史、因子和严格次日收益的可见问题明细。"""

    columns = list(core.HISTORY_ERROR_COLUMNS)
    problems = [
        frame.reindex(columns=columns)
        for frame in (history_errors, factor_errors)
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if "状态" in daily_results and "选中股票代码" in daily_results:
        missing_returns = daily_results.loc[
            daily_results["状态"].eq("次日收益缺失"),
            ["选股日期", "选中股票代码", "选中股票名称", "说明"],
        ].copy()
        if not missing_returns.empty:
            missing_returns.insert(0, "序号", pd.NA)
            missing_returns = missing_returns.rename(
                columns={
                    "选中股票代码": "股票代码",
                    "选中股票名称": "股票名称",
                    "选股日期": "选股日期",
                    "说明": "失败原因",
                }
            )
            missing_returns.insert(4, "问题类型", "严格次日收益缺失")
            problems.append(missing_returns.reindex(columns=columns))
    if not problems:
        return pd.DataFrame(columns=columns)
    return pd.concat(problems, ignore_index=True)


def _run_backtest(
    return_data: core.ReturnData,
    companies: pd.DataFrame,
    settings: Mapping[str, object],
    *,
    history_progress: Callable[[int, int, str, int, int, int], None] | None = None,
    factor_progress: Callable[[int, int, str, int, int, int], None] | None = None,
) -> dict[str, object]:
    """执行一次指定日期范围的股票池回测；只在调用方显式提交时运行。"""

    selected_return_data = _selected_return_data(
        return_data,
        settings["start_date"],  # type: ignore[arg-type]
        settings["end_date"],  # type: ignore[arg-type]
    )
    backtest_companies = _exclude_return_failure_companies(
        companies,
        selected_return_data.failed_return_codes,
    )
    if backtest_companies.empty:
        raise core.BacktestDataError("收益文件失败明细已剔除全部股票池。")
    # 始终用收益文件的完整可用区间取长历史，以复用已有全区间磁盘缓存。
    full_first_signal_date = return_data.signal_dates[0]
    full_last_market_date = return_data.next_trade_dates[return_data.signal_dates[-1]]
    histories, history_errors, history_summary, cache_key = core.collect_full_histories(
        backtest_companies,
        first_signal_date=full_first_signal_date,
        end_date=full_last_market_date,
        cache_hours=float(settings["cache_hours"]),
        force_refresh=bool(settings["force_refresh"]),
        workers=int(settings["workers"]),
        request_interval_seconds=float(settings["request_interval"]),
        timeout_seconds=core.DEFAULT_TIMEOUT_SECONDS,
        progress_callback=history_progress,
    )
    factors_by_day, day_stats, factor_errors = core.collect_all_factor_rows_by_day(
        backtest_companies,
        histories,
        selected_return_data.signal_dates,
        cache_key=cache_key,
        cache_hours=float(settings["cache_hours"]),
        progress_callback=factor_progress,
    )
    daily_results, summary = core.evaluate_strategy(
        selected_return_data,
        factors_by_day,
        day_stats,
        selected=settings["selected"],  # type: ignore[arg-type]
        selected_risks=settings["selected_risks"],  # type: ignore[arg-type]
        turnover_range=settings["turnover_range"],  # type: ignore[arg-type]
        float_market_cap_range_yi=settings["float_market_cap_range_yi"],  # type: ignore[arg-type]
        pct_change_range=settings["pct_change_range"],  # type: ignore[arg-type]
        amplitude_threshold=float(settings["amplitude_threshold"]),
        rsi_range=tuple(float(value) for value in settings["rsi_range"]),
        macd_golden_cross_lookback_days=int(
            settings["macd_golden_cross_lookback_days"]
        ),
        require_all=bool(settings["require_all"]),
        kdj_healthy_golden_cross_age_range=tuple(
            int(value) for value in settings["kdj_healthy_golden_cross_age_range"]
        ),
        macd_dea_minus_dif_range=tuple(
            float(value) for value in settings["macd_dea_minus_dif_range"]
        ),
        volume_ratio_range=tuple(
            float(value) for value in settings["volume_ratio_range"]
        ),
        top_n=1,
    )
    return {
        "daily_results": daily_results,
        "summary": summary,
        "data_problems": _build_data_problems(
            history_errors, factor_errors, daily_results
        ),
        "history_summary": dict(history_summary),
        "start_date": settings["start_date"],
        "end_date": settings["end_date"],
    }


def _actual_prediction_results(daily_results: pd.DataFrame) -> pd.DataFrame:
    """生成页面和下载使用的实际预测结果，不修改完整内部日表。"""

    required_columns = {
        "下一市场交易日",
        "是否预测正确",
        "次日真实涨跌幅（%）",
        "累计收益率（%）",
        "选中股票名称",
        "选中股票代码",
    }
    if not required_columns.issubset(daily_results.columns):
        return pd.DataFrame(columns=DISPLAY_RESULT_COLUMNS)

    actual_predictions = daily_results.loc[
        daily_results["是否预测正确"].isin(("正确", "错误", "失败"))
        & daily_results["次日真实涨跌幅（%）"].notna()
        & daily_results["选中股票代码"].notna()
    ].copy()
    if actual_predictions.empty:
        return pd.DataFrame(columns=DISPLAY_RESULT_COLUMNS)

    return pd.DataFrame(
        {
            "预测日期": pd.to_datetime(
                actual_predictions["下一市场交易日"], errors="coerce"
            ).dt.strftime("%Y-%m-%d"),
            "预测结果": actual_predictions["是否预测正确"].replace(
                {"错误": "失败"}
            ),
            "涨跌幅（%）": actual_predictions["次日真实涨跌幅（%）"],
            "总收益率（%）": actual_predictions["累计收益率（%）"],
            "公司名称": actual_predictions["选中股票名称"],
            "股票代码": actual_predictions["选中股票代码"],
        }
    ).reset_index(drop=True)


def _visible_data_problems(data_problems: object) -> pd.DataFrame:
    """隐藏已自动剔除的股票，仅保留需要人工关注的数据问题。"""

    if not isinstance(data_problems, pd.DataFrame) or data_problems.empty:
        return pd.DataFrame()
    if "问题类型" not in data_problems:
        return data_problems.copy()
    return data_problems.loc[
        ~data_problems["问题类型"].isin(AUTO_EXCLUDED_PROBLEM_TYPES)
    ].copy()


def _render_results(payload: Mapping[str, object]) -> None:
    """展示已提交回测的核心统计、明细和问题清单。"""

    summary = payload["summary"]
    daily_results = payload["daily_results"]
    data_problems = payload["data_problems"]
    if not isinstance(summary, Mapping) or not isinstance(daily_results, pd.DataFrame):
        return

    displayed_results = _actual_prediction_results(daily_results)
    correct_days = int(
        summary.get("预测正确天数", displayed_results["预测结果"].eq("正确").sum())
    )
    prediction_days = int(summary.get("预测天数", len(displayed_results)))
    unpredicted_days = int(
        summary.get("未预测天数", max(len(daily_results) - prediction_days, 0))
    )
    history_summary = payload.get("history_summary", {})
    history_excluded_count = (
        int(
            history_summary.get(
                "历史自动剔除股票数", history_summary.get("历史失败", 0)
            )
        )
        if isinstance(history_summary, Mapping)
        else 0
    )

    st.subheader("回测结果")
    if history_excluded_count:
        st.warning(
            f"已自动剔除 {history_excluded_count} 只历史行情不可用或日线不足的股票，"
            "以下回测统计基于其余股票。"
        )
    metric_columns = st.columns(3)
    metric_columns[0].metric("正确率", f"{correct_days} / {prediction_days}", border=True)
    metric_columns[1].metric("未预测天数", unpredicted_days, border=True)
    metric_columns[2].metric(
        "总收益率",
        f"{float(summary.get('总收益率（%）', 0.0)):.2f}%",
        border=True,
    )

    date_start = payload.get("start_date")
    date_end = payload.get("end_date")
    file_suffix = (
        f"{date_start.isoformat()}_{date_end.isoformat()}"
        if isinstance(date_start, date) and isinstance(date_end, date)
        else "结果"
    )
    st.download_button(
        "下载实际预测日 CSV",
        data=displayed_results.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"深市主板策略回测_{file_suffix}.csv",
        mime="text/csv",
        width="stretch",
    )

    st.subheader("每日回测（实际预测日）")
    if displayed_results.empty:
        st.info("所选日期范围内没有可评估的实际预测。")
    else:
        st.dataframe(
            displayed_results,
            hide_index=True,
            key="backtest_daily_results_table",
            column_config={
                "预测日期": st.column_config.TextColumn("预测日期"),
                "涨跌幅（%）": st.column_config.NumberColumn("涨跌幅（%）", format="%.2f%%"),
                "总收益率（%）": st.column_config.NumberColumn(
                    "总收益率（%）", format="%.2f%%"
                ),
            },
            width="stretch",
        )

    visible_problems = _visible_data_problems(data_problems)
    if not visible_problems.empty:
        with st.expander(f"其他数据问题（{len(visible_problems)}）", expanded=False):
            st.dataframe(visible_problems, hide_index=True, key="backtest_data_problems")


st.set_page_config(page_title="深市主板策略回测", layout="wide")
st.title("深市主板策略回测")

try:
    return_data = _load_return_data(
        str(DEFAULT_RETURNS_WORKBOOK),
        DEFAULT_RETURNS_WORKBOOK.stat().st_mtime_ns,
        RETURN_DATA_CACHE_VERSION,
    )
    companies = _load_companies(
        str(DEFAULT_STOCK_POOL), DEFAULT_STOCK_POOL.stat().st_mtime_ns
    )
    if companies.empty:
        raise core.BacktestDataError("股票池为空。")
except (OSError, ValueError, core.BacktestDataError) as exc:
    st.error(str(exc))
    st.stop()

_initialize_widget_state(return_data)
run_clicked, settings = _render_settings_form(_backtest_date_options(return_data))

if run_clicked:
    progress_slot = st.container()
    with progress_slot.status("正在运行回测", expanded=True) as status:
        progress = st.progress(0, text="准备读取长历史缓存")

        def update_history(
            completed: int,
            total: int,
            code: str,
            cache_hits: int,
            succeeded: int,
            failed: int,
        ) -> None:
            percent = int(completed / total * 100) if total else 100
            progress.progress(
                percent,
                text=(
                    f"历史行情 {completed}/{total}：{code}；缓存命中 {cache_hits}，"
                    f"成功 {succeeded}，失败 {failed}"
                ),
            )

        def update_factors(
            completed: int,
            total: int,
            code: str,
            cache_hits: int,
            succeeded: int,
            failed: int,
        ) -> None:
            percent = int(completed / total * 100) if total else 100
            progress.progress(
                percent,
                text=(
                    f"全量因子 {completed}/{total}：{code}；缓存命中 {cache_hits}，"
                    f"成功 {succeeded}，失败 {failed}"
                ),
            )

        try:
            payload = _run_backtest(
                return_data,
                companies,
                settings,
                history_progress=update_history,
                factor_progress=update_factors,
            )
        except (OSError, TypeError, ValueError, core.BacktestDataError) as exc:
            _clear_results()
            st.session_state[ERROR_STATE_KEY] = str(exc)
            status.update(label="回测未完成", state="error", expanded=True)
        else:
            st.session_state[RESULT_STATE_KEY] = payload
            st.session_state.pop(ERROR_STATE_KEY, None)
            status.update(label="回测完成", state="complete", expanded=False)

if error_message := st.session_state.get(ERROR_STATE_KEY):
    st.error(str(error_message))

result_payload = st.session_state.get(RESULT_STATE_KEY)
if isinstance(result_payload, Mapping):
    _render_results(result_payload)
