"""单股技术分析图表的 Altair 构建函数。

本模块只负责将已经计算好的日线和技术指标转换为图表，不请求网络数据，
也不依赖 Streamlit 页面状态。页面入口可直接将返回值传给
``st.altair_chart(..., width=\"stretch\")``。
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from stock_analysis import DEFAULT_KDJ_PARAMETERS, KdjParameters


# 与 stock_analysis.build_analysis_frame() 的输出字段保持一致。
REQUIRED_ANALYSIS_COLUMNS = (
    "date",
    "open",
    "close",
    "high",
    "low",
    "ma5",
    "ma20",
    "macd_dif",
    "macd_dea",
    "macd_histogram",
    "kdj_k",
    "kdj_d",
    "kdj_j",
)

_PRICE_COLUMNS = ("open", "close", "high", "low")
_INDICATOR_COLUMNS = REQUIRED_ANALYSIS_COLUMNS[5:]

# A 股通常以红色表示上涨、绿色表示下跌。其余颜色用于区分指标线。
_RISE_COLOR = "#d84a4a"
_FALL_COLOR = "#208a5b"
_MA5_COLOR = "#e08a00"
_MA20_COLOR = "#3568c0"
_DIF_COLOR = "#3568c0"
_DEA_COLOR = "#dc7f00"
_K_COLOR = "#d2592d"
_D_COLOR = "#3568c0"
_J_COLOR = "#8a5bbf"
_REFERENCE_COLOR = "#8b96a3"


def _crosses_above(leading: pd.Series, lagging: pd.Series) -> pd.Series:
    """判断一条指标线是否在当前交易日从不高于另一条线变为高于。"""

    previous_leading = leading.shift(1)
    previous_lagging = lagging.shift(1)
    return (
        leading.notna()
        & lagging.notna()
        & previous_leading.notna()
        & previous_lagging.notna()
        & leading.gt(lagging)
        & previous_leading.le(previous_lagging)
    ).fillna(False).astype(bool)


def _prepare_chart_data(frame: pd.DataFrame) -> pd.DataFrame:
    """复制并校验图表所需字段，避免修改调用方持有的 DataFrame。"""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("分析数据必须是 pandas.DataFrame。")

    missing_columns = [
        column for column in REQUIRED_ANALYSIS_COLUMNS if column not in frame.columns
    ]
    if missing_columns:
        raise ValueError(f"分析数据缺少图表字段：{', '.join(missing_columns)}。")
    if frame.empty:
        raise ValueError("没有可用于绘图的日线数据。")

    data = frame.loc[:, REQUIRED_ANALYSIS_COLUMNS].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    if data["date"].isna().any():
        raise ValueError("分析数据含有无法识别的日期。")

    for column in _PRICE_COLUMNS + _INDICATOR_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if data.loc[:, _PRICE_COLUMNS].isna().any().any():
        raise ValueError("分析数据含有无法绘制的开盘、收盘、最高或最低价。")

    # 排序与去重使时间轴稳定；指标中的早期缺失值会由 Altair 自然跳过。
    data = data.sort_values("date", kind="stable")
    data = data.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    data["candle_direction"] = data["close"].ge(data["open"]).map(
        {True: "上涨", False: "下跌"}
    )
    # 交叉事件以排序后的上一交易日为基准，首行或指标预热期不标记。
    data["ma5_golden_cross"] = _crosses_above(data["ma5"], data["ma20"])
    data["macd_golden_cross"] = _crosses_above(
        data["macd_dif"], data["macd_dea"]
    )
    data["kdj_golden_cross"] = _crosses_above(data["kdj_k"], data["kdj_d"])
    return data


def _date_encoding(*, show_axis: bool) -> alt.X:
    """创建各面板共用的日期轴编码，仅在最下方面板显示标签。"""

    axis = (
        alt.Axis(
            title="交易日期",
            format="%Y-%m-%d",
            labelAngle=-30,
            labelOverlap="greedy",
            tickCount=8,
        )
        if show_axis
        else None
    )
    return alt.X("date:T", title=None, axis=axis)


def _price_tooltips() -> list[alt.Tooltip]:
    return [
        alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
        alt.Tooltip("open:Q", title="开盘", format=".2f"),
        alt.Tooltip("close:Q", title="收盘", format=".2f"),
        alt.Tooltip("high:Q", title="最高", format=".2f"),
        alt.Tooltip("low:Q", title="最低", format=".2f"),
        alt.Tooltip("ma5:Q", title="MA5", format=".2f"),
        alt.Tooltip("ma20:Q", title="MA20", format=".2f"),
    ]


def _macd_tooltips() -> list[alt.Tooltip]:
    return [
        alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
        alt.Tooltip("macd_dif:Q", title="DIF", format=".4f"),
        alt.Tooltip("macd_dea:Q", title="DEA", format=".4f"),
        alt.Tooltip("macd_histogram:Q", title="MACD柱", format=".4f"),
    ]


def _kdj_tooltips() -> list[alt.Tooltip]:
    return [
        alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
        alt.Tooltip("kdj_k:Q", title="K", format=".2f"),
        alt.Tooltip("kdj_d:Q", title="D", format=".2f"),
        alt.Tooltip("kdj_j:Q", title="J", format=".2f"),
    ]


def _resolve_kdj_parameters(
    kdj_parameters: KdjParameters | None,
) -> KdjParameters:
    """返回图表标题使用的 KDJ 参数，并保持默认配置向后兼容。"""

    if kdj_parameters is None:
        return DEFAULT_KDJ_PARAMETERS
    if not isinstance(kdj_parameters, KdjParameters):
        raise TypeError("kdj_parameters 必须是 KdjParameters 或 None。")
    return kdj_parameters


def _format_kdj_parameters(kdj_parameters: KdjParameters) -> str:
    """生成图表与页面展示使用的 KDJ 参数文本。"""

    return (
        f"{kdj_parameters.rsv_period}, "
        f"{kdj_parameters.k_smoothing_period}, "
        f"{kdj_parameters.d_smoothing_period}"
    )


def build_stock_analysis_chart(
    frame: pd.DataFrame,
    *,
    kdj_parameters: KdjParameters | None = None,
) -> alt.VConcatChart:
    """构建 K 线、MACD 和指定参数 KDJ 的三面板图表。

    参数
    ----
    frame:
        ``stock_analysis.build_analysis_frame`` 返回的 DataFrame，需包含
        日线 OHLC、MA5、MA20、MACD 和 KDJ 字段。
    kdj_parameters:
        KDJ 的 ``N, M1, M2`` 参数；未传入时使用默认的 ``89, 3, 3``。

    返回
    ----
    altair.VConcatChart
        三个纵向拼接且共享日期轴的 Altair 图表，可直接在浏览器页面中渲染。
    """

    parameters = _resolve_kdj_parameters(kdj_parameters)
    data = _prepare_chart_data(frame)

    candle_scale = alt.Scale(
        domain=["上涨", "下跌"], range=[_RISE_COLOR, _FALL_COLOR]
    )
    price_base = alt.Chart(data)
    price_wicks = price_base.mark_rule(strokeWidth=1.1).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("low:Q", title="价格（元）", scale=alt.Scale(zero=False)),
        y2=alt.Y2("high:Q"),
        color=alt.Color("candle_direction:N", scale=candle_scale, legend=None),
        tooltip=_price_tooltips(),
    )
    price_bodies = price_base.mark_bar(size=6, opacity=0.9).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("open:Q", title="价格（元）", scale=alt.Scale(zero=False)),
        y2=alt.Y2("close:Q"),
        color=alt.Color(
            "candle_direction:N", scale=candle_scale, legend=alt.Legend(title="K线")
        ),
        tooltip=_price_tooltips(),
    )
    moving_average_lines = (
        price_base.transform_fold(["ma5", "ma20"], as_=["series", "value"])
        .transform_calculate(
            series_name="datum.series === 'ma5' ? 'MA5' : 'MA20'"
        )
        .mark_line(strokeWidth=1.8)
        .encode(
            x=_date_encoding(show_axis=False),
            y=alt.Y("value:Q", title="价格（元）", scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series_name:N",
                title="均线",
                scale=alt.Scale(domain=["MA5", "MA20"], range=[_MA5_COLOR, _MA20_COLOR]),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("series_name:N", title="均线"),
                alt.Tooltip("value:Q", title="数值", format=".2f"),
            ],
        )
    )
    ma5_cross_data = data.loc[data["ma5_golden_cross"]].copy()
    ma5_cross_data["signal_name"] = "MA5上穿MA20"
    ma5_cross_markers = alt.Chart(ma5_cross_data).mark_point(
        shape="diamond",
        filled=True,
        size=90,
        color="#7a4d00",
        stroke="white",
        strokeWidth=0.8,
    ).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("ma5:Q", title="价格（元）", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("signal_name:N", title="信号"),
            alt.Tooltip("date:T", title="发生日期", format="%Y-%m-%d"),
            alt.Tooltip("ma5:Q", title="MA5", format=".2f"),
            alt.Tooltip("ma20:Q", title="MA20", format=".2f"),
        ],
    )
    ma5_cross_labels = alt.Chart(ma5_cross_data).mark_text(
        align="left",
        baseline="bottom",
        color="#7a4d00",
        dx=5,
        dy=-5,
        fontSize=9,
    ).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("ma5:Q", title="价格（元）", scale=alt.Scale(zero=False)),
        text=alt.Text("date:T", format="%m-%d"),
    )
    price_panel = (
        alt.layer(
            price_wicks,
            price_bodies,
            moving_average_lines,
            ma5_cross_markers,
            ma5_cross_labels,
        )
        .properties(height=320, title=alt.TitleParams(text="K线与均线", anchor="start"))
    )

    macd_base = alt.Chart(data)
    macd_histogram = macd_base.mark_bar(size=6, opacity=0.82).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("macd_histogram:Q", title="MACD", axis=alt.Axis(format=".2f")),
        color=alt.condition(
            alt.datum.macd_histogram >= 0,
            alt.value(_RISE_COLOR),
            alt.value(_FALL_COLOR),
        ),
        tooltip=_macd_tooltips(),
    )
    macd_zero_line = alt.Chart(pd.DataFrame({"reference": [0.0]})).mark_rule(
        color=_REFERENCE_COLOR, strokeDash=[4, 3]
    ).encode(y=alt.Y("reference:Q"))
    macd_lines = (
        macd_base.transform_fold(
            ["macd_dif", "macd_dea"], as_=["series", "value"]
        )
        .transform_calculate(
            series_name="datum.series === 'macd_dif' ? 'DIF' : 'DEA'"
        )
        .mark_line(strokeWidth=1.8)
        .encode(
            x=_date_encoding(show_axis=False),
            y=alt.Y("value:Q", title="MACD", axis=alt.Axis(format=".2f")),
            color=alt.Color(
                "series_name:N",
                title="MACD线",
                scale=alt.Scale(domain=["DIF", "DEA"], range=[_DIF_COLOR, _DEA_COLOR]),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("series_name:N", title="指标"),
                alt.Tooltip("value:Q", title="数值", format=".4f"),
            ],
        )
    )
    macd_cross_data = data.loc[data["macd_golden_cross"]].copy()
    macd_cross_data["signal_name"] = "MACD金叉"
    macd_cross_markers = alt.Chart(macd_cross_data).mark_point(
        shape="triangle-up",
        filled=True,
        size=90,
        color="#7f3c8d",
        stroke="white",
        strokeWidth=0.8,
    ).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("macd_dif:Q", title="MACD", axis=alt.Axis(format=".2f")),
        tooltip=[
            alt.Tooltip("signal_name:N", title="信号"),
            alt.Tooltip("date:T", title="发生日期", format="%Y-%m-%d"),
            alt.Tooltip("macd_dif:Q", title="DIF", format=".4f"),
            alt.Tooltip("macd_dea:Q", title="DEA", format=".4f"),
        ],
    )
    macd_cross_labels = alt.Chart(macd_cross_data).mark_text(
        align="left",
        baseline="bottom",
        color="#7f3c8d",
        dx=5,
        dy=-5,
        fontSize=9,
    ).encode(
        x=_date_encoding(show_axis=False),
        y=alt.Y("macd_dif:Q", title="MACD", axis=alt.Axis(format=".2f")),
        text=alt.Text("date:T", format="%m-%d"),
    )
    macd_panel = (
        alt.layer(
            macd_histogram,
            macd_zero_line,
            macd_lines,
            macd_cross_markers,
            macd_cross_labels,
        )
        .properties(
            height=170,
            title=alt.TitleParams(text="MACD（12, 26, 9）", anchor="start"),
        )
    )

    kdj_base = alt.Chart(data)
    kdj_reference_lines = alt.Chart(
        pd.DataFrame(
            {
                "level": [20.0, 80.0],
                "label": ["超卖参考线（20）", "超买参考线（80）"],
            }
        )
    ).mark_rule(color=_REFERENCE_COLOR, strokeDash=[4, 3]).encode(
        y=alt.Y("level:Q"),
        tooltip=[alt.Tooltip("label:N", title="KDJ参考线")],
    )
    kdj_lines = (
        kdj_base.transform_fold(
            ["kdj_k", "kdj_d", "kdj_j"], as_=["series", "value"]
        )
        .transform_calculate(
            series_name=(
                "datum.series === 'kdj_k' ? 'K' : "
                "datum.series === 'kdj_d' ? 'D' : 'J'"
            )
        )
        .mark_line(strokeWidth=1.8)
        .encode(
            x=_date_encoding(show_axis=True),
            y=alt.Y("value:Q", title="KDJ", scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series_name:N",
                title="KDJ线",
                scale=alt.Scale(
                    domain=["K", "D", "J"], range=[_K_COLOR, _D_COLOR, _J_COLOR]
                ),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("series_name:N", title="指标"),
                alt.Tooltip("value:Q", title="数值", format=".2f"),
            ],
        )
    )
    kdj_cross_data = data.loc[data["kdj_golden_cross"]].copy()
    kdj_cross_data["signal_name"] = "KDJ金叉"
    kdj_cross_markers = alt.Chart(kdj_cross_data).mark_point(
        shape="circle",
        filled=True,
        size=75,
        color="#007a70",
        stroke="white",
        strokeWidth=0.8,
    ).encode(
        x=_date_encoding(show_axis=True),
        y=alt.Y("kdj_k:Q", title="KDJ", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("signal_name:N", title="信号"),
            alt.Tooltip("date:T", title="发生日期", format="%Y-%m-%d"),
            alt.Tooltip("kdj_k:Q", title="K", format=".2f"),
            alt.Tooltip("kdj_d:Q", title="D", format=".2f"),
        ],
    )
    kdj_cross_labels = alt.Chart(kdj_cross_data).mark_text(
        align="left",
        baseline="bottom",
        color="#007a70",
        dx=5,
        dy=-5,
        fontSize=9,
    ).encode(
        x=_date_encoding(show_axis=True),
        y=alt.Y("kdj_k:Q", title="KDJ", scale=alt.Scale(zero=False)),
        text=alt.Text("date:T", format="%m-%d"),
    )
    kdj_panel = (
        alt.layer(kdj_reference_lines, kdj_lines, kdj_cross_markers, kdj_cross_labels)
        .properties(
            height=190,
            title=alt.TitleParams(
                text=f"KDJ（{_format_kdj_parameters(parameters)}）",
                anchor="start",
            ),
        )
    )

    return (
        alt.vconcat(price_panel, macd_panel, kdj_panel, spacing=12)
        .resolve_scale(x="shared", y="independent")
        .properties(title=alt.TitleParams(text="股票技术分析", anchor="start", fontSize=18))
        .configure_axis(
            gridColor="#e2e8f0",
            labelColor="#334155",
            titleColor="#1e293b",
            labelFontSize=11,
            titleFontSize=12,
        )
        .configure_legend(
            orient="top",
            labelColor="#334155",
            titleColor="#1e293b",
            labelFontSize=11,
            titleFontSize=11,
        )
        .configure_view(stroke=None)
    )


__all__ = ["build_stock_analysis_chart"]
