"""深市主板量化选股 Streamlit 应用。

在本目录运行：

    uv run --locked streamlit run szse_quant_app.py --server.port 8504

本应用读取本目录的 ``深交所数据.xlsx`` 中“主板公司”工作表。行情主源采用
东方财富公开日 K JSON 接口；接口不可用时回退腾讯公开日 K 接口。

东方财富返回完整的前复权 OHLC、成交量、成交额和换手率。腾讯增强日 K 回退
同样提供历史成交额和换手率；仅当增强接口不可用时才降级到旧版 OHLCV，并在
当日行情快照可对齐时补齐最新交易日数据。任何无法获得的字段均明确标记缺失，
绝不以估算值冒充真实数据。流通市值按同一交易日的成交额与换手率换算为
成交均价口径，明确显示为派生因子。
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

import pandas as pd
import requests
import streamlit as st

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKBOOK_PATH = MODULE_DIR / "深交所数据.xlsx"
CACHE_DIR = MODULE_DIR / "data_cache" / "szse_quant"
OPTIMIZED_PARAMETER_FILE = (
    MODULE_DIR
    / "outputs"
    / "rolling_parameter_updates"
    / "rolling_parameter_optimization_current.json"
)

EASTMONEY_DAILY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_RICH_DAILY_KLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
)
TENCENT_LEGACY_DAILY_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
EASTMONEY_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# KDJ(89,3,3) 的顶背离需要比较最近 60 个有效 J 值。保留 150 根日线后，
# 截至昨日仍有至少 60 个有效 KDJ 值可用于判断。
INDICATOR_WARMUP_BARS = 150
MIN_REQUIRED_BARS = INDICATOR_WARMUP_BARS
FETCH_CALENDAR_DAYS = 420
DEFAULT_CACHE_HOURS = 12.0
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.25
DEFAULT_WORKERS = 4
MAX_WORKERS = 6
MAX_RETRIES = 3
CACHE_SCHEMA_VERSION = 5
FACTOR_CACHE_VERSION = 12
SOURCE_FAILURE_THRESHOLD = 3
SOURCE_COOLDOWN_SECONDS = 30.0
DATA_PIPELINE_VERSION = "qfq-platform-v18"
UNKNOWN_INDUSTRY = "未分类"
PREDICTION_REVIEW_TOP_N = 50
PREDICTION_REVIEW_RANK_COLUMN = "排名（风险过滤后）"
PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN = "入选数（前50）"

KDJ_RSV_PERIOD = 89
KDJ_K_SMOOTHING_PERIOD = 3
KDJ_D_SMOOTHING_PERIOD = 3
# MACD 金叉只读取筛选日前已完整收盘的交易日；N=1 即昨天。
MACD_GOLDEN_CROSS_OFFSET_BARS = 1
DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS = 3
MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS = 20
# KDJ 健康金叉年龄以当前选股日为零点，0 表示当日。
KDJ_HEALTHY_GOLDEN_CROSS_OFFSET_BARS = 0
MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE = 0
MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE = 10
DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE = (1, 3)
KDJ_DIVERGENCE_LOOKBACK_BARS = 60
KDJ_OVERSOLD_THRESHOLD = 20.0
PLATFORM_BREAKOUT_LOOKBACK_BARS = 20
CLOSE_NEAR_DAILY_HIGH_THRESHOLD = 70.0
CANDLESTICK_RISK_LOOKBACK_BARS = 3
CANDLESTICK_DOJI_MAX_BODY_RATIO = 0.10
CANDLESTICK_INVERTED_T_MAX_LOWER_SHADOW_RATIO = 0.10
CANDLESTICK_INVERTED_T_MIN_UPPER_SHADOW_RATIO = 0.60
CANDLESTICK_HANGING_MAN_LOOKBACK_BARS = 20
CANDLESTICK_HANGING_MAN_HIGH_ZONE_RATIO = 0.95
CANDLESTICK_HANGING_MAN_MAX_BODY_RATIO = 0.30
CANDLESTICK_HANGING_MAN_MIN_LOWER_SHADOW_RATIO = 0.50
CANDLESTICK_HANGING_MAN_MAX_UPPER_SHADOW_RATIO = 0.20
CANDLESTICK_EXTREME_BULLISH_MIN_PCT_CHANGE = 8.0
CANDLESTICK_COMPARISON_TOLERANCE = 1e-9

DEFAULT_TURNOVER_RANGE = (5.4, 10.7)
FLOAT_MARKET_CAP_MIN_YI = 1.0
FLOAT_MARKET_CAP_MAX_YI = 1_000.0
DEFAULT_FLOAT_MARKET_CAP_RANGE_YI = (50.0, 200.0)
DEFAULT_PCT_CHANGE_RANGE = (-2.7, 10.1)
RSI_MIN_VALUE = 0.0
RSI_MAX_VALUE = 100.0
DEFAULT_RSI_RANGE = (49.1, 62.6)
MACD_DEA_MINUS_DIF_MIN_THRESHOLD = -1.0
MACD_DEA_MINUS_DIF_MAX_THRESHOLD = 1.0
DEFAULT_MACD_DEA_MINUS_DIF_RANGE = (0.1, 0.2)
MACD_DEA_MINUS_DIF_COMPARISON_TOLERANCE = 1e-9
VOLUME_RATIO_MIN_THRESHOLD = 0.0
# 回测筛选使用闭区间；保留单阈值常量供旧调用方使用。
VOLUME_RATIO_MAX_THRESHOLD = 15.0
DEFAULT_VOLUME_RATIO_THRESHOLD = 1.5
DEFAULT_VOLUME_RATIO_RANGE = (1.8, 3.8)

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

# 风险规则只负责剔除候选股；大多数得分条件为 1 分，KDJ 健康金叉为 1.5 分。
SCORING_INDICATOR_WEIGHTS = {
    "above_ma5": 1.0,
    "above_ma20": 1.0,
    "ma5_above_ma20": 1.0,
    "rsi_in_range": 1.0,
    "macd_bullish": 1.0,
    "macd_golden_cross": 1.0,
    "macd_bearish": 1.0,
    "macd_dead_cross": 1.0,
    "macd_dea_minus_dif_high": 1.0,
    "kdj_healthy_golden_cross_3d": 1.5,
    "platform_breakout_20d": 1.0,
    "ma5_rising": 1.0,
    "close_near_daily_high": 1.0,
    "volume_breakout": 1.0,
    "amount_at_least_100m": 1.0,
    "turnover_in_range": 1.0,
    "float_market_cap_in_range": 1.0,
    "positive_change": 1.0,
    "pct_change_in_range": 1.0,
    "amplitude_high": 1.0,
}
SCORING_INDICATOR_KEYS = tuple(SCORING_INDICATOR_WEIGHTS)
MACD_GOLDEN_CROSS_DATE_COLUMN = "最近MACD金叉日期"
MACD_GOLDEN_CROSS_AGE_COLUMN = "最近MACD金叉距今交易日数"
MACD_GOLDEN_CROSS_LABEL = (
    f"近{DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS}个已完成交易日MACD金叉"
)
KDJ_HEALTHY_GOLDEN_CROSS_NAME = "KDJ金叉且状态良好"
KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN = "最近KDJ健康金叉日期"
KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN = "最近KDJ健康金叉距今交易日数（0=当日）"
KDJ_HEALTHY_GOLDEN_CROSS_LABEL = (
    f"KDJ金叉距今{DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE[0]}-"
    f"{DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE[1]}个交易日且状态良好"
)
CANDLESTICK_RISK_PATTERN_LABELS = {
    "doji_3d": "十字星（多空分歧）",
    "inverted_t_doji_3d": "倒T字星（冲高回落）",
    "hanging_man_3d": "吊颈线（高位震荡诱多）",
    "long_upper_shadow_bullish_3d": "长上影线阳线（抛压沉重）",
    "extreme_bullish_3d": "极端大阳线（涨幅超过8%）",
}
CANDLESTICK_RISK_PATTERN_KEYS = tuple(CANDLESTICK_RISK_PATTERN_LABELS)
CANDLESTICK_RISK_FACTOR_COLUMNS = {
    "doji_3d": "近3日十字星",
    "inverted_t_doji_3d": "近3日倒T字星",
    "hanging_man_3d": "近3日吊颈线",
    "long_upper_shadow_bullish_3d": "近3日长上影阳线",
    "extreme_bullish_3d": "近3日极端大阳线",
}
CANDLESTICK_RISK_EXCLUSION_LABELS = {
    key: f"近3日出现{label}" for key, label in CANDLESTICK_RISK_PATTERN_LABELS.items()
}

# 所有可由策略预设控制的筛选控件都使用显式 key，默认配置保持原有界面行为。
SCREENING_WIDGET_DEFAULTS: dict[str, object] = {
    "szse_quant_filter_above_ma5": True,
    "szse_quant_filter_above_ma20": True,
    "szse_quant_filter_ma5_above_ma20": False,
    "szse_quant_filter_platform_breakout_20d": False,
    "szse_quant_filter_ma5_rising": False,
    "szse_quant_filter_close_near_daily_high": False,
    "szse_quant_filter_rsi_in_range": True,
    "szse_quant_filter_rsi_range": DEFAULT_RSI_RANGE,
    "szse_quant_filter_macd_bullish": True,
    "szse_quant_filter_macd_golden_cross": False,
    "szse_quant_filter_macd_golden_cross_lookback_days": (
        DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS
    ),
    "szse_quant_filter_macd_bearish": False,
    "szse_quant_filter_macd_dead_cross": False,
    "szse_quant_filter_macd_dea_minus_dif_high": False,
    "szse_quant_filter_macd_dea_minus_dif_range": DEFAULT_MACD_DEA_MINUS_DIF_RANGE,
    "szse_quant_filter_kdj_healthy_golden_cross_3d": True,
    "szse_quant_filter_kdj_healthy_golden_cross_age_range": (
        DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE
    ),
    "szse_quant_filter_volume_breakout": True,
    "szse_quant_filter_volume_ratio_range": DEFAULT_VOLUME_RATIO_RANGE,
    # 兼容旧的固定策略、优化脚本和原始因子展示；回测页面不再读取此键。
    "szse_quant_filter_volume_ratio_threshold": DEFAULT_VOLUME_RATIO_THRESHOLD,
    "szse_quant_filter_amount_at_least_100m": True,
    "szse_quant_filter_turnover_in_range": True,
    "szse_quant_filter_turnover_range": DEFAULT_TURNOVER_RANGE,
    "szse_quant_filter_float_market_cap_in_range": False,
    "szse_quant_filter_float_market_cap_range_yi": DEFAULT_FLOAT_MARKET_CAP_RANGE_YI,
    "szse_quant_filter_positive_change": False,
    "szse_quant_filter_pct_change_in_range": True,
    "szse_quant_filter_pct_change_range": DEFAULT_PCT_CHANGE_RANGE,
    "szse_quant_filter_amplitude_high": False,
    "szse_quant_filter_amplitude_threshold": 3.0,
    "szse_quant_filter_require_all": True,
    "szse_quant_risk_bias_high": True,
    "szse_quant_risk_upper_shadow": False,
    "szse_quant_risk_resistance_60_day": True,
    "szse_quant_risk_candlestick_patterns": [
        "doji_3d",
        "inverted_t_doji_3d",
        "hanging_man_3d",
    ],
}

# 策略预设只覆盖筛选设置，不改变用户选择的日期、股票数量、缓存或并发配置。
PLATFORM_BREAKOUT_PRESET_OVERRIDES: dict[str, object] = {
    "szse_quant_filter_above_ma5": True,
    "szse_quant_filter_above_ma20": True,
    "szse_quant_filter_ma5_above_ma20": False,
    "szse_quant_filter_platform_breakout_20d": True,
    "szse_quant_filter_ma5_rising": True,
    "szse_quant_filter_close_near_daily_high": True,
    "szse_quant_filter_rsi_in_range": False,
    "szse_quant_filter_macd_bullish": False,
    "szse_quant_filter_macd_golden_cross": False,
    "szse_quant_filter_macd_bearish": False,
    "szse_quant_filter_macd_dead_cross": False,
    "szse_quant_filter_macd_dea_minus_dif_high": False,
    "szse_quant_filter_kdj_healthy_golden_cross_3d": False,
    "szse_quant_filter_volume_breakout": True,
    "szse_quant_filter_amount_at_least_100m": True,
    "szse_quant_filter_turnover_in_range": True,
    "szse_quant_filter_float_market_cap_in_range": False,
    "szse_quant_filter_positive_change": False,
    "szse_quant_filter_pct_change_in_range": False,
    "szse_quant_filter_amplitude_high": False,
    "szse_quant_filter_require_all": True,
    "szse_quant_risk_bias_high": True,
    "szse_quant_risk_upper_shadow": False,
    "szse_quant_risk_resistance_60_day": False,
    "szse_quant_risk_candlestick_patterns": list(CANDLESTICK_RISK_PATTERN_KEYS),
}

COLLECTION_SESSION_STATE_KEYS = (
    "szse_quant_factors",
    "szse_quant_errors",
    "szse_quant_summary",
    "szse_quant_results",
    "szse_quant_eligible_count",
    "szse_quant_risk_excluded_count",
    "szse_quant_as_of_date",
    "szse_quant_results_as_of_date",
    "szse_quant_results_max_score",
    "szse_quant_ranked_top_50",
    # 清理上一版预测页留下的自动行业共识结果。
    "szse_quant_industry_consensus",
    "szse_quant_industry_consensus_message",
)

SCREENING_RESULT_SESSION_STATE_KEYS = (
    "szse_quant_results",
    "szse_quant_eligible_count",
    "szse_quant_risk_excluded_count",
    "szse_quant_results_as_of_date",
    "szse_quant_results_max_score",
    "szse_quant_ranked_top_50",
    # 清理上一版预测页留下的自动行业共识结果。
    "szse_quant_industry_consensus",
    "szse_quant_industry_consensus_message",
)

# The rolling optimizer only searches these interval controls.  Loading its
# result must not replace each app's independently configured conditions.
OPTIMIZED_PARAMETER_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "szse_quant_filter_rsi_range": (RSI_MIN_VALUE, RSI_MAX_VALUE, False),
    "szse_quant_filter_turnover_range": (0.0, 100.0, False),
    "szse_quant_filter_volume_ratio_range": (
        VOLUME_RATIO_MIN_THRESHOLD,
        VOLUME_RATIO_MAX_THRESHOLD,
        False,
    ),
    "szse_quant_filter_pct_change_range": (-20.0, 20.0, False),
    "szse_quant_filter_kdj_healthy_golden_cross_age_range": (
        float(MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE),
        float(MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE),
        True,
    ),
    "szse_quant_filter_macd_dea_minus_dif_range": (
        MACD_DEA_MINUS_DIF_MIN_THRESHOLD,
        MACD_DEA_MINUS_DIF_MAX_THRESHOLD,
        False,
    ),
}


@dataclass(frozen=True)
class FetchOutcome:
    """单只股票行情请求的统一结果，失败不会中断其他股票。"""

    code: str
    history: pd.DataFrame | None
    source: str | None
    from_cache: bool
    factors: dict[str, object] | None = None
    error: str | None = None


class RequestRateLimiter:
    """跨线程限制公开接口请求频率，避免批量扫描触发临时风控。"""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            scheduled_at = max(now, self._next_allowed_at)
            self._next_allowed_at = scheduled_at + self.minimum_interval_seconds
        delay = scheduled_at - now
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """在服务端限流后延后尚未开始的请求，避免继续施压同一接口。"""

        with self._lock:
            self._next_allowed_at = max(
                self._next_allowed_at,
                time.monotonic() + max(0.0, float(seconds)),
            )


class FetchFailure(RuntimeError):
    """保留请求失败是否属于服务级故障，供批次熔断器判断。"""

    def __init__(self, message: str, *, service_unavailable: bool) -> None:
        super().__init__(message)
        self.service_unavailable = service_unavailable


class SourceCircuitBreaker:
    """连续服务级失败时临时绕过数据源，避免整批任务反复等待长超时。"""

    def __init__(
        self,
        source_name: str,
        *,
        failure_threshold: int = SOURCE_FAILURE_THRESHOLD,
        cooldown_seconds: float = SOURCE_COOLDOWN_SECONDS,
    ) -> None:
        self.source_name = source_name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow_request(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def record_service_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = max(
                    self._open_until,
                    time.monotonic() + self.cooldown_seconds,
                )

    def unavailable_error(self) -> FetchFailure:
        return FetchFailure(
            f"{self.source_name}服务暂时不可用，已跳过本次请求。",
            service_unavailable=True,
        )


_THREAD_LOCAL = threading.local()


def _request_session() -> requests.Session:
    """为每个工作线程复用独立 Session，避免跨线程共享连接状态。"""

    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://quote.eastmoney.com/",
            }
        )
        _THREAD_LOCAL.session = session
    return session


def _coerce_number(value: object) -> float | None:
    """将公开接口中的数字、空值和占位符统一转换为有限浮点数。"""

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "None", "null"}:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def estimated_float_market_cap_yi(amount: object, turnover: object) -> float | None:
    """Derive circulating market value in 100 million yuan from one trading day.

    Daily turnover rate is the traded-share ratio of the circulating shares.  Combining
    it with the same day's transaction value produces the circulating market value at
    the volume-weighted transaction price, without mixing in a current quote.
    """

    amount_value = _coerce_number(amount)
    turnover_value = _coerce_number(turnover)
    if amount_value is None or turnover_value is None or amount_value < 0 or turnover_value <= 0:
        return None
    return amount_value * 100.0 / turnover_value / 100_000_000.0


def _as_six_digit_code(value: object) -> str | None:
    """返回严格的六位股票代码，拒绝无效或被 Excel 转成科学计数法的值。"""

    text = str(value).strip()
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return None


def _industry_or_unknown(value: object) -> str:
    """规范化行业文本；旧版股票池缺列时明确标记为未分类。"""

    if value is None:
        return UNKNOWN_INDUSTRY
    try:
        if bool(pd.isna(value)):
            return UNKNOWN_INDUSTRY
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.casefold() in {"", "nan", "none", "nat", "<na>"}:
        return UNKNOWN_INDUSTRY
    return text


def _industry_is_blank(value: object) -> bool:
    """识别 Excel 空单元格与常见缺失占位，供正式股票池完整性校验使用。"""

    if value is None:
        return True
    try:
        if bool(pd.isna(value)):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in {"", "nan", "none", "nat", "<na>"}


def load_mainboard_companies(workbook_path: Path) -> pd.DataFrame:
    """读取经官方字段校验的“主板公司”表，返回代码、名称和行业。"""

    if not workbook_path.is_file():
        raise FileNotFoundError(
            f"未找到股票池文件：{workbook_path}。请先运行 fetch_szse_data.py 生成它。"
        )

    try:
        frame = pd.read_excel(workbook_path, sheet_name="主板公司", dtype=str)
    except ValueError as exc:
        raise ValueError("Excel 中没有“主板公司”工作表。") from exc

    required_columns = {"公司代码", "公司简称", "所属行业"}
    missing_columns = required_columns.difference(frame.columns)
    if missing_columns:
        missing_text = "、".join(sorted(missing_columns))
        raise ValueError(f"“主板公司”工作表缺少必要列：{missing_text}。")

    companies = frame.loc[:, ["公司代码", "公司简称", "所属行业"]].copy()
    companies = companies.rename(
        columns={"公司代码": "股票代码", "公司简称": "股票名称"}
    )
    companies["股票代码"] = companies["股票代码"].map(_as_six_digit_code)
    companies["股票名称"] = companies["股票名称"].fillna("").astype(str).str.strip()
    invalid_code_count = int(companies["股票代码"].isna().sum())
    blank_name_count = int(companies["股票名称"].eq("").sum())
    blank_industry_count = int(companies["所属行业"].map(_industry_is_blank).sum())
    duplicate_code_count = int(companies["股票代码"].duplicated(keep=False).sum())
    if invalid_code_count or blank_name_count or blank_industry_count or duplicate_code_count:
        details = []
        if invalid_code_count:
            details.append(f"无效股票代码 {invalid_code_count} 条")
        if blank_name_count:
            details.append(f"空公司简称 {blank_name_count} 条")
        if blank_industry_count:
            details.append(f"空所属行业 {blank_industry_count} 条")
        if duplicate_code_count:
            details.append(f"重复股票代码 {duplicate_code_count} 条")
        raise ValueError("“主板公司”工作表数据不完整：" + "；".join(details) + "。")
    companies["所属行业"] = companies["所属行业"].map(_industry_or_unknown)
    if companies.empty:
        raise ValueError("“主板公司”工作表中没有可用的股票代码。")
    # 序号来自原始主板公司清单。得分相同的股票按这个序号排序。
    companies.insert(0, "序号", range(1, len(companies) + 1))
    return companies


@st.cache_data(show_spinner=False)
def load_mainboard_companies_cached(workbook_path_text: str, modified_ns: int) -> pd.DataFrame:
    """按 Excel 修改时间缓存股票池；修改文件后会自动重新读取。"""

    del modified_ns
    return load_mainboard_companies(Path(workbook_path_text))


def _normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """规范化不同来源的日线字段，计算缺失的涨跌幅和振幅。"""

    normalized = frame.copy()
    for column in HISTORY_COLUMNS:
        if column not in normalized:
            normalized[column] = pd.NA

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    numeric_columns = [column for column in HISTORY_COLUMNS if column != "date"]
    for column in numeric_columns:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized = normalized.dropna(subset=["date", "open", "close", "high", "low", "volume"])
    normalized = normalized.loc[
        (normalized["open"] > 0)
        & (normalized["close"] > 0)
        & (normalized["high"] > 0)
        & (normalized["low"] > 0)
        & (normalized["volume"] >= 0)
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


def _limit_history_to_as_of_date(
    history: pd.DataFrame,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """只保留不晚于目标日的 K 线，防御上游接口忽略截止日期。"""

    target_day = pd.Timestamp(_as_of_day(as_of_date))
    return (
        history.loc[history["date"].le(target_day)]
        .tail(INDICATOR_WARMUP_BARS)
        .reset_index(drop=True)
    )


def parse_eastmoney_history(
    payload: Mapping[str, Any],
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """解析东方财富日 K JSON，字段顺序对应 f51 至 f61。"""

    if str(payload.get("rc", "0")) not in {"0", "None"} and payload.get("rc") not in (0, None):
        raise ValueError(f"东方财富接口返回 rc={payload.get('rc')!r}。")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("东方财富响应没有 data 对象。")
    raw_klines = data.get("klines")
    if not isinstance(raw_klines, list):
        raise ValueError("东方财富响应没有日 K 列表。")

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
    history = _limit_history_to_as_of_date(
        _normalize_history_frame(pd.DataFrame(rows)),
        as_of_date,
    )
    if history.empty:
        raise ValueError("东方财富响应中没有可用的日 K 数据。")
    return history


def parse_tencent_history(
    payload: Mapping[str, Any],
    symbol: str,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """解析腾讯前复权日 K；该来源的历史数组不提供逐日成交额和换手率。"""

    if payload.get("code") not in (None, 0, "0"):
        raise ValueError(f"腾讯接口返回 code={payload.get('code')!r}。")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("腾讯响应没有 data 对象。")
    security_data = data.get(symbol)
    if not isinstance(security_data, Mapping):
        raise ValueError(f"腾讯响应没有 {symbol} 的日 K 数据。")
    # 前复权请求返回 qfqday；保留 day 兼容性，便于解析旧样本和接口临时降级。
    raw_klines = security_data.get("qfqday")
    if not isinstance(raw_klines, list):
        raw_klines = security_data.get("day")
    if not isinstance(raw_klines, list):
        raise ValueError("腾讯响应没有日 K 列表。")

    rows: list[dict[str, object]] = []
    for raw_kline in raw_klines:
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
    history = _limit_history_to_as_of_date(
        _normalize_history_frame(pd.DataFrame(rows)),
        as_of_date,
    )
    if history.empty:
        raise ValueError("腾讯响应中没有可用的日 K 数据。")
    return history


def parse_tencent_rich_history(
    raw_text: str,
    symbol: str,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """解析腾讯增强日 K 的 JS 赋值响应，补全逐日成交额和换手率。"""

    json_text = raw_text.strip()
    if "=" in json_text:
        json_text = json_text.split("=", 1)[1].strip()
    json_text = json_text.rstrip(";").strip()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError("腾讯增强日 K 响应不是有效 JSON。") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("腾讯增强日 K 响应不是对象。")
    if payload.get("code") not in (None, 0, "0"):
        raise ValueError(f"腾讯增强日 K 返回 code={payload.get('code')!r}。")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("腾讯增强日 K 响应没有 data 对象。")
    security_data = data.get(symbol)
    if not isinstance(security_data, Mapping):
        raise ValueError(f"腾讯增强日 K 响应没有 {symbol} 的日 K 数据。")
    raw_klines = security_data.get("qfqday")
    if not isinstance(raw_klines, list):
        raw_klines = security_data.get("day")
    if not isinstance(raw_klines, list):
        raise ValueError("腾讯增强日 K 响应没有日 K 列表。")

    rows: list[dict[str, object]] = []
    for raw_kline in raw_klines:
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
                # 第 7 位是换手率百分比；第 8 位是万元口径的成交额。
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
    history = _limit_history_to_as_of_date(
        _normalize_history_frame(pd.DataFrame(rows)),
        as_of_date,
    )
    if history.empty:
        raise ValueError("腾讯增强日 K 响应中没有可用的日 K 数据。")
    return history


def parse_tencent_quote_snapshot(raw_text: str, code: str) -> dict[str, object]:
    """解析腾讯实时快照中的日期、当日成交额和换手率。"""

    expected_prefix = f"v_sz{code}="
    for line in raw_text.split(";"):
        if not line.startswith(expected_prefix) or '"' not in line:
            continue
        fields = line.split('"', 2)[1].split("~")
        if len(fields) <= 38:
            continue
        date_text = fields[30].strip()[:8]
        quote_date = pd.to_datetime(date_text, format="%Y%m%d", errors="coerce")
        if pd.isna(quote_date):
            continue
        # 第 37 位是“万元”口径的当日成交额，第 38 位是换手率百分比。
        amount_in_ten_thousand = _coerce_number(fields[37])
        return {
            "date": pd.Timestamp(quote_date).normalize(),
            "amount": (
                amount_in_ten_thousand * 10_000
                if amount_in_ten_thousand is not None
                else None
            ),
            "turnover": _coerce_number(fields[38]),
        }
    raise ValueError(f"腾讯行情快照中没有 {code} 的有效记录。")


def supplement_tencent_latest_snapshot(
    history: pd.DataFrame, snapshot: Mapping[str, object] | None
) -> pd.DataFrame:
    """仅在快照日期与最后一根日线一致时补齐当日成交额和换手率。"""

    if snapshot is None or history.empty:
        return history
    snapshot_date = pd.to_datetime(snapshot.get("date"), errors="coerce")
    if pd.isna(snapshot_date):
        return history
    supplemented = history.copy()
    latest_index = supplemented.index[-1]
    latest_date = pd.Timestamp(supplemented.at[latest_index, "date"]).normalize()
    if latest_date != pd.Timestamp(snapshot_date).normalize():
        return history
    for column in ("amount", "turnover"):
        value = _coerce_number(snapshot.get(column))
        if value is not None:
            supplemented.at[latest_index, column] = value
    return supplemented


def _as_of_day(value: date | datetime | None = None) -> date:
    """规范化用户选择的筛选截至日期。"""

    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("筛选截至日期必须是 date 或 datetime。")


def _collection_matches_as_of_date(
    collected_as_of_date: object,
    as_of_date: date | datetime | None,
) -> bool:
    return collected_as_of_date == _as_of_day(as_of_date).isoformat()


def _cached_factors_match_as_of_date(
    factors: Mapping[str, object],
    as_of_date: date | datetime | None,
) -> bool:
    factor_date = pd.to_datetime(factors.get("数据日期"), errors="coerce")
    return not pd.isna(factor_date) and pd.Timestamp(factor_date).normalize() <= pd.Timestamp(
        _as_of_day(as_of_date)
    )


def _has_incomplete_tencent_liquidity(source: object, history: pd.DataFrame) -> bool:
    """旧版腾讯回退不缓存缺失量能，便于增强接口恢复后自动补齐。"""

    return (
        isinstance(source, str)
        and "腾讯" in source
        and not history.empty
        and (pd.isna(history["amount"].iloc[-1]) or pd.isna(history["turnover"].iloc[-1]))
    )


def _clear_collection_session_state() -> None:
    for key in COLLECTION_SESSION_STATE_KEYS:
        st.session_state.pop(key, None)


def default_screening_settings() -> dict[str, object]:
    """返回独立副本，避免调用方修改全局默认筛选配置。"""

    return deepcopy(SCREENING_WIDGET_DEFAULTS)


def platform_breakout_screening_settings() -> dict[str, object]:
    """返回“低位企稳后的平台突破”预设的完整筛选配置。"""

    settings = default_screening_settings()
    settings.update(PLATFORM_BREAKOUT_PRESET_OVERRIDES)
    return settings


def apply_screening_settings(
    state: MutableMapping[str, object], settings: Mapping[str, object]
) -> None:
    """整体写入筛选设置，供 Streamlit 回调及无界面测试共用。"""

    state.update(settings)


def load_optimized_parameter_overrides(
    path: Path = OPTIMIZED_PARAMETER_FILE,
) -> dict[str, tuple[float | int, float | int]]:
    """Read and validate the optimizer's interval settings from its JSON report."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取优化参数文件：{path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"优化参数文件不是 JSON 对象：{path}")

    best_settings = payload.get("best_settings")
    if not isinstance(best_settings, Mapping):
        raise ValueError(f"优化参数文件缺少 best_settings：{path}")

    overrides: dict[str, tuple[float | int, float | int]] = {}
    for key, (minimum, maximum, integer_only) in OPTIMIZED_PARAMETER_BOUNDS.items():
        if key not in best_settings:
            continue
        values = best_settings[key]
        if (
            not isinstance(values, (list, tuple))
            or len(values) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values)
        ):
            raise ValueError(f"优化参数 {key} 必须是两个数值组成的区间。")
        lower, upper = float(values[0]), float(values[1])
        if (
            not math.isfinite(lower)
            or not math.isfinite(upper)
            or lower > upper
            or lower < minimum
            or upper > maximum
        ):
            raise ValueError(f"优化参数 {key} 超出允许范围。")
        if integer_only:
            if not lower.is_integer() or not upper.is_integer():
                raise ValueError(f"优化参数 {key} 必须是整数区间。")
            overrides[key] = (int(lower), int(upper))
        else:
            overrides[key] = (lower, upper)

    if not overrides:
        raise ValueError(f"优化参数文件未包含可应用的区间参数：{path}")
    return overrides


def apply_optimized_parameter_overrides(
    state: MutableMapping[str, object],
    path: Path = OPTIMIZED_PARAMETER_FILE,
) -> None:
    """Overlay only optimizer-owned parameters, preserving module-specific rules."""

    state.update(load_optimized_parameter_overrides(path))


def _clear_screening_result_session_state() -> None:
    """预设切换后移除旧规则的筛选结果，保留已计算因子。"""

    for key in SCREENING_RESULT_SESSION_STATE_KEYS:
        st.session_state.pop(key, None)


def _initialize_screening_widget_state() -> None:
    """只在首次渲染时写入原有默认值，后续保留用户选择。"""

    for key, value in SCREENING_WIDGET_DEFAULTS.items():
        st.session_state.setdefault(key, deepcopy(value))


def _apply_screening_preset(preset_name: str) -> None:
    """在控件渲染前由按钮回调整体加载指定策略预设。"""

    if preset_name == "platform_breakout":
        settings = platform_breakout_screening_settings()
    elif preset_name == "default":
        settings = default_screening_settings()
    else:
        raise ValueError(f"未知筛选预设：{preset_name}")
    apply_screening_settings(st.session_state, settings)
    _clear_screening_result_session_state()


def _apply_optimized_parameter_overrides() -> None:
    apply_optimized_parameter_overrides(st.session_state)
    _clear_screening_result_session_state()


def _cache_path(code: str, as_of_date: date | datetime | None = None) -> Path:
    return CACHE_DIR / _as_of_day(as_of_date).isoformat() / f"{code}.json"


def _history_to_records(history: pd.DataFrame) -> list[dict[str, object]]:
    """将日线转为无 NaN 的 JSON 记录，便于原子写入持久缓存。"""

    serializable = history.loc[:, HISTORY_COLUMNS].copy()
    serializable["date"] = pd.to_datetime(serializable["date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    serializable = serializable.astype(object).where(serializable.notna(), None)
    return serializable.to_dict("records")


def _write_cache_record(
    code: str,
    *,
    source: str | None,
    history: pd.DataFrame | None,
    error: str | None,
    factors: Mapping[str, object] | None = None,
    as_of_date: date | datetime | None = None,
) -> None:
    """以临时文件替换方式写缓存，避免中断时留下半个 JSON 文件。"""

    target_day = _as_of_day(as_of_date)
    target = _cache_path(code, target_day)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "as_of_date": target_day.isoformat(),
        "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": source,
        "error": error,
        "bars": _history_to_records(history) if history is not None else [],
        "factor_cache_version": FACTOR_CACHE_VERSION if factors is not None else None,
        "factors": dict(factors) if factors is not None else None,
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _read_fresh_cache(
    code: str,
    cache_hours: float,
    *,
    as_of_date: date | datetime | None = None,
) -> FetchOutcome | None:
    """读取未过期缓存；失败缓存只短暂保留，防止持续重试同一错误。"""

    target_day = _as_of_day(as_of_date)
    target = _cache_path(code, target_day)
    if not target.is_file():
        return None
    age_seconds = max(0.0, time.time() - target.stat().st_mtime)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if payload.get("as_of_date") != target_day.isoformat():
            return None
        error = payload.get("error")
        source = payload.get("source")
        # 失败记录只缓存最多 15 分钟：既防止页面反复重试同一网络故障，也允许
        # 网络恢复后较快重新请求，不把临时故障误当成全天无数据。
        max_age_hours = min(max(0.0, float(cache_hours)), 0.25) if error else max(
            0.0, float(cache_hours)
        )
        if age_seconds > max_age_hours * 3600:
            return None
        if error:
            return FetchOutcome(
                code=code,
                history=None,
                source=str(source) if source else None,
                from_cache=True,
                error=f"缓存中的失败记录：{error}",
            )
        bars = payload.get("bars")
        if not isinstance(bars, list):
            return None
        history = _limit_history_to_as_of_date(
            _normalize_history_frame(pd.DataFrame(bars)),
            target_day,
        )
        if len(history) < MIN_REQUIRED_BARS:
            return None
        if _has_incomplete_tencent_liquidity(source, history):
            return None
        cached_factors = payload.get("factors")
        if (
            payload.get("factor_cache_version") != FACTOR_CACHE_VERSION
            or not isinstance(cached_factors, Mapping)
            or not _cached_factors_match_as_of_date(cached_factors, target_day)
        ):
            cached_factors = None
        return FetchOutcome(
            code=code,
            history=history.tail(INDICATOR_WARMUP_BARS).reset_index(drop=True),
            source=str(source) if source else "本地缓存",
            from_cache=True,
            factors=dict(cached_factors) if cached_factors is not None else None,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _eastmoney_parameters(
    code: str,
    *,
    as_of_date: date | datetime | None = None,
) -> dict[str, str]:
    """构造深市 A 股日 K 参数；明确起止日期以规避接口忽略 lmt 的情况。"""

    end_day = _as_of_day(as_of_date)
    start_day = end_day - timedelta(days=FETCH_CALENDAR_DAYS)
    return {
        "secid": f"0.{code}",
        "klt": "101",
        # 技术指标采用前复权价格，消除分红、送配等公司行为造成的价格跳空。
        "fqt": "1",
        "beg": start_day.strftime("%Y%m%d"),
        "end": end_day.strftime("%Y%m%d"),
        "lmt": str(INDICATOR_WARMUP_BARS),
        "fields1": EASTMONEY_FIELDS1,
        "fields2": EASTMONEY_FIELDS2,
    }


def _is_service_failure(exc: requests.RequestException) -> bool:
    """仅把连接失败、限流和服务端错误计入数据源熔断。"""

    response = getattr(exc, "response", None)
    if response is None:
        return True
    return response.status_code in {403, 408, 429} or response.status_code >= 500


def _is_rate_limited(exc: requests.RequestException) -> bool:
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in {403, 429}


def _fetch_eastmoney_history(
    code: str,
    limiter: RequestRateLimiter,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """请求东方财富完整日 K，网络或数据错误由调用方统一处理。"""

    parameters = _eastmoney_parameters(code, as_of_date=as_of_date)
    problems: list[str] = []
    service_failures = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            limiter.wait()
            response = _request_session().get(
                EASTMONEY_DAILY_KLINE_URL,
                params=parameters,
                timeout=15,
            )
            response.raise_for_status()
            history = parse_eastmoney_history(response.json(), as_of_date=as_of_date)
            if len(history) < MIN_REQUIRED_BARS:
                raise ValueError(f"仅返回 {len(history)} 根有效日线，少于 {MIN_REQUIRED_BARS} 根。")
            return history
        except requests.RequestException as exc:
            if _is_service_failure(exc):
                service_failures += 1
            if _is_rate_limited(exc):
                limiter.penalize(1.5 * attempt)
            problems.append(f"第 {attempt} 次：{exc}")
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * attempt)
        except (ValueError, TypeError) as exc:
            problems.append(f"第 {attempt} 次：{exc}")
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * attempt)
    raise FetchFailure(
        "；".join(problems),
        service_unavailable=service_failures == MAX_RETRIES,
    )


def _fetch_tencent_rich_history(
    code: str,
    limiter: RequestRateLimiter,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """请求腾讯增强前复权日 K，包含逐日成交额和换手率。"""

    end_day = _as_of_day(as_of_date)
    start_day = end_day - timedelta(days=FETCH_CALENDAR_DAYS)
    symbol = f"sz{code}"
    parameters = {
        "_var": "kline_dayqfq",
        "param": (
            f"{symbol},day,{start_day.isoformat()},{end_day.isoformat()},"
            f"{INDICATOR_WARMUP_BARS},qfq"
        ),
        "r": f"{time.time():.6f}",
    }
    problems: list[str] = []
    service_failures = 0
    total_requests = 0
    for attempt in range(1, MAX_RETRIES + 1):
        for endpoint in TENCENT_RICH_DAILY_KLINE_URLS:
            total_requests += 1
            try:
                limiter.wait()
                response = _request_session().get(
                    endpoint,
                    params=parameters,
                    timeout=15,
                )
                response.raise_for_status()
                history = parse_tencent_rich_history(
                    response.text,
                    symbol,
                    as_of_date=as_of_date,
                )
                if len(history) < MIN_REQUIRED_BARS:
                    raise ValueError(f"仅返回 {len(history)} 根有效日线，少于 {MIN_REQUIRED_BARS} 根。")
                return history
            except requests.RequestException as exc:
                if _is_service_failure(exc):
                    service_failures += 1
                if _is_rate_limited(exc):
                    limiter.penalize(1.5 * attempt)
                problems.append(f"第 {attempt} 次 {endpoint}：{exc}")
            except (ValueError, TypeError) as exc:
                problems.append(f"第 {attempt} 次 {endpoint}：{exc}")
        if attempt < MAX_RETRIES:
            time.sleep(0.8 * attempt)
    raise FetchFailure(
        "；".join(problems),
        service_unavailable=service_failures == total_requests,
    )


def _fetch_tencent_legacy_history(
    code: str,
    limiter: RequestRateLimiter,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """增强接口不可用时请求旧版腾讯前复权日 K。"""

    end_day = _as_of_day(as_of_date)
    start_day = end_day - timedelta(days=FETCH_CALENDAR_DAYS)
    symbol = f"sz{code}"
    parameters = {
        "param": (
            f"{symbol},day,{start_day.isoformat()},{end_day.isoformat()},"
            f"{INDICATOR_WARMUP_BARS},qfq"
        )
    }
    problems: list[str] = []
    service_failures = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            limiter.wait()
            response = _request_session().get(
                TENCENT_LEGACY_DAILY_KLINE_URL,
                params=parameters,
                timeout=15,
            )
            response.raise_for_status()
            history = parse_tencent_history(response.json(), symbol, as_of_date=as_of_date)
            if len(history) < MIN_REQUIRED_BARS:
                raise ValueError(f"仅返回 {len(history)} 根有效日线，少于 {MIN_REQUIRED_BARS} 根。")
            return history
        except requests.RequestException as exc:
            if _is_service_failure(exc):
                service_failures += 1
            if _is_rate_limited(exc):
                limiter.penalize(1.5 * attempt)
            problems.append(f"第 {attempt} 次：{exc}")
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * attempt)
        except (ValueError, TypeError) as exc:
            problems.append(f"第 {attempt} 次：{exc}")
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * attempt)
    raise FetchFailure(
        "；".join(problems),
        service_unavailable=service_failures == MAX_RETRIES,
    )


def _fetch_tencent_history(
    code: str,
    limiter: RequestRateLimiter,
    *,
    as_of_date: date | datetime | None = None,
) -> pd.DataFrame:
    """优先腾讯增强日 K，失败时保留旧版 OHLCV 回退。"""

    try:
        return _fetch_tencent_rich_history(code, limiter, as_of_date=as_of_date)
    except FetchFailure as rich_error:
        try:
            return _fetch_tencent_legacy_history(code, limiter, as_of_date=as_of_date)
        except FetchFailure as legacy_error:
            raise FetchFailure(
                f"腾讯增强日 K 失败：{rich_error}；旧版日 K 失败：{legacy_error}",
                service_unavailable=(
                    rich_error.service_unavailable and legacy_error.service_unavailable
                ),
            ) from legacy_error


def _fetch_tencent_quote_snapshot(code: str, limiter: RequestRateLimiter) -> dict[str, object] | None:
    """请求腾讯快照；快照不可用不影响历史日 K 回退本身。"""

    try:
        limiter.wait()
        response = _request_session().get(
            f"{TENCENT_QUOTE_URL}sz{code}",
            timeout=15,
        )
        response.raise_for_status()
        return parse_tencent_quote_snapshot(response.content.decode("gbk", errors="replace"), code)
    except (requests.RequestException, UnicodeDecodeError, ValueError):
        return None


def fetch_stock_history(
    code: str,
    *,
    cache_hours: float,
    force_refresh: bool,
    limiter: RequestRateLimiter,
    eastmoney_breaker: SourceCircuitBreaker | None = None,
    tencent_breaker: SourceCircuitBreaker | None = None,
    as_of_date: date | datetime | None = None,
) -> FetchOutcome:
    """获取单只股票日线：优先缓存，再主源，最后腾讯回退。"""

    target_day = _as_of_day(as_of_date)
    if not force_refresh:
        cached = _read_fresh_cache(code, cache_hours, as_of_date=target_day)
        if cached is not None:
            return cached

    eastmoney_breaker = eastmoney_breaker or SourceCircuitBreaker("东方财富")
    tencent_breaker = tencent_breaker or SourceCircuitBreaker("腾讯")
    try:
        if not eastmoney_breaker.allow_request():
            raise eastmoney_breaker.unavailable_error()
        history = _fetch_eastmoney_history(code, limiter, as_of_date=target_day)
        eastmoney_breaker.record_success()
    except FetchFailure as eastmoney_error:
        if eastmoney_error.service_unavailable:
            eastmoney_breaker.record_service_failure()
        try:
            if not tencent_breaker.allow_request():
                raise tencent_breaker.unavailable_error()
            history = _fetch_tencent_history(code, limiter, as_of_date=target_day)
            tencent_breaker.record_success()
        except FetchFailure as tencent_error:
            if tencent_error.service_unavailable:
                tencent_breaker.record_service_failure()
            message = f"东方财富失败：{eastmoney_error}；腾讯回退失败：{tencent_error}"
            _write_cache_record(
                code,
                source=None,
                history=None,
                error=message,
                as_of_date=target_day,
            )
            return FetchOutcome(code=code, history=None, source=None, from_cache=False, error=message)

        latest_amount = history["amount"].iloc[-1]
        latest_turnover = history["turnover"].iloc[-1]
        if target_day == date.today() and (pd.isna(latest_amount) or pd.isna(latest_turnover)):
            snapshot = _fetch_tencent_quote_snapshot(code, limiter)
            history = supplement_tencent_latest_snapshot(history, snapshot)
            latest_amount = history["amount"].iloc[-1]
            latest_turnover = history["turnover"].iloc[-1]
        if pd.notna(latest_amount) and pd.notna(latest_turnover):
            source = "腾讯前复权日K回退（成交额、换手率已提供）"
        else:
            source = "腾讯前复权日K回退（最新成交额、换手率缺失）"
        return FetchOutcome(code=code, history=history, source=source, from_cache=False)

    source = "东方财富公开日K（前复权）"
    return FetchOutcome(code=code, history=history, source=source, from_cache=False)


def _calculate_kdj_series(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """按国内常用 SMA 递推计算固定参数 KDJ(89,3,3)。

    K、D 的首个有效值以 50 为种子，随后分别使用 (2 * 前值 + 当期值) / 3
    递推。价格区间为零时 RSV 无定义，保持缺失而不伪造交易信号。
    """

    rolling_low = bars["low"].rolling(KDJ_RSV_PERIOD, min_periods=KDJ_RSV_PERIOD).min()
    rolling_high = bars["high"].rolling(KDJ_RSV_PERIOD, min_periods=KDJ_RSV_PERIOD).max()
    price_range = rolling_high - rolling_low
    rsv = ((bars["close"] - rolling_low) / price_range * 100.0).where(price_range.ne(0.0))

    previous_k = 50.0
    previous_d = 50.0
    k_values: list[float] = []
    d_values: list[float] = []
    for rsv_value in rsv:
        if pd.isna(rsv_value):
            k_values.append(float("nan"))
            d_values.append(float("nan"))
            continue
        current_k = (
            (KDJ_K_SMOOTHING_PERIOD - 1) * previous_k + float(rsv_value)
        ) / KDJ_K_SMOOTHING_PERIOD
        current_d = (
            (KDJ_D_SMOOTHING_PERIOD - 1) * previous_d + current_k
        ) / KDJ_D_SMOOTHING_PERIOD
        k_values.append(current_k)
        d_values.append(current_d)
        previous_k = current_k
        previous_d = current_d

    k_line = pd.Series(k_values, index=bars.index, dtype="float64")
    d_line = pd.Series(d_values, index=bars.index, dtype="float64")
    j_line = 3.0 * k_line - 2.0 * d_line
    return rsv, k_line, d_line, j_line


def _has_kdj_top_divergence(bars: pd.DataFrame) -> bool:
    """判断最近 60 个交易日内最后两个确认价格高点是否形成 KDJ 顶背离。

    确认高点定义为当日最高价不低于前一日且高于后一日。若后一个确认高点
    的价格更高、对应 J 值却更低，则判定为顶背离。没有两个可比较高点时不
    视为已检测到顶背离。
    """

    window = bars.loc[:, ["high", "kdj_j"]].tail(KDJ_DIVERGENCE_LOOKBACK_BARS).dropna()
    if len(window) < 3:
        return False

    highs = window["high"].tolist()
    j_values = window["kdj_j"].tolist()
    peak_positions: list[int] = []
    for position in range(1, len(window) - 1):
        if highs[position] >= highs[position - 1] and highs[position] > highs[position + 1]:
            peak_positions.append(position)
    if len(peak_positions) < 2:
        return False

    previous_peak, latest_peak = peak_positions[-2:]
    return bool(
        highs[latest_peak] > highs[previous_peak]
        and j_values[latest_peak] < j_values[previous_peak]
    )


def _latest_valid_macd_golden_cross(
    bars: pd.DataFrame,
) -> tuple[int | None, int | None]:
    """Return the latest valid completed MACD golden cross and its age."""

    completed_position = len(bars) - 1 - MACD_GOLDEN_CROSS_OFFSET_BARS
    minimum_position = max(
        1,
        completed_position - MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS + 1,
    )
    if completed_position < minimum_position:
        return None, None

    has_dead_cross_after = False
    for position in range(completed_position, minimum_position - 1, -1):
        if bool(bars["macd_dead_cross"].iloc[position]):
            has_dead_cross_after = True
            continue
        if has_dead_cross_after or not bool(bars["macd_golden_cross"].iloc[position]):
            continue
        return position, completed_position - position + 1

    return None, None


def _latest_healthy_kdj_golden_cross(
    bars: pd.DataFrame,
) -> tuple[int | None, int | None]:
    """返回最近有效 KDJ 金叉的位置和距今交易日数，0 表示当前选股日。

    当前选股日参与信号确认。候选金叉必须发生在超卖区、J 线上行、当时
    无顶背离，并且在该金叉后的交易日内没有死叉。
    """

    completed_position = len(bars) - 1 - KDJ_HEALTHY_GOLDEN_CROSS_OFFSET_BARS
    minimum_position = max(
        1,
        completed_position - MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE,
    )
    if completed_position < minimum_position:
        return None, None

    for position in range(completed_position, minimum_position - 1, -1):
        if not bool(bars["kdj_golden_cross"].iloc[position]):
            continue
        if bool(bars["kdj_dead_cross"].iloc[position + 1 : completed_position + 1].any()):
            continue

        current = bars.iloc[position]
        previous = bars.iloc[position - 1]
        in_oversold = bool(
            current["kdj_k"] < KDJ_OVERSOLD_THRESHOLD
            and current["kdj_d"] < KDJ_OVERSOLD_THRESHOLD
            and current["kdj_j"] < KDJ_OVERSOLD_THRESHOLD
        )
        j_rising = bool(
            pd.notna(current["kdj_j"])
            and pd.notna(previous["kdj_j"])
            and current["kdj_j"] > previous["kdj_j"]
        )
        if not in_oversold or not j_rising:
            continue
        if _has_kdj_top_divergence(bars.iloc[: position + 1]):
            continue
        return position, completed_position - position

    return None, None


def _recent_candlestick_risk_flags(bars: pd.DataFrame) -> dict[str, bool]:
    """返回最近三个实际交易日内出现的风险 K 线形态，窗口包含当天。"""

    recent_bars = bars.tail(CANDLESTICK_RISK_LOOKBACK_BARS)
    if recent_bars.empty:
        return {key: False for key in CANDLESTICK_RISK_PATTERN_KEYS}

    opens = recent_bars["open"]
    closes = recent_bars["close"]
    highs = recent_bars["high"]
    lows = recent_bars["low"]
    body = (closes - opens).abs()
    intraday_range = highs - lows
    body_top = pd.concat([opens, closes], axis=1).max(axis=1)
    body_bottom = pd.concat([opens, closes], axis=1).min(axis=1)
    upper_shadow = highs - body_top
    lower_shadow = body_bottom - lows
    valid_candle = (
        intraday_range.gt(0)
        & highs.ge(body_top)
        & lows.le(body_bottom)
        & upper_shadow.ge(0)
        & lower_shadow.ge(0)
    ).fillna(False)
    valid_range = intraday_range.where(valid_candle)
    body_ratio = body.div(valid_range)
    upper_shadow_ratio = upper_shadow.div(valid_range)
    lower_shadow_ratio = lower_shadow.div(valid_range)

    doji = valid_candle & body_ratio.le(
        CANDLESTICK_DOJI_MAX_BODY_RATIO + CANDLESTICK_COMPARISON_TOLERANCE
    ).fillna(False)
    inverted_t_doji = (
        doji
        & lower_shadow_ratio.le(
            CANDLESTICK_INVERTED_T_MAX_LOWER_SHADOW_RATIO
            + CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & upper_shadow_ratio.ge(
            CANDLESTICK_INVERTED_T_MIN_UPPER_SHADOW_RATIO
            - CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )
    prior_high = (
        bars["high"]
        .shift(1)
        .rolling(
            CANDLESTICK_HANGING_MAN_LOOKBACK_BARS,
            min_periods=CANDLESTICK_HANGING_MAN_LOOKBACK_BARS,
        )
        .max()
        .tail(len(recent_bars))
    )
    hanging_man = (
        valid_candle
        & prior_high.notna()
        & highs.ge(
            prior_high * CANDLESTICK_HANGING_MAN_HIGH_ZONE_RATIO
            - CANDLESTICK_COMPARISON_TOLERANCE
        )
        & body_ratio.le(
            CANDLESTICK_HANGING_MAN_MAX_BODY_RATIO + CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & lower_shadow_ratio.ge(
            CANDLESTICK_HANGING_MAN_MIN_LOWER_SHADOW_RATIO
            - CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
        & upper_shadow_ratio.le(
            CANDLESTICK_HANGING_MAN_MAX_UPPER_SHADOW_RATIO
            + CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )
    long_upper_shadow_bullish = (
        valid_candle
        & closes.gt(opens)
        & upper_shadow.gt(body + CANDLESTICK_COMPARISON_TOLERANCE)
    ).fillna(False)
    daily_close_change = bars["close"].pct_change(fill_method=None).mul(100.0).tail(
        len(recent_bars)
    )
    extreme_bullish = (
        valid_candle
        & closes.gt(opens)
        & daily_close_change.gt(
            CANDLESTICK_EXTREME_BULLISH_MIN_PCT_CHANGE
            + CANDLESTICK_COMPARISON_TOLERANCE
        ).fillna(False)
    )

    pattern_hits = {
        "doji_3d": doji,
        "inverted_t_doji_3d": inverted_t_doji,
        "hanging_man_3d": hanging_man,
        "long_upper_shadow_bullish_3d": long_upper_shadow_bullish,
        "extreme_bullish_3d": extreme_bullish,
    }
    return {key: bool(hit.any()) for key, hit in pattern_hits.items()}


def _calculate_factors_from_normalized_history(
    history: pd.DataFrame,
    *,
    as_of_date: date | datetime | None = None,
) -> dict[str, object]:
    """根据已规范化的日线计算最新交易日的趋势、动能、量能和波动因子。"""

    bars = _limit_history_to_as_of_date(history, as_of_date).copy()
    if len(bars) < MIN_REQUIRED_BARS:
        raise ValueError(f"可用于计算的日线不足 {MIN_REQUIRED_BARS} 根。")

    bars["ma5"] = bars["close"].rolling(5, min_periods=5).mean()
    bars["ma20"] = bars["close"].rolling(20, min_periods=20).mean()
    bars["volume_ma5"] = bars["volume"].shift(1).rolling(5, min_periods=5).mean()

    delta = bars["close"].diff()
    gains = delta.clip(lower=0)
    losses = (-delta.clip(upper=0))
    average_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    average_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = average_gain / average_loss
    bars["rsi14"] = 100.0 - 100.0 / (1.0 + relative_strength)
    bars.loc[(average_loss == 0) & (average_gain > 0), "rsi14"] = 100.0
    bars.loc[(average_gain == 0) & (average_loss > 0), "rsi14"] = 0.0

    # MACD 使用 DIF(12, 26) 与 DEA(9)；柱值采用国内常见的 2 倍差值表示。
    ema12 = bars["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = bars["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    bars["macd_dif"] = ema12 - ema26
    bars["macd_dea"] = bars["macd_dif"].ewm(span=9, adjust=False, min_periods=9).mean()
    bars["macd_histogram"] = 2.0 * (bars["macd_dif"] - bars["macd_dea"])
    bars["macd_golden_cross"] = (
        bars["macd_dif"].gt(bars["macd_dea"])
        & bars["macd_dif"].shift(1).le(bars["macd_dea"].shift(1))
    ).fillna(False)
    bars["macd_dead_cross"] = (
        bars["macd_dif"].lt(bars["macd_dea"])
        & bars["macd_dif"].shift(1).ge(bars["macd_dea"].shift(1))
    ).fillna(False)

    bars["kdj_rsv"], bars["kdj_k"], bars["kdj_d"], bars["kdj_j"] = _calculate_kdj_series(
        bars
    )
    bars["kdj_golden_cross"] = (
        bars["kdj_k"].gt(bars["kdj_d"])
        & bars["kdj_k"].shift(1).le(bars["kdj_d"].shift(1))
    ).fillna(False)
    bars["kdj_dead_cross"] = (
        bars["kdj_k"].lt(bars["kdj_d"])
        & bars["kdj_k"].shift(1).ge(bars["kdj_d"].shift(1))
    ).fillna(False)

    latest = bars.iloc[-1]
    previous = bars.iloc[-2]
    if pd.isna(latest["ma20"]) or pd.isna(latest["rsi14"]) or pd.isna(latest["macd_dea"]):
        raise ValueError("技术指标预热不足。")

    # 突破只比较截至前一交易日的 20 日平台，避免把当日最高价混入基准。
    prior_platform_high = (
        bars["high"]
        .shift(1)
        .rolling(PLATFORM_BREAKOUT_LOOKBACK_BARS, min_periods=PLATFORM_BREAKOUT_LOOKBACK_BARS)
        .max()
        .iloc[-1]
    )
    platform_breakout = bool(
        pd.notna(prior_platform_high)
        and pd.notna(latest["close"])
        and latest["close"] > prior_platform_high
    )
    platform_breakout_pct = (
        (latest["close"] - prior_platform_high) / prior_platform_high * 100.0
        if pd.notna(prior_platform_high) and prior_platform_high > 0
        else None
    )
    ma5_rising = bool(latest["ma5"] > previous["ma5"])
    volume_ratio = latest["volume"] / latest["volume_ma5"] if latest["volume_ma5"] > 0 else pd.NA
    macd_bullish = bool(latest["macd_dif"] > latest["macd_dea"])
    macd_bearish = bool(latest["macd_dif"] < latest["macd_dea"])
    macd_golden_cross = bool(latest["macd_golden_cross"])
    macd_dead_cross = bool(latest["macd_dead_cross"])
    macd_golden_cross_position, macd_golden_cross_age = (
        _latest_valid_macd_golden_cross(bars)
    )
    macd_golden_cross_date = (
        pd.Timestamp(bars.iloc[macd_golden_cross_position]["date"]).date().isoformat()
        if macd_golden_cross_position is not None
        else None
    )
    healthy_kdj_cross_position, healthy_kdj_cross_age = (
        _latest_healthy_kdj_golden_cross(bars)
    )
    healthy_kdj_cross_date = (
        pd.Timestamp(bars.iloc[healthy_kdj_cross_position]["date"]).date().isoformat()
        if healthy_kdj_cross_position is not None
        else None
    )

    def numeric_or_none(value: object) -> float | None:
        return None if pd.isna(value) else float(value)

    float_market_cap_yi = estimated_float_market_cap_yi(latest["amount"], latest["turnover"])

    # BIAS 用百分比呈现，数值 10 即表示题设中的 10%。
    bias20 = (latest["close"] - latest["ma20"]) / latest["ma20"] * 100.0
    intraday_range = latest["high"] - latest["low"]
    body_top = max(latest["open"], latest["close"])
    upper_shadow = max(0.0, latest["high"] - body_top)
    upper_shadow_ratio = (
        0.0
        if intraday_range <= 0
        else upper_shadow / intraday_range * 100.0
    )
    if pd.isna(intraday_range) or pd.isna(latest["close"]) or pd.isna(latest["low"]):
        close_position = None
    elif intraday_range > 0:
        close_position = (latest["close"] - latest["low"]) / intraday_range * 100.0
    elif latest["close"] == latest["high"]:
        # 一字价格没有日内振幅，收盘即为当日最高价。
        close_position = 100.0
    else:
        close_position = None
    close_near_daily_high = bool(
        close_position is not None and close_position >= CLOSE_NEAR_DAILY_HIGH_THRESHOLD
    )
    rolling_high_60 = bars["high"].shift(1).rolling(60, min_periods=60).max().iloc[-1]
    touches_60_day_resistance = bool(latest["close"] >= rolling_high_60 * 0.98)
    candlestick_risk_flags = _recent_candlestick_risk_flags(bars)

    return {
        "数据日期": pd.Timestamp(latest["date"]).date().isoformat(),
        "收盘价": numeric_or_none(latest["close"]),
        "MA5": numeric_or_none(latest["ma5"]),
        "MA20": numeric_or_none(latest["ma20"]),
        "站上MA5": bool(latest["close"] > latest["ma5"]),
        "站上MA20": bool(latest["close"] > latest["ma20"]),
        "MA5高于MA20": bool(latest["ma5"] > latest["ma20"]),
        "MA5上行": ma5_rising,
        "前20日平台最高价": numeric_or_none(prior_platform_high),
        "平台突破幅度（%）": numeric_or_none(platform_breakout_pct),
        "收盘突破前20日平台": platform_breakout,
        "RSI14": numeric_or_none(latest["rsi14"]),
        "MACD_DIF": numeric_or_none(latest["macd_dif"]),
        "MACD_DEA": numeric_or_none(latest["macd_dea"]),
        "MACD柱": numeric_or_none(latest["macd_histogram"]),
        "MACD多头": macd_bullish,
        "MACD空头": macd_bearish,
        "MACD金叉": macd_golden_cross,
        "MACD死叉": macd_dead_cross,
        MACD_GOLDEN_CROSS_DATE_COLUMN: macd_golden_cross_date,
        MACD_GOLDEN_CROSS_AGE_COLUMN: macd_golden_cross_age,
        "KDJ_K(89,3,3)": numeric_or_none(latest["kdj_k"]),
        "KDJ_D(89,3,3)": numeric_or_none(latest["kdj_d"]),
        "KDJ_J(89,3,3)": numeric_or_none(latest["kdj_j"]),
        "KDJ金叉": bool(latest["kdj_golden_cross"]),
        "KDJ死叉": bool(latest["kdj_dead_cross"]),
        KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN: healthy_kdj_cross_date,
        KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN: healthy_kdj_cross_age,
        KDJ_HEALTHY_GOLDEN_CROSS_NAME: healthy_kdj_cross_position is not None,
        "当日成交量": numeric_or_none(latest["volume"]),
        "5日均量": numeric_or_none(latest["volume_ma5"]),
        "量比": numeric_or_none(volume_ratio),
        "放量": bool(
            not pd.isna(volume_ratio)
            and DEFAULT_VOLUME_RATIO_RANGE[0]
            <= volume_ratio
            <= DEFAULT_VOLUME_RATIO_RANGE[1]
        ),
        "当日成交额": numeric_or_none(latest["amount"]),
        "换手率": numeric_or_none(latest["turnover"]),
        "估算流通市值（亿元）": float_market_cap_yi,
        "当日涨跌幅": numeric_or_none(latest["pct_change"]),
        "振幅": numeric_or_none(latest["amplitude"]),
        "BIAS20": numeric_or_none(bias20),
        "上影线比例": numeric_or_none(upper_shadow_ratio),
        "收盘日内位置（%）": numeric_or_none(close_position),
        "收盘位于日内高位": close_near_daily_high,
        "60日最高价": numeric_or_none(rolling_high_60),
        "触及60日高点压力": touches_60_day_resistance,
        **{
            CANDLESTICK_RISK_FACTOR_COLUMNS[key]: hit
            for key, hit in candlestick_risk_flags.items()
        },
    }


def calculate_factors(
    history: pd.DataFrame,
    *,
    as_of_date: date | datetime | None = None,
) -> dict[str, object]:
    """根据任意日线输入计算因子；对外保留规范化保护。"""

    return _calculate_factors_from_normalized_history(
        _normalize_history_frame(history),
        as_of_date=as_of_date,
    )


ProgressCallback = Callable[[int, int, str, int, int, int], None]


def collect_factor_frame(
    companies: pd.DataFrame,
    *,
    max_companies: int,
    cache_hours: float,
    force_refresh: bool,
    workers: int,
    request_interval_seconds: float,
    as_of_date: date | datetime | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """按指定截至日期批量读取缓存/行情并计算因子。"""

    selected_companies = companies.head(max(1, min(int(max_companies), len(companies)))).copy()
    records = selected_companies.to_dict("records")
    target_day = _as_of_day(as_of_date)
    limiter = RequestRateLimiter(request_interval_seconds)
    eastmoney_breaker = SourceCircuitBreaker("东方财富")
    tencent_breaker = SourceCircuitBreaker("腾讯")
    factor_rows: list[dict[str, object]] = []
    error_rows: list[dict[str, object]] = []
    completed = cache_hits = succeeded = failed = 0
    last_progress_at = 0.0
    last_progress_completed = 0

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), MAX_WORKERS))) as executor:
        futures = {
            executor.submit(
                fetch_stock_history,
                str(record["股票代码"]),
                cache_hours=cache_hours,
                force_refresh=force_refresh,
                limiter=limiter,
                eastmoney_breaker=eastmoney_breaker,
                tencent_breaker=tencent_breaker,
                as_of_date=target_day,
            ): record
            for record in records
        }
        for future in as_completed(futures):
            company = futures[future]
            code = str(company["股票代码"])
            name = str(company["股票名称"])
            completed += 1
            try:
                outcome = future.result()
            except Exception as exc:  # 防御性兜底，确保线程异常不终止整批任务。
                outcome = FetchOutcome(
                    code=code,
                    history=None,
                    source=None,
                    from_cache=False,
                    error=f"工作线程异常：{exc}",
                )

            if outcome.from_cache:
                cache_hits += 1
            if outcome.history is None:
                failed += 1
                error_rows.append(
                    {
                        "序号": company["序号"],
                        "股票代码": code,
                        "股票名称": name,
                        "失败原因": outcome.error or "未返回行情数据。",
                    }
                )
            else:
                try:
                    factor_values = outcome.factors
                    if factor_values is None:
                        factor_values = _calculate_factors_from_normalized_history(
                            outcome.history,
                            as_of_date=target_day,
                        )
                        # 因子版本升级后的本地日线仍可直接复用；将重算结果回写，
                        # 避免后续页面运行反复计算同一只股票。
                        _write_cache_record(
                            code,
                            source=outcome.source,
                            history=outcome.history,
                            error=None,
                            factors=factor_values,
                            as_of_date=target_day,
                        )
                    factor_rows.append(
                        {
                            "序号": company["序号"],
                            "股票代码": code,
                            "股票名称": name,
                            "所属行业": _industry_or_unknown(company.get("所属行业")),
                            "数据来源": outcome.source or "未知来源",
                            "缓存命中": outcome.from_cache,
                            **factor_values,
                        }
                    )
                    succeeded += 1
                except (OSError, ValueError, TypeError) as exc:
                    failed += 1
                    error_rows.append(
                        {
                            "序号": company["序号"],
                            "股票代码": code,
                            "股票名称": name,
                            "失败原因": f"因子计算失败：{exc}",
                        }
                    )

            if progress_callback is not None:
                now = time.monotonic()
                if (
                    completed == len(records)
                    or completed - last_progress_completed >= 10
                    or now - last_progress_at >= 1.0
                ):
                    progress_callback(completed, len(records), code, cache_hits, succeeded, failed)
                    last_progress_at = now
                    last_progress_completed = completed

    factors = pd.DataFrame(factor_rows)
    if not factors.empty:
        factor_dates = pd.to_datetime(factors["数据日期"], errors="coerce")
        latest_market_date = factor_dates.max()
        stale_mask = factor_dates.lt(latest_market_date)
        if bool(stale_mask.any()):
            stale_rows = factors.loc[stale_mask]
            error_rows.extend(
                {
                    "序号": row["序号"],
                    "股票代码": row["股票代码"],
                    "股票名称": row["股票名称"],
                    "失败原因": (
                        f"截至本轮实际交易日 {latest_market_date.date().isoformat()} 无日线，"
                        f"该股票最后数据日为 {row['数据日期']}。"
                    ),
                }
                for _, row in stale_rows.iterrows()
            )
            stale_count = int(stale_mask.sum())
            succeeded -= stale_count
            failed += stale_count
            factors = factors.loc[~stale_mask].copy()
        factors = factors.sort_values("序号", kind="stable").reset_index(drop=True)
    errors = pd.DataFrame(error_rows)
    if not errors.empty:
        errors = errors.sort_values("序号", kind="stable").reset_index(drop=True)
    return factors, errors, {"总数": len(records), "缓存命中": cache_hits, "成功": succeeded, "失败": failed}


def _range_bounds(values: tuple[float, float], label: str) -> tuple[float, float]:
    try:
        lower, upper = (float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须包含两个数值。") from exc
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise ValueError(f"{label}的下限不能高于上限。")
    return lower, upper


def _float_market_cap_range_bounds(values: tuple[float, float]) -> tuple[float, float]:
    """校验流通市值筛选范围，保持与界面滑块的可选边界一致。"""

    lower, upper = _range_bounds(values, "流通市值区间")
    if lower < FLOAT_MARKET_CAP_MIN_YI or upper > FLOAT_MARKET_CAP_MAX_YI:
        raise ValueError(
            "流通市值区间必须在"
            f"{FLOAT_MARKET_CAP_MIN_YI:g}至{FLOAT_MARKET_CAP_MAX_YI:g}亿元之间。"
        )
    return lower, upper


def _rsi_range_bounds(values: tuple[float, float]) -> tuple[float, float]:
    """校验 RSI 得分条件的可选区间。"""

    lower, upper = _range_bounds(values, "RSI区间")
    if lower < RSI_MIN_VALUE or upper > RSI_MAX_VALUE:
        raise ValueError(
            "RSI区间必须在"
            f"{RSI_MIN_VALUE:g}至{RSI_MAX_VALUE:g}之间。"
        )
    return lower, upper


def _macd_dea_minus_dif_range_bounds(
    values: tuple[float, float],
) -> tuple[float, float]:
    """校验 MACD DEA-DIF 得分条件的可选区间。"""

    lower, upper = _range_bounds(values, "MACD红线-蓝线区间")
    if (
        lower < MACD_DEA_MINUS_DIF_MIN_THRESHOLD
        or upper > MACD_DEA_MINUS_DIF_MAX_THRESHOLD
    ):
        raise ValueError(
            "MACD红线-蓝线区间必须在"
            f"{MACD_DEA_MINUS_DIF_MIN_THRESHOLD:g}至"
            f"{MACD_DEA_MINUS_DIF_MAX_THRESHOLD:g}之间。"
        )
    return lower, upper


def _volume_ratio_threshold(value: object) -> float:
    """校验放量条件使用的量比下限。"""

    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("量比阈值必须是数值。") from exc
    if not math.isfinite(threshold) or threshold < VOLUME_RATIO_MIN_THRESHOLD:
        raise ValueError(
            f"量比阈值必须是大于或等于{VOLUME_RATIO_MIN_THRESHOLD:g}的有限数值。"
        )
    return threshold


def _volume_ratio_range_bounds(values: object) -> tuple[float, float]:
    """校验回测放量条件使用的量比闭区间。"""

    lower, upper = _range_bounds(values, "量比区间")
    if lower < VOLUME_RATIO_MIN_THRESHOLD or upper > VOLUME_RATIO_MAX_THRESHOLD:
        raise ValueError(
            "量比区间必须在"
            f"{VOLUME_RATIO_MIN_THRESHOLD:g}至"
            f"{VOLUME_RATIO_MAX_THRESHOLD:g}之间。"
        )
    return lower, upper


def volume_breakout_condition_label(threshold: float) -> str:
    """返回旧版单阈值放量条件名称，供兼容调用方使用。"""

    return f"放量（量比>{threshold:g}）"


def volume_ratio_range_condition_label(values: tuple[float, float]) -> str:
    """返回带有当前量比闭区间的放量条件名称。"""

    lower, upper = _volume_ratio_range_bounds(values)
    return f"放量（量比{lower:g}-{upper:g}）"


def _macd_golden_cross_lookback_days(value: object) -> int:
    """Validate the selectable MACD golden-cross lookback in trading days."""

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("MACD金叉回看天数必须是整数。") from exc
    if (
        not math.isfinite(numeric_value)
        or not numeric_value.is_integer()
        or not 1 <= numeric_value <= MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS
    ):
        raise ValueError(
            "MACD金叉回看天数必须在1至"
            f"{MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS}个交易日之间。"
        )
    return int(numeric_value)


def macd_golden_cross_condition_label(lookback_days: int) -> str:
    """Return the display label for the active MACD golden-cross window."""

    return f"近{lookback_days}个已完成交易日MACD金叉"


def _kdj_healthy_golden_cross_age_range(
    value: object,
) -> tuple[int, int]:
    """校验 KDJ 健康金叉距今交易日区间，0 表示当前选股日。"""

    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("KDJ健康金叉距今交易日区间必须包含最小值和最大值。")
    try:
        minimum_value = float(value[0])
        maximum_value = float(value[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("KDJ健康金叉距今交易日区间必须是整数。") from exc
    if (
        not math.isfinite(minimum_value)
        or not math.isfinite(maximum_value)
        or not minimum_value.is_integer()
        or not maximum_value.is_integer()
        or not (
            MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE
            <= minimum_value
            <= maximum_value
            <= MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE
        )
    ):
        raise ValueError(
            "KDJ健康金叉距今交易日区间必须在"
            f"{MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE}至"
            f"{MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE}之间，且最小值不能大于最大值。"
        )
    return int(minimum_value), int(maximum_value)


def kdj_healthy_golden_cross_condition_label(
    age_range: tuple[int, int],
) -> str:
    """返回带有当前距今交易日区间的 KDJ 得分条件名称。"""

    minimum_age, maximum_age = _kdj_healthy_golden_cross_age_range(age_range)
    return f"KDJ金叉距今{minimum_age}-{maximum_age}个交易日且状态良好"


def condition_matrix(
    factors: pd.DataFrame,
    selected: Mapping[str, bool],
    *,
    turnover_range: tuple[float, float],
    float_market_cap_range_yi: tuple[float, float],
    pct_change_range: tuple[float, float],
    amplitude_threshold: float,
    rsi_range: tuple[float, float] = DEFAULT_RSI_RANGE,
    macd_golden_cross_lookback_days: int = (
        DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS
    ),
    kdj_healthy_golden_cross_age_range: tuple[int, int] = (
        DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE
    ),
    macd_dea_minus_dif_range: tuple[float, float] = DEFAULT_MACD_DEA_MINUS_DIF_RANGE,
    volume_ratio_range: tuple[float, float] = DEFAULT_VOLUME_RATIO_RANGE,
    volume_ratio_threshold: float | None = None,
) -> pd.DataFrame:
    """把已勾选的规则转换为布尔矩阵；每一列对应一个可得分条件。"""

    conditions: dict[str, pd.Series] = {}

    def truth(column: str) -> pd.Series:
        if column not in factors:
            return pd.Series(False, index=factors.index, dtype=bool)
        return factors[column].fillna(False).astype(bool)

    numeric_at_least = lambda column, threshold: pd.to_numeric(
        factors.get(column, pd.Series(index=factors.index, dtype="float64")), errors="coerce"
    ).ge(threshold).fillna(False)

    def numeric_in_range(column: str, lower: float, upper: float) -> pd.Series:
        if column not in factors:
            return pd.Series(False, index=factors.index, dtype=bool)
        return pd.to_numeric(factors[column], errors="coerce").between(
            lower, upper, inclusive="both"
        ).fillna(False)

    turnover_min, turnover_max = _range_bounds(turnover_range, "换手率区间")
    market_cap_min, market_cap_max = _float_market_cap_range_bounds(
        float_market_cap_range_yi
    )
    pct_change_min, pct_change_max = _range_bounds(pct_change_range, "涨幅区间")
    rsi_min, rsi_max = _rsi_range_bounds(rsi_range)
    macd_lookback_days = _macd_golden_cross_lookback_days(
        macd_golden_cross_lookback_days
    )
    kdj_age_min, kdj_age_max = _kdj_healthy_golden_cross_age_range(
        kdj_healthy_golden_cross_age_range
    )
    macd_gap_min, macd_gap_max = _macd_dea_minus_dif_range_bounds(
        macd_dea_minus_dif_range
    )
    volume_ratio_min, volume_ratio_max = _volume_ratio_range_bounds(
        volume_ratio_range
    )
    volume_threshold = (
        _volume_ratio_threshold(volume_ratio_threshold)
        if volume_ratio_threshold is not None
        else None
    )

    if selected.get("above_ma5"):
        conditions["站上5日线"] = truth("站上MA5")
    if selected.get("above_ma20"):
        conditions["站上20日线"] = truth("站上MA20")
    if selected.get("ma5_above_ma20"):
        conditions["MA5高于MA20"] = truth("MA5高于MA20")
    if selected.get("platform_breakout_20d"):
        conditions["收盘突破前20日平台"] = truth("收盘突破前20日平台")
    if selected.get("ma5_rising"):
        conditions["MA5上行"] = truth("MA5上行")
    if selected.get("close_near_daily_high"):
        conditions["收盘位于日内高位（上30%）"] = truth("收盘位于日内高位")
    if selected.get("rsi_in_range"):
        conditions[f"RSI区间[{rsi_min:g}, {rsi_max:g}]"] = numeric_in_range(
            "RSI14", rsi_min, rsi_max
        )
    if selected.get("macd_bullish"):
        conditions["MACD多头"] = truth("MACD多头")
    if selected.get("macd_golden_cross"):
        conditions[macd_golden_cross_condition_label(macd_lookback_days)] = (
            pd.to_numeric(
                factors.get(
                    MACD_GOLDEN_CROSS_AGE_COLUMN,
                    pd.Series(index=factors.index, dtype="float64"),
                ),
                errors="coerce",
            )
            .between(1, macd_lookback_days, inclusive="both")
            .fillna(False)
        )
    if selected.get("macd_bearish"):
        conditions["MACD空头"] = truth("MACD空头")
    if selected.get("macd_dead_cross"):
        conditions["当日MACD死叉"] = truth("MACD死叉")
    if selected.get("macd_dea_minus_dif_high"):
        conditions[
            f"MACD红线-蓝线区间[{macd_gap_min:g}, {macd_gap_max:g}]（DEA-DIF）"
        ] = pd.to_numeric(
            factors.get("MACD_DEA", pd.Series(index=factors.index, dtype="float64")),
            errors="coerce",
        ).sub(
            pd.to_numeric(
                factors.get("MACD_DIF", pd.Series(index=factors.index, dtype="float64")),
                errors="coerce",
            )
        ).between(
            macd_gap_min - MACD_DEA_MINUS_DIF_COMPARISON_TOLERANCE,
            macd_gap_max + MACD_DEA_MINUS_DIF_COMPARISON_TOLERANCE,
            inclusive="both",
        ).fillna(False)
    if selected.get("kdj_healthy_golden_cross_3d"):
        conditions[
            kdj_healthy_golden_cross_condition_label((kdj_age_min, kdj_age_max))
        ] = (
            pd.to_numeric(
                factors.get(
                    KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN,
                    pd.Series(index=factors.index, dtype="float64"),
                ),
                errors="coerce",
            )
            .between(kdj_age_min, kdj_age_max, inclusive="both")
            .fillna(False)
        )
    if selected.get("volume_breakout"):
        volume_ratio = pd.to_numeric(
            factors.get("量比", pd.Series(index=factors.index, dtype="float64")),
            errors="coerce",
        )
        if volume_threshold is None:
            conditions[
                volume_ratio_range_condition_label(
                    (volume_ratio_min, volume_ratio_max)
                )
            ] = volume_ratio.between(
                volume_ratio_min,
                volume_ratio_max,
                inclusive="both",
            ).fillna(False)
        else:
            conditions[volume_breakout_condition_label(volume_threshold)] = (
                volume_ratio.gt(volume_threshold).fillna(False)
            )
    if selected.get("amount_at_least_100m"):
        conditions["日成交额≥1亿元"] = numeric_at_least("当日成交额", 100_000_000.0)
    if selected.get("turnover_in_range"):
        conditions[f"换手率{turnover_min:g}-{turnover_max:g}%"] = numeric_in_range(
            "换手率", turnover_min, turnover_max
        )
    if selected.get("float_market_cap_in_range"):
        conditions[f"流通市值{market_cap_min:g}-{market_cap_max:g}亿元"] = numeric_in_range(
            "估算流通市值（亿元）", market_cap_min, market_cap_max
        )
    if selected.get("positive_change"):
        conditions["当日上涨"] = pd.to_numeric(
            factors.get("当日涨跌幅", pd.Series(index=factors.index, dtype="float64")),
            errors="coerce",
        ).gt(0.0).fillna(False)
    if selected.get("pct_change_in_range"):
        conditions[f"涨幅{pct_change_min:g}-{pct_change_max:g}%"] = numeric_in_range(
            "当日涨跌幅", pct_change_min, pct_change_max
        )
    if selected.get("amplitude_high"):
        conditions[f"振幅≥{amplitude_threshold:g}%"] = numeric_at_least("振幅", amplitude_threshold)

    return pd.DataFrame(conditions, index=factors.index, dtype=bool)


def maximum_score(selected: Mapping[str, bool]) -> float:
    """返回当前已启用得分规则的满分。"""

    return sum(
        weight for key, weight in SCORING_INDICATOR_WEIGHTS.items() if selected.get(key)
    )


def validate_selected_conditions(
    selected: Mapping[str, bool],
    *,
    require_all: bool,
    macd_dea_minus_dif_range: tuple[float, float] = DEFAULT_MACD_DEA_MINUS_DIF_RANGE,
) -> None:
    """“全部满足”模式下拒绝当前交易日不可能同时成立的条件组合。"""

    if not require_all:
        return
    incompatible_pairs: list[tuple[str, str, str, str]] = [
        ("macd_bullish", "macd_bearish", "MACD多头", "MACD空头"),
        ("macd_bullish", "macd_dead_cross", "MACD多头", "当日MACD死叉"),
    ]
    if selected.get("macd_dea_minus_dif_high"):
        macd_gap_min, macd_gap_max = _macd_dea_minus_dif_range_bounds(
            macd_dea_minus_dif_range
        )
        if macd_gap_min >= 0.0:
            incompatible_pairs.extend(
                (
                    (
                        "macd_bullish",
                        "macd_dea_minus_dif_high",
                        "MACD多头",
                        "MACD红线-蓝线区间",
                    ),
                )
            )
        if macd_gap_max <= 0.0:
            incompatible_pairs.extend(
                (
                    (
                        "macd_bearish",
                        "macd_dea_minus_dif_high",
                        "MACD空头",
                        "MACD红线-蓝线区间",
                    ),
                    (
                        "macd_dead_cross",
                        "macd_dea_minus_dif_high",
                        "当日MACD死叉",
                        "MACD红线-蓝线区间",
                    ),
                )
            )
    conflicts = [
        f"{left_label}、{right_label}"
        for left_key, right_key, left_label, right_label in incompatible_pairs
        if selected.get(left_key) and selected.get(right_key)
    ]
    if conflicts:
        raise ValueError("“全部满足”模式不能同时勾选互斥条件：" + "；".join(conflicts) + "。")


def risk_exclusion_matrix(
    factors: pd.DataFrame,
    selected_risks: Mapping[str, bool],
) -> pd.DataFrame:
    """返回风险规则命中矩阵；任一命中即从最终候选中剔除。"""

    risks: dict[str, pd.Series] = {}

    def numeric(column: str) -> pd.Series:
        """缺少新增因子时按未知处理，不把记录误判为风险命中。"""

        if column not in factors:
            return pd.Series(index=factors.index, dtype="float64")
        return pd.to_numeric(factors[column], errors="coerce")

    def boolean(column: str) -> pd.Series:
        """缺少形态因子时不命中，避免旧缓存被误判为风险。"""

        if column not in factors:
            return pd.Series(False, index=factors.index, dtype=bool)
        return factors[column].fillna(False).astype(bool)

    if selected_risks.get("bias_high"):
        risks["BIAS>10%（偏离20日线过远）"] = numeric("BIAS20").gt(10.0).fillna(False)
    if selected_risks.get("upper_shadow"):
        risks["上影线>30%且涨幅<3%（冲高回落）"] = (
            numeric("上影线比例").gt(30.0) & numeric("当日涨跌幅").lt(3.0)
        ).fillna(False)
    if selected_risks.get("resistance_60_day"):
        resistance = factors.get(
            "触及60日高点压力",
            pd.Series(False, index=factors.index, dtype=bool),
        )
        risks["触及60日高点压力区"] = resistance.fillna(False).astype(bool)
    for risk_key in CANDLESTICK_RISK_PATTERN_KEYS:
        if selected_risks.get(risk_key):
            risks[CANDLESTICK_RISK_EXCLUSION_LABELS[risk_key]] = boolean(
                CANDLESTICK_RISK_FACTOR_COLUMNS[risk_key]
            )
    return pd.DataFrame(risks, index=factors.index, dtype=bool)


def score_and_select(
    factors: pd.DataFrame,
    selected: Mapping[str, bool],
    *,
    selected_risks: Mapping[str, bool] | None = None,
    turnover_range: tuple[float, float] = DEFAULT_TURNOVER_RANGE,
    float_market_cap_range_yi: tuple[float, float] = DEFAULT_FLOAT_MARKET_CAP_RANGE_YI,
    pct_change_range: tuple[float, float] = DEFAULT_PCT_CHANGE_RANGE,
    amplitude_threshold: float = 3.0,
    rsi_range: tuple[float, float] = DEFAULT_RSI_RANGE,
    macd_golden_cross_lookback_days: int = (
        DEFAULT_MACD_GOLDEN_CROSS_LOOKBACK_DAYS
    ),
    kdj_healthy_golden_cross_age_range: tuple[int, int] = (
        DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE
    ),
    macd_dea_minus_dif_range: tuple[float, float] = DEFAULT_MACD_DEA_MINUS_DIF_RANGE,
    volume_ratio_range: tuple[float, float] = DEFAULT_VOLUME_RATIO_RANGE,
    volume_ratio_threshold: float | None = None,
    require_all: bool = False,
    top_n: int = 10,
) -> tuple[pd.DataFrame, int, int]:
    """按勾选条件打分，再按已启用风险规则剔除并取前十。"""

    validate_selected_conditions(
        selected,
        require_all=require_all,
        macd_dea_minus_dif_range=macd_dea_minus_dif_range,
    )
    if factors.empty:
        return pd.DataFrame(), 0, 0
    matrix = condition_matrix(
        factors,
        selected,
        turnover_range=turnover_range,
        float_market_cap_range_yi=float_market_cap_range_yi,
        pct_change_range=pct_change_range,
        amplitude_threshold=amplitude_threshold,
        rsi_range=rsi_range,
        macd_golden_cross_lookback_days=macd_golden_cross_lookback_days,
        kdj_healthy_golden_cross_age_range=kdj_healthy_golden_cross_age_range,
        macd_dea_minus_dif_range=macd_dea_minus_dif_range,
        volume_ratio_range=volume_ratio_range,
        volume_ratio_threshold=volume_ratio_threshold,
    )
    if matrix.empty:
        raise ValueError("请至少勾选一个筛选条件。")

    scored = factors.copy()
    if volume_ratio_threshold is None:
        volume_condition_label = volume_ratio_range_condition_label(
            _volume_ratio_range_bounds(volume_ratio_range)
        )
    else:
        volume_condition_label = volume_breakout_condition_label(
            _volume_ratio_threshold(volume_ratio_threshold)
        )
    if selected.get("volume_breakout") and volume_condition_label in matrix:
        scored["放量"] = matrix[volume_condition_label]
    kdj_condition_label = kdj_healthy_golden_cross_condition_label(
        _kdj_healthy_golden_cross_age_range(kdj_healthy_golden_cross_age_range)
    )
    condition_weights = pd.Series(
        {
            column: (
                SCORING_INDICATOR_WEIGHTS["kdj_healthy_golden_cross_3d"]
                if column == kdj_condition_label
                else 1.0
            )
            for column in matrix.columns
        },
        dtype="float64",
    )

    def penalty_label(condition: str) -> str:
        display_name = condition
        return f"{display_name}（-{condition_weights[condition]:g}分）"

    scored["得分"] = matrix.astype(float).mul(condition_weights, axis=1).sum(axis=1)
    scored["满足条件"] = matrix.apply(
        lambda row: "；".join(column for column, matched in row.items() if bool(matched)),
        axis=1,
    )
    scored["未满足条件（扣分项）"] = matrix.apply(
        lambda row: "；".join(
            penalty_label(column) for column, matched in row.items() if not bool(matched)
        )
        or "无",
        axis=1,
    )
    if require_all:
        eligible = scored.loc[matrix.all(axis=1)].copy()
    else:
        eligible = scored.loc[scored["得分"].gt(0)].copy()

    risk_matrix = risk_exclusion_matrix(scored, selected_risks or {})
    risk_excluded_count = 0
    if not risk_matrix.empty and not eligible.empty:
        risk_hit = risk_matrix.any(axis=1)
        risk_excluded_count = int(risk_hit.loc[eligible.index].sum())
        eligible = eligible.loc[~risk_hit.loc[eligible.index]].copy()

    eligible["_排序量比"] = pd.to_numeric(eligible.get("量比"), errors="coerce")
    eligible["_排序收盘位置"] = pd.to_numeric(
        eligible.get("收盘日内位置（%）"), errors="coerce"
    )
    eligible["_排序成交额"] = pd.to_numeric(eligible.get("当日成交额"), errors="coerce")
    eligible = eligible.sort_values(
        ["得分", "_排序成交额", "_排序量比", "_排序收盘位置", "股票代码"],
        ascending=[False, False, False, False, True],
        kind="stable",
        na_position="last",
    )
    result_columns = [
        "序号",
        "股票代码",
        "股票名称",
        "得分",
        "满足条件",
        "未满足条件（扣分项）",
        "数据日期",
        "收盘价",
        "MA5",
        "MA20",
        "MA5上行",
        "前20日平台最高价",
        "平台突破幅度（%）",
        "收盘突破前20日平台",
        "RSI14",
        "KDJ_K(89,3,3)",
        "KDJ_D(89,3,3)",
        "KDJ_J(89,3,3)",
        MACD_GOLDEN_CROSS_DATE_COLUMN,
        MACD_GOLDEN_CROSS_AGE_COLUMN,
        KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN,
        KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN,
        KDJ_HEALTHY_GOLDEN_CROSS_NAME,
        "量比",
        "当日成交额",
        "换手率",
        "估算流通市值（亿元）",
        "当日涨跌幅",
        "振幅",
        "BIAS20",
        "上影线比例",
        *CANDLESTICK_RISK_FACTOR_COLUMNS.values(),
        "收盘日内位置（%）",
        "收盘位于日内高位",
        "60日最高价",
        "数据来源",
    ]
    return (
        eligible.loc[
            :, [column for column in result_columns if column in eligible.columns]
        ]
        .head(max(1, int(top_n)))
        .reset_index(drop=True),
        len(eligible),
        risk_excluded_count,
    )


def _ranked_candidates_with_industry(
    ranked_candidates: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    """为已排序候选附加行业，且不改变原有排名或 Top 10 展示数据。"""

    candidates = ranked_candidates.copy()
    if "所属行业" not in candidates:
        if {"股票代码", "所属行业"}.issubset(factors.columns):
            industry_lookup = factors.loc[:, ["股票代码", "所属行业"]].copy()
            industry_lookup["股票代码"] = industry_lookup["股票代码"].astype(str)
            industry_by_code = (
                industry_lookup.drop_duplicates(subset=["股票代码"], keep="first")
                .set_index("股票代码")["所属行业"]
            )
            candidates["所属行业"] = candidates["股票代码"].astype(str).map(
                industry_by_code
            )
        else:
            candidates["所属行业"] = UNKNOWN_INDUSTRY
    candidates["所属行业"] = candidates["所属行业"].map(_industry_or_unknown)
    return candidates


def prepare_prediction_review_candidates(
    ranked_candidates: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    """整理风险过滤后的前 50 名，供人工按行业判断。"""

    candidates = _ranked_candidates_with_industry(ranked_candidates, factors).head(
        PREDICTION_REVIEW_TOP_N
    )
    if candidates.empty:
        return candidates

    candidates = candidates.reset_index(drop=True)
    candidates.insert(
        0,
        PREDICTION_REVIEW_RANK_COLUMN,
        range(1, len(candidates) + 1),
    )
    leading_columns = (
        PREDICTION_REVIEW_RANK_COLUMN,
        "股票代码",
        "股票名称",
        "所属行业",
        "得分",
    )
    ordered_columns = [
        column for column in leading_columns if column in candidates.columns
    ]
    ordered_columns.extend(
        column for column in candidates.columns if column not in ordered_columns
    )
    return candidates.loc[:, ordered_columns]


def summarize_prediction_review_industries(
    review_candidates: pd.DataFrame,
) -> pd.DataFrame:
    """汇总风险过滤后前 50 名中各行业的入选数量。"""

    summary_columns = ["所属行业", PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN]
    candidates = review_candidates.head(PREDICTION_REVIEW_TOP_N)
    if candidates.empty:
        return pd.DataFrame(columns=summary_columns)

    if "所属行业" in candidates:
        industries = candidates["所属行业"].map(_industry_or_unknown)
    else:
        industries = pd.Series(UNKNOWN_INDUSTRY, index=candidates.index)
    industry_summary = (
        industries.value_counts()
        .rename(PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN)
        .rename_axis("所属行业")
        .reset_index()
    )
    return industry_summary.sort_values(
        [PREDICTION_REVIEW_INDUSTRY_COUNT_COLUMN, "所属行业"],
        ascending=[False, True],
        kind="stable",
    ).reset_index(drop=True)


def clear_disk_cache() -> None:
    """仅删除本应用自己的缓存目录，不触碰项目其他数据。"""

    if CACHE_DIR.is_dir():
        shutil.rmtree(CACHE_DIR)
    load_mainboard_companies_cached.clear()


def _format_number(value: object, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.{digits}f}"


def _style_result_table(frame: pd.DataFrame) -> pd.io.formats.style.Styler:
    """突出得分、命中条件和扣分项，保留运维型表格的紧凑可读性。"""

    def row_style(row: pd.Series) -> list[str]:
        styles = [""] * len(row)
        score_position = row.index.get_loc("得分")
        conditions_position = row.index.get_loc("满足条件")
        styles[score_position] = "background-color: #d7f3df; color: #0a5132; font-weight: 700"
        styles[conditions_position] = "background-color: #f4f8e8"
        penalty_column = "未满足条件（扣分项）"
        if penalty_column in row.index and row[penalty_column] != "无":
            penalty_position = row.index.get_loc(penalty_column)
            styles[penalty_position] = "background-color: #fde8e7; color: #8c2d24"
        return styles

    numeric_columns = [
        "收盘价",
        "MA5",
        "MA20",
        "前20日平台最高价",
        "平台突破幅度（%）",
        "RSI14",
        "KDJ_K(89,3,3)",
        "KDJ_D(89,3,3)",
        "KDJ_J(89,3,3)",
        "量比",
        "换手率",
        "估算流通市值（亿元）",
        "当日涨跌幅",
        "振幅",
        "BIAS20",
        "上影线比例",
        "收盘日内位置（%）",
        "60日最高价",
    ]
    formats = {column: "{:.2f}" for column in numeric_columns if column in frame.columns}
    if "得分" in frame.columns:
        formats["得分"] = "{:g}"
    if "当日成交额" in frame.columns:
        formats["当日成交额"] = "{:,.0f}"
    return frame.style.apply(row_style, axis=1).format(formats, na_rep="-")


def _render_sidebar(company_count: int) -> dict[str, object]:
    """渲染数据获取与全部因子条件，返回当前界面配置。"""

    _initialize_screening_widget_state()
    with st.sidebar:
        st.header("数据获取")
        as_of_date = st.date_input(
            "筛选截至日期",
            value=date.today(),
            max_value=date.today(),
            help="非交易日会使用此前最近一个可用交易日的数据。",
        )
        max_companies = int(
            st.number_input(
                "本次处理股票数",
                min_value=1,
                max_value=company_count,
                value=company_count,
                step=50,
            )
        )
        cache_hours = float(
            st.number_input(
                "行情缓存有效小时数",
                min_value=0.0,
                max_value=168.0,
                value=DEFAULT_CACHE_HOURS,
                step=1.0,
            )
        )
        force_refresh = st.checkbox("忽略缓存，重新请求行情", value=False)
        workers = int(
            st.slider("并发请求数", min_value=1, max_value=MAX_WORKERS, value=DEFAULT_WORKERS)
        )
        request_interval = float(
            st.number_input(
                "请求最小间隔（秒）",
                min_value=0.25,
                max_value=3.0,
                value=DEFAULT_REQUEST_INTERVAL_SECONDS,
                step=0.05,
                help="所有工作线程共用此限速。较低的值会加快首次扫描，但可能触发公开接口风控。",
            )
        )
        if st.button("清除本应用行情缓存", width="stretch"):
            clear_disk_cache()
            _clear_collection_session_state()
            st.success("已清除本应用的行情缓存。")

        st.divider()
        st.header("策略预设")
        preset_columns = st.columns([3, 1, 2], vertical_alignment="center")
        preset_columns[0].button(
            "低位企稳后的平台突破",
            type="primary",
            icon=":material/trending_up:",
            width="stretch",
            on_click=_apply_screening_preset,
            args=("platform_breakout",),
        )
        preset_columns[1].button(
            "默认",
            icon=":material/restart_alt:",
            width="stretch",
            on_click=_apply_screening_preset,
            args=("default",),
        )
        preset_columns[2].button(
            "加载优化参数",
            icon=":material/tune:",
            width="stretch",
            on_click=_apply_optimized_parameter_overrides,
        )

        st.divider()
        st.header("趋势因子")
        selected = {
            "above_ma5": st.checkbox("站上5日线", key="szse_quant_filter_above_ma5"),
            "above_ma20": st.checkbox("站上20日线", key="szse_quant_filter_above_ma20"),
            "ma5_above_ma20": st.checkbox(
                "MA5高于MA20", key="szse_quant_filter_ma5_above_ma20"
            ),
        }

        st.header("平台突破因子")
        selected.update(
            {
                "platform_breakout_20d": st.checkbox(
                    "收盘突破前20日平台",
                    key="szse_quant_filter_platform_breakout_20d",
                    help="收盘价严格高于此前20个实际交易日的最高价，不将当天高点计入平台。",
                ),
                "ma5_rising": st.checkbox(
                    "MA5上行", key="szse_quant_filter_ma5_rising"
                ),
                "close_near_daily_high": st.checkbox(
                    "收盘位于日内高位（上30%）",
                    key="szse_quant_filter_close_near_daily_high",
                ),
            }
        )

        st.header("动能因子")
        selected.update(
            {
                "rsi_in_range": st.checkbox(
                    "RSI区间", key="szse_quant_filter_rsi_in_range"
                ),
                "macd_bullish": st.checkbox(
                    "MACD多头（DIF>DEA）", key="szse_quant_filter_macd_bullish"
                ),
                "macd_golden_cross": st.checkbox(
                    "最近N日MACD金叉（1分）",
                    key="szse_quant_filter_macd_golden_cross",
                    help="仅检查当前选股日前已完成的交易日；金叉后在该窗口内出现死叉时失效。",
                ),
                "macd_bearish": st.checkbox(
                    "MACD空头（DIF<DEA）", key="szse_quant_filter_macd_bearish"
                ),
                "macd_dead_cross": st.checkbox(
                    "当日MACD死叉", key="szse_quant_filter_macd_dead_cross"
                ),
                "macd_dea_minus_dif_high": st.checkbox(
                    "MACD红线-蓝线区间（DEA-DIF）",
                    key="szse_quant_filter_macd_dea_minus_dif_high",
                    help="当日 DEA-DIF 位于所设区间内（含边界）时，作为一项 1 分的得分条件。",
                ),
                "kdj_healthy_golden_cross_3d": st.checkbox(
                    "KDJ金叉且状态良好（1.5分）",
                    key="szse_quant_filter_kdj_healthy_golden_cross_3d",
                    help=(
                        "固定参数 KDJ(89,3,3)。在设定的交易日窗口内（含当日），金叉日 K、D、J "
                        "均低于 20、J 线上行、无顶背离，且金叉后未出现死叉。"
                    ),
                ),
            }
        )
        rsi_range = tuple(
            float(value)
            for value in st.slider(
                "RSI区间",
                min_value=RSI_MIN_VALUE,
                max_value=RSI_MAX_VALUE,
                step=0.1,
                key="szse_quant_filter_rsi_range",
                help=(
                    "当日 RSI14 位于该区间内（含边界）时满足该得分条件；"
                    f"默认值为 {DEFAULT_RSI_RANGE[0]:g} 至 {DEFAULT_RSI_RANGE[1]:g}。"
                ),
            )
        )
        macd_dea_minus_dif_range = tuple(
            float(value)
            for value in st.slider(
                "MACD红线-蓝线区间（DEA-DIF）",
                min_value=MACD_DEA_MINUS_DIF_MIN_THRESHOLD,
                max_value=MACD_DEA_MINUS_DIF_MAX_THRESHOLD,
                step=0.01,
                key="szse_quant_filter_macd_dea_minus_dif_range",
                help="当日 DEA-DIF 位于该区间内（含边界）时满足该得分条件；默认值为 0.1 至 0.2。",
            )
        )
        macd_golden_cross_lookback_days = int(
            st.number_input(
                "MACD金叉回看天数（交易日）",
                min_value=1,
                max_value=MAX_MACD_GOLDEN_CROSS_LOOKBACK_DAYS,
                step=1,
                key="szse_quant_filter_macd_golden_cross_lookback_days",
                help="1 表示只判断昨天；默认值为 3，窗口不包含当日。",
            )
        )
        kdj_healthy_golden_cross_age_range = tuple(
            int(value)
            for value in st.slider(
                "KDJ金叉出现时间（距今交易日）",
                min_value=MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE,
                max_value=MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE,
                step=1,
                key="szse_quant_filter_kdj_healthy_golden_cross_age_range",
                help="0 表示当日，区间包含边界；默认值为 1 至 3。",
            )
        )

        st.header("量能因子")
        selected.update(
            {
                "volume_breakout": st.checkbox(
                    "放量（量比区间）", key="szse_quant_filter_volume_breakout"
                ),
                "amount_at_least_100m": st.checkbox(
                    "日成交额≥1亿元", key="szse_quant_filter_amount_at_least_100m"
                ),
                "turnover_in_range": st.checkbox(
                    "换手率区间", key="szse_quant_filter_turnover_in_range"
                ),
            }
        )
        volume_ratio_range = tuple(
            float(value)
            for value in st.slider(
                "量比区间",
                min_value=VOLUME_RATIO_MIN_THRESHOLD,
                max_value=VOLUME_RATIO_MAX_THRESHOLD,
                step=0.1,
                key="szse_quant_filter_volume_ratio_range",
                help=(
                    "量比位于区间内（含边界）时满足放量条件；"
                    f"默认值为 {DEFAULT_VOLUME_RATIO_RANGE[0]:g} 至 "
                    f"{DEFAULT_VOLUME_RATIO_RANGE[1]:g}。"
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
                key="szse_quant_filter_turnover_range",
            )
        )

        st.header("市值因子")
        selected.update(
            {
                "float_market_cap_in_range": st.checkbox(
                    "流通市值区间",
                    key="szse_quant_filter_float_market_cap_in_range",
                    help="按同日成交额和换手率换算为成交均价口径；缺少任一数据时不满足此条件。",
                ),
            }
        )
        float_market_cap_range_yi = tuple(
            float(value)
            for value in st.slider(
                "流通市值区间（亿元）",
                min_value=FLOAT_MARKET_CAP_MIN_YI,
                max_value=FLOAT_MARKET_CAP_MAX_YI,
                step=1.0,
                key="szse_quant_filter_float_market_cap_range_yi",
            )
        )

        st.header("波动因子")
        selected.update(
            {
                "positive_change": st.checkbox(
                    "当日上涨", key="szse_quant_filter_positive_change"
                ),
                "pct_change_in_range": st.checkbox(
                    "涨幅区间", key="szse_quant_filter_pct_change_in_range"
                ),
                "amplitude_high": st.checkbox(
                    "振幅高于阈值", key="szse_quant_filter_amplitude_high"
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
                key="szse_quant_filter_pct_change_range",
            )
        )
        amplitude_threshold = float(
            st.number_input(
                "振幅阈值（%）",
                min_value=0.0,
                max_value=30.0,
                step=0.1,
                key="szse_quant_filter_amplitude_threshold",
            )
        )
        max_score = maximum_score(selected)
        selected_score_count = sum(bool(selected.get(key)) for key in SCORING_INDICATOR_KEYS)
        st.caption(f"当前勾选 {selected_score_count} 项得分指标，满分 {max_score:g} 分。")
        require_all = st.checkbox(
            "仅保留满足全部勾选条件的股票",
            key="szse_quant_filter_require_all",
            help="默认模式为至少满足一个条件即可参与排名；普通条件每项 1 分，KDJ 健康金叉为 1.5 分。",
        )

        st.divider()
        st.header("风险过滤（命中即剔除）")
        candlestick_patterns = st.multiselect(
            "K线形状",
            options=CANDLESTICK_RISK_PATTERN_KEYS,
            format_func=CANDLESTICK_RISK_PATTERN_LABELS.__getitem__,
            key="szse_quant_risk_candlestick_patterns",
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
                key="szse_quant_risk_bias_high",
            ),
            "upper_shadow": st.checkbox(
                "剔除上影线>30%且涨幅<3%（冲高回落）",
                key="szse_quant_risk_upper_shadow",
            ),
            "resistance_60_day": st.checkbox(
                "剔除触及60日高点压力区（距最高价2%内）",
                key="szse_quant_risk_resistance_60_day",
            ),
            **{
                risk_key: risk_key in candlestick_patterns
                for risk_key in CANDLESTICK_RISK_PATTERN_KEYS
            },
        }

        st.divider()
        fetch_clicked = st.button("获取行情并计算因子", type="primary", width="stretch")
        screen_clicked = st.button("开始筛选", width="stretch")

    return {
        "as_of_date": _as_of_day(as_of_date),
        "max_companies": max_companies,
        "cache_hours": cache_hours,
        "force_refresh": force_refresh,
        "workers": workers,
        "request_interval": request_interval,
        "selected": selected,
        "max_score": max_score,
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
        "fetch_clicked": fetch_clicked,
        "screen_clicked": screen_clicked,
    }


def _run_collection(companies: pd.DataFrame, settings: Mapping[str, object]) -> None:
    """在主线程更新进度条，后台线程只负责网络和计算，不直接调用 Streamlit。"""

    target_day = _as_of_day(settings.get("as_of_date"))
    progress_bar = st.progress(0, text="准备读取缓存和公开行情数据")
    progress_text = st.empty()

    def update(completed: int, total: int, code: str, cache_hits: int, succeeded: int, failed: int) -> None:
        percent = int(completed / total * 100) if total else 100
        detail = (
            f"已处理 {completed}/{total}：{code}；缓存命中 {cache_hits}，"
            f"成功 {succeeded}，失败 {failed}"
        )
        progress_bar.progress(percent, text=detail)
        progress_text.caption(detail)

    factors, errors, summary = collect_factor_frame(
        companies,
        max_companies=int(settings["max_companies"]),
        cache_hours=float(settings["cache_hours"]),
        force_refresh=bool(settings["force_refresh"]),
        workers=int(settings["workers"]),
        request_interval_seconds=float(settings["request_interval"]),
        as_of_date=target_day,
        progress_callback=update,
    )
    progress_bar.progress(100, text="行情和因子计算完成")
    st.session_state["szse_quant_factors"] = factors
    st.session_state["szse_quant_errors"] = errors
    st.session_state["szse_quant_summary"] = summary
    st.session_state["szse_quant_as_of_date"] = target_day.isoformat()
    _clear_screening_result_session_state()


def _render_factor_status() -> None:
    """展示本轮因子覆盖范围和失败清单，便于判断结果完整性。"""

    factors = st.session_state.get("szse_quant_factors")
    if not isinstance(factors, pd.DataFrame):
        st.info("请先在侧边栏点击“获取行情并计算因子”。")
        return

    summary = st.session_state.get("szse_quant_summary", {})
    errors = st.session_state.get("szse_quant_errors", pd.DataFrame())
    selected_as_of_date = st.session_state.get("szse_quant_as_of_date")
    if selected_as_of_date:
        st.caption(f"本轮筛选截至日期：{selected_as_of_date}。数据日期列显示各股票实际交易日。")
    total = int(summary.get("总数", 0))
    source_count = int(factors["数据来源"].str.contains("东方财富", na=False).sum()) if not factors.empty else 0
    fallback_count = int(factors["数据来源"].str.contains("腾讯", na=False).sum()) if not factors.empty else 0
    metric_columns = st.columns(5)
    metric_columns[0].metric("处理股票", total)
    metric_columns[1].metric("成功计算", int(summary.get("成功", 0)))
    metric_columns[2].metric("失败", int(summary.get("失败", 0)))
    metric_columns[3].metric("东方财富完整数据", source_count)
    metric_columns[4].metric("腾讯回退数据", fallback_count)

    incomplete_fallback_count = (
        int(factors["数据来源"].str.contains("缺失", na=False).sum())
        if not factors.empty
        else 0
    )
    if incomplete_fallback_count:
        st.warning(
            f"有 {incomplete_fallback_count} 条腾讯回退数据无法补齐最新成交额和换手率；"
            "涉及成交额或换手率的条件不会把这类记录判为满足。"
        )
    if isinstance(errors, pd.DataFrame) and not errors.empty:
        with st.expander(f"查看 {len(errors)} 条获取或计算失败记录", expanded=False):
            st.dataframe(errors, hide_index=True, width="stretch")
    with st.expander("查看已计算因子（前 50 条）", expanded=False):
        factor_columns = [
            "序号",
            "股票代码",
            "股票名称",
            "所属行业",
            "数据日期",
            "收盘价",
            "MA5",
            "MA20",
            "MA5上行",
            "前20日平台最高价",
            "平台突破幅度（%）",
            "收盘突破前20日平台",
            "RSI14",
            "MACD_DIF",
            "MACD_DEA",
            "KDJ_K(89,3,3)",
            "KDJ_D(89,3,3)",
            "KDJ_J(89,3,3)",
            MACD_GOLDEN_CROSS_DATE_COLUMN,
            MACD_GOLDEN_CROSS_AGE_COLUMN,
            KDJ_HEALTHY_GOLDEN_CROSS_DATE_COLUMN,
            KDJ_HEALTHY_GOLDEN_CROSS_AGE_COLUMN,
            KDJ_HEALTHY_GOLDEN_CROSS_NAME,
            "当日成交量",
            "当日成交额",
            "量比",
            "换手率",
            "估算流通市值（亿元）",
            "当日涨跌幅",
            "振幅",
            "BIAS20",
            "上影线比例",
            *CANDLESTICK_RISK_FACTOR_COLUMNS.values(),
            "收盘日内位置（%）",
            "收盘位于日内高位",
            "60日最高价",
            "触及60日高点压力",
            "数据来源",
        ]
        st.dataframe(
            factors.loc[:, [column for column in factor_columns if column in factors.columns]].head(50),
            hide_index=True,
            width="stretch",
        )


def _render_results(as_of_date: date | datetime | None = None) -> None:
    """展示本次严格风险筛选结果。"""

    results_as_of_date = st.session_state.get("szse_quant_results_as_of_date")
    if as_of_date is not None and (
        not _collection_matches_as_of_date(
            st.session_state.get("szse_quant_as_of_date"),
            as_of_date,
        )
        or not _collection_matches_as_of_date(
            results_as_of_date,
            as_of_date,
        )
    ):
        return

    results = st.session_state.get("szse_quant_results")
    if not isinstance(results, pd.DataFrame):
        return

    result_max_score = st.session_state.get("szse_quant_results_max_score")
    score_title_suffix = (
        f"（满分 {float(result_max_score):g} 分）"
        if isinstance(result_max_score, (int, float)) and float(result_max_score) > 0
        else ""
    )
    if results.empty:
        st.warning(
            f"当前满分 {float(result_max_score):g} 分；没有股票满足当前严格风险筛选条件。"
            if score_title_suffix
            else "没有股票满足当前严格风险筛选条件。"
        )
    else:
        st.subheader(f"得分最高的前 10 只股票{score_title_suffix}")
        risk_excluded_count = int(st.session_state.get("szse_quant_risk_excluded_count", 0))
        if risk_excluded_count:
            st.caption(f"风险过滤已剔除 {risk_excluded_count} 只股票。")
        st.dataframe(_style_result_table(results), hide_index=True, width="stretch", height=420)
        result_as_of_date = results_as_of_date or "当前"
        st.download_button(
            "下载当前筛选结果 CSV",
            data=results.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"深市主板量化筛选结果_{result_as_of_date}.csv",
            mime="text/csv",
        )
        review_candidates = st.session_state.get("szse_quant_ranked_top_50")
        if isinstance(review_candidates, pd.DataFrame) and not review_candidates.empty:
            st.subheader("风险过滤后评分排名第一（含所属行业）")
            st.dataframe(
                review_candidates.head(1),
                hide_index=True,
                width="stretch",
            )
            st.subheader(f"风险过滤后前 {len(review_candidates)} 名（含所属行业）")
            st.dataframe(
                review_candidates,
                hide_index=True,
                width="stretch",
                height=720,
            )
            st.subheader(f"当日前 {len(review_candidates)} 名行业入选数量")
            st.dataframe(
                summarize_prediction_review_industries(review_candidates),
                hide_index=True,
                width="stretch",
            )


def _ensure_current_data_pipeline() -> None:
    """清除与当前数据和结果结构不兼容的会话数据。"""

    if st.session_state.get("szse_quant_pipeline_version") == DATA_PIPELINE_VERSION:
        results = st.session_state.get("szse_quant_results")
        if isinstance(results, pd.DataFrame):
            review_candidates = st.session_state.get("szse_quant_ranked_top_50")
            if (
                "未满足条件（扣分项）" not in results
                or not isinstance(review_candidates, pd.DataFrame)
            ):
                _clear_screening_result_session_state()
        return
    _clear_collection_session_state()
    st.session_state["szse_quant_pipeline_version"] = DATA_PIPELINE_VERSION


def main() -> None:
    """Streamlit 入口。"""

    st.set_page_config(page_title="深市主板量化选股", layout="wide")
    _ensure_current_data_pipeline()
    st.title("深市主板量化选股")
    st.caption("基于所选截至日期技术因子的规则筛选工具，不构成投资建议。")

    try:
        modified_ns = DEFAULT_WORKBOOK_PATH.stat().st_mtime_ns
        companies = load_mainboard_companies_cached(str(DEFAULT_WORKBOOK_PATH), modified_ns)
    except (FileNotFoundError, ValueError, OSError) as exc:
        st.error(str(exc))
        st.stop()

    st.caption(
        f"股票池：{len(companies)} 家深市主板公司。主源为东方财富前复权日 K，"
        "失败时回退腾讯前复权日 K；缓存保存在本目录的 data_cache/szse_quant。"
    )
    settings = _render_sidebar(len(companies))
    target_day = _as_of_day(settings.get("as_of_date"))
    collected_day = st.session_state.get("szse_quant_as_of_date")
    if collected_day is not None and not _collection_matches_as_of_date(collected_day, target_day):
        _clear_collection_session_state()

    if bool(settings["fetch_clicked"]):
        _run_collection(companies, settings)

    _render_factor_status()

    if bool(settings["screen_clicked"]):
        factors = st.session_state.get("szse_quant_factors")
        max_score = float(settings["max_score"])
        collected_day = st.session_state.get("szse_quant_as_of_date")
        if not _collection_matches_as_of_date(collected_day, target_day):
            st.warning("筛选截至日期已变更，请先重新获取行情并计算因子。")
        elif not isinstance(factors, pd.DataFrame) or factors.empty:
            st.warning("尚无可用因子数据，请先点击“获取行情并计算因子”。")
        else:
            try:
                score_options = {
                    "turnover_range": tuple(settings["turnover_range"]),
                    "float_market_cap_range_yi": tuple(
                        settings["float_market_cap_range_yi"]
                    ),
                    "pct_change_range": tuple(settings["pct_change_range"]),
                    "amplitude_threshold": float(settings["amplitude_threshold"]),
                    "rsi_range": tuple(float(value) for value in settings["rsi_range"]),
                    "macd_golden_cross_lookback_days": int(
                        settings["macd_golden_cross_lookback_days"]
                    ),
                    "kdj_healthy_golden_cross_age_range": tuple(
                        int(value)
                        for value in settings["kdj_healthy_golden_cross_age_range"]
                    ),
                    "macd_dea_minus_dif_range": tuple(
                        float(value) for value in settings["macd_dea_minus_dif_range"]
                    ),
                    "volume_ratio_range": tuple(
                        float(value) for value in settings["volume_ratio_range"]
                    ),
                    "require_all": bool(settings["require_all"]),
                }
                ranked_candidates, eligible_count, risk_excluded_count = score_and_select(
                    factors,
                    settings["selected"],
                    selected_risks=settings["selected_risks"],
                    top_n=PREDICTION_REVIEW_TOP_N,
                    **score_options,
                )
                results = ranked_candidates.head(10).reset_index(drop=True)
                review_candidates = prepare_prediction_review_candidates(
                    ranked_candidates,
                    factors,
                )
                st.session_state["szse_quant_results"] = results
                st.session_state["szse_quant_eligible_count"] = eligible_count
                st.session_state["szse_quant_risk_excluded_count"] = risk_excluded_count
                st.session_state["szse_quant_results_as_of_date"] = target_day.isoformat()
                st.session_state["szse_quant_results_max_score"] = max_score
                st.session_state["szse_quant_ranked_top_50"] = review_candidates
                if eligible_count:
                    st.success(
                        f"当前满分 {max_score:g} 分；风险过滤剔除 {risk_excluded_count} 只股票；"
                        f"共有 {eligible_count} 只股票进入当前规则排序，已展示前 10 名和前 50 名。"
                    )
                else:
                    st.warning(
                        f"当前满分 {max_score:g} 分；风险过滤剔除 {risk_excluded_count} 只股票；"
                        "没有股票满足当前严格风险筛选条件。"
                    )
            except ValueError as exc:
                st.warning(str(exc))

    _render_results(target_day)


if __name__ == "__main__":
    main()
