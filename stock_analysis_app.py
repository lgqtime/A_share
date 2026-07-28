"""独立的 A 股个股技术分析 Streamlit 页面。"""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from stock_analysis import (
    DEFAULT_DISPLAY_BARS,
    DEFAULT_KDJ_PARAMETERS,
    KdjParameters,
    StockHistoryError,
    build_analysis_frame,
    fetch_adjusted_daily_history,
    normalize_stock_code,
)
from stock_analysis_charts import build_stock_analysis_chart


MIN_DISPLAY_BARS = 20
MAX_DISPLAY_BARS = 500
MIN_KDJ_RSV_PERIOD = 2
MAX_KDJ_RSV_PERIOD = 250
MIN_KDJ_SMOOTHING_PERIOD = 1
MAX_KDJ_SMOOTHING_PERIOD = 20


@st.cache_data(ttl="15m", max_entries=32, show_spinner=False)
def load_adjusted_history_cached(
    stock_code: str,
    display_bars: int,
    kdj_parameters: KdjParameters = DEFAULT_KDJ_PARAMETERS,
) -> pd.DataFrame:
    """缓存行情接口返回的原始日线，避免相同查询重复请求网络。"""

    return fetch_adjusted_daily_history(
        stock_code,
        display_bars=display_bars,
        kdj_parameters=kdj_parameters,
    )


def _query_int_value(
    query: object,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """从旧会话查询中安全读取整数，缺失或无效值回退为默认值。"""

    if not isinstance(query, dict):
        return default
    value = query.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if minimum <= number <= maximum else default


def _kdj_parameters_from_query(query: object) -> KdjParameters:
    """兼容热重载前保存的查询；旧查询缺少 KDJ 参数时沿用默认值。"""

    return KdjParameters(
        rsv_period=_query_int_value(
            query,
            "kdj_rsv_period",
            DEFAULT_KDJ_PARAMETERS.rsv_period,
            minimum=MIN_KDJ_RSV_PERIOD,
            maximum=MAX_KDJ_RSV_PERIOD,
        ),
        k_smoothing_period=_query_int_value(
            query,
            "kdj_k_smoothing_period",
            DEFAULT_KDJ_PARAMETERS.k_smoothing_period,
            minimum=MIN_KDJ_SMOOTHING_PERIOD,
            maximum=MAX_KDJ_SMOOTHING_PERIOD,
        ),
        d_smoothing_period=_query_int_value(
            query,
            "kdj_d_smoothing_period",
            DEFAULT_KDJ_PARAMETERS.d_smoothing_period,
            minimum=MIN_KDJ_SMOOTHING_PERIOD,
            maximum=MAX_KDJ_SMOOTHING_PERIOD,
        ),
    )


def _format_kdj_parameters(kdj_parameters: KdjParameters) -> str:
    """生成页面展示的 KDJ 参数文本。"""

    return (
        f"{kdj_parameters.rsv_period}, "
        f"{kdj_parameters.k_smoothing_period}, "
        f"{kdj_parameters.d_smoothing_period}"
    )


def _finite_number(value: object) -> float | None:
    """将单个指标值转为可展示的有限浮点数。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_price(value: object) -> str:
    number = _finite_number(value)
    return "--" if number is None else f"{number:.2f}"


def _format_percent(value: object, *, signed: bool = False) -> str:
    number = _finite_number(value)
    if number is None:
        return "--"
    sign = "+" if signed else ""
    return f"{number:{sign}.2f}%"


def _format_amount(value: object) -> str:
    number = _finite_number(value)
    if number is None:
        return "--"
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f} 亿"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.2f} 万"
    return f"{number:,.0f}"


def _render_summary(
    analysis_frame: pd.DataFrame,
    *,
    kdj_parameters: KdjParameters,
) -> None:
    """渲染最近一个交易日的简要行情与技术指标。"""

    latest = analysis_frame.iloc[-1]
    with st.container(horizontal=True):
        st.metric(
            "最新收盘",
            _format_price(latest["close"]),
            delta=_format_percent(latest["pct_change"], signed=True),
            border=True,
        )
        st.metric("MA5", _format_price(latest["ma5"]), border=True)
        st.metric("MA20", _format_price(latest["ma20"]), border=True)
        st.metric(
            f"KDJ（{_format_kdj_parameters(kdj_parameters)}）",
            f"K {_format_price(latest['kdj_k'])} / D {_format_price(latest['kdj_d'])}",
            delta=f"J {_format_price(latest['kdj_j'])}",
            border=True,
        )

    latest_date = pd.Timestamp(latest["date"]).strftime("%Y-%m-%d")
    st.caption(
        f"最新交易日：{latest_date} | 日成交额：{_format_amount(latest['amount'])}"
        f" | 换手率：{_format_percent(latest['turnover'])}"
        f" | DIF：{_format_price(latest['macd_dif'])}"
        f" | DEA：{_format_price(latest['macd_dea'])}"
    )


st.set_page_config(
    page_title="个股技术分析",
    page_icon=":material/candlestick_chart:",
    layout="wide",
)

st.title("个股技术分析")

st.session_state.setdefault("stock_analysis_query", None)
last_query = st.session_state["stock_analysis_query"]
initial_code = (
    "000001"
    if not isinstance(last_query, dict)
    else str(last_query.get("stock_code", "000001"))
)
initial_display_bars = _query_int_value(
    last_query,
    "display_bars",
    DEFAULT_DISPLAY_BARS,
    minimum=MIN_DISPLAY_BARS,
    maximum=MAX_DISPLAY_BARS,
)
initial_kdj_parameters = _kdj_parameters_from_query(last_query)

with st.form("stock_analysis_query_form", border=True):
    stock_code_input = st.text_input(
        "股票代码",
        value=initial_code,
        placeholder="例如 000001、600000 或 000001.SZ",
        help="支持沪深 A 股的 6 位代码，可带 .SZ、.SH、SZ 或 SH 市场标识。",
    )
    display_bars_input = st.number_input(
        "显示交易日数",
        min_value=MIN_DISPLAY_BARS,
        max_value=MAX_DISPLAY_BARS,
        value=int(initial_display_bars),
        step=20,
        help="系统会按所选 KDJ 参数额外获取预热数据，确保指标计算完整。",
    )
    with st.container(horizontal=True):
        kdj_rsv_period_input = st.number_input(
            "KDJ N（RSV 周期）",
            min_value=MIN_KDJ_RSV_PERIOD,
            max_value=MAX_KDJ_RSV_PERIOD,
            value=initial_kdj_parameters.rsv_period,
            step=1,
            key="stock_analysis_kdj_n",
        )
        kdj_k_smoothing_period_input = st.number_input(
            "KDJ M1（K 平滑周期）",
            min_value=MIN_KDJ_SMOOTHING_PERIOD,
            max_value=MAX_KDJ_SMOOTHING_PERIOD,
            value=initial_kdj_parameters.k_smoothing_period,
            step=1,
            key="stock_analysis_kdj_m1",
        )
        kdj_d_smoothing_period_input = st.number_input(
            "KDJ M2（D 平滑周期）",
            min_value=MIN_KDJ_SMOOTHING_PERIOD,
            max_value=MAX_KDJ_SMOOTHING_PERIOD,
            value=initial_kdj_parameters.d_smoothing_period,
            step=1,
            key="stock_analysis_kdj_m2",
        )
    submitted = st.form_submit_button(
        "获取行情并绘制图表",
        icon=":material/search:",
        type="primary",
    )

if submitted:
    kdj_parameters = KdjParameters(
        rsv_period=int(kdj_rsv_period_input),
        k_smoothing_period=int(kdj_k_smoothing_period_input),
        d_smoothing_period=int(kdj_d_smoothing_period_input),
    )
    st.session_state["stock_analysis_query"] = {
        "stock_code": stock_code_input.strip(),
        "display_bars": int(display_bars_input),
        "kdj_rsv_period": kdj_parameters.rsv_period,
        "kdj_k_smoothing_period": kdj_parameters.k_smoothing_period,
        "kdj_d_smoothing_period": kdj_parameters.d_smoothing_period,
    }

query = st.session_state["stock_analysis_query"]
result_slot = st.container()

if query is not None:
    try:
        security = normalize_stock_code(query["stock_code"])
        kdj_parameters = _kdj_parameters_from_query(query)
        with result_slot:
            with st.spinner(f"正在获取 {security.code} 的历史日线..."):
                history = load_adjusted_history_cached(
                    security.code,
                    int(query["display_bars"]),
                    kdj_parameters,
                )
                analysis_frame = build_analysis_frame(
                    history,
                    display_bars=int(query["display_bars"]),
                    kdj_parameters=kdj_parameters,
                )
                chart = build_stock_analysis_chart(
                    analysis_frame,
                    kdj_parameters=kdj_parameters,
                )
    except ValueError as exc:
        result_slot.error(f"查询条件无效：{exc}")
    except StockHistoryError as exc:
        result_slot.error(f"暂时无法获取历史行情：{exc}")
    except Exception:
        result_slot.error("分析图表生成失败，请稍后重试或检查股票代码。")
    else:
        with result_slot:
            st.subheader(f"{security.code}（{security.exchange}）技术图表")
            _render_summary(analysis_frame, kdj_parameters=kdj_parameters)
            st.altair_chart(chart, width="stretch")
