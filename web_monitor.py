import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("zhituapi")
if not TOKEN:
    raise ValueError("请在 .env 中设置 zhituapi")
TOP = 20
CSV_PATH = BASE_DIR / "前 50 名（含所属行业）.csv"
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
df_top20 = df.head(TOP).copy()
stocks = [
    {
        "code": str(row["股票代码"]).strip().zfill(6),
        "name": str(row["股票名称"]).strip(),
    }
    for _, row in df_top20.iterrows()
]
logger.info("加载了 %s 只股票", len(stocks))

# The documented single-stock endpoint takes a six-digit code only.
QUOTE_URL_TEMPLATE = "https://api.zhituapi.com/hs/real/ssjy/{code}"
QUOTE_UPDATE_SECONDS = max(float(os.getenv("QUOTE_UPDATE_SECONDS", "10")), 5.0)
QUOTE_REQUEST_WORKERS = max(int(os.getenv("QUOTE_REQUEST_WORKERS", "10")), 1)
QUOTE_HISTORY_POINTS = max(int(os.getenv("QUOTE_HISTORY_POINTS", "0")), 0)

latest_quotes = {}
quote_history = {}
history_lock = threading.Lock()
last_update_time = None
update_error = None


class QuoteParseError(ValueError):
    """The quote API returned an error or invalid price data."""


def build_quote_url(code):
    return QUOTE_URL_TEMPLATE.format(code=str(code).strip().zfill(6))


def append_quote_history(history, quotes, timestamp, max_points=QUOTE_HISTORY_POINTS):
    for code, quote in quotes.items():
        points = history.setdefault(code, [])
        points.append({"timestamp": timestamp, "change_pct": quote["change_pct"]})
        if max_points and len(points) > max_points:
            del points[:-max_points]


def merge_quotes(previous_quotes, new_quotes, stock_list, timestamp):
    """Keep the last valid quote for a stock when the current request fails."""
    merged = {}
    for stock in stock_list:
        code = stock["code"]
        if code in new_quotes:
            quote = dict(new_quotes[code])
            quote["stale"] = False
            merged[code] = quote
        elif code in previous_quotes:
            quote = dict(previous_quotes[code])
            quote["stale"] = True
            quote["fallback_at"] = timestamp
            merged[code] = quote
    return merged


def filter_quotes_for_stocks(quotes, stock_list):
    active_codes = {stock["code"] for stock in stock_list}
    return {code: quote for code, quote in quotes.items() if code in active_codes}


def load_stocks_from_csv():
    candidate_df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    return [
        {
            "code": str(row["股票代码"]).strip().zfill(6),
            "name": str(row["股票名称"]).strip(),
        }
        for _, row in candidate_df.head(TOP).iterrows()
    ]


csv_mtime_ns = CSV_PATH.stat().st_mtime_ns


def reload_stocks_if_changed():
    global stocks, latest_quotes, csv_mtime_ns
    try:
        current_mtime_ns = CSV_PATH.stat().st_mtime_ns
    except OSError as exc:
        logger.error("无法检查候选 CSV: %s", exc)
        return False

    if current_mtime_ns == csv_mtime_ns:
        return False

    try:
        updated_stocks = load_stocks_from_csv()
    except (OSError, UnicodeError, pd.errors.ParserError, KeyError) as exc:
        logger.error("重新加载候选 CSV 失败，继续使用上一份列表: %s", exc)
        return False

    stocks = updated_stocks
    latest_quotes = filter_quotes_for_stocks(latest_quotes, stocks)
    with history_lock:
        retained_history = filter_quotes_for_stocks(quote_history, stocks)
        quote_history.clear()
        quote_history.update(retained_history)
    csv_mtime_ns = current_mtime_ns
    logger.info("候选 CSV 已重载，当前监控 %s 只股票", len(stocks))
    return True


def parse_quote_payload(data, stock):
    if not isinstance(data, dict):
        raise QuoteParseError("行情接口返回格式不正确")
    if data.get("error"):
        raise QuoteParseError(str(data["error"]))

    try:
        last = float(data["p"])
        previous_close = float(data["yc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QuoteParseError("行情数据缺少有效的最新价或昨收价") from exc

    if last <= 0 or previous_close <= 0:
        raise QuoteParseError("行情数据中的最新价或昨收价必须大于零")

    return {
        "code": stock.get("code", ""),
        "name": stock["name"],
        "last_price": last,
        "prev_close": previous_close,
        # ud is the absolute price change. Calculate the percentage explicitly.
        "change_pct": round((last - previous_close) / previous_close * 100, 2),
        "updated_at": data.get("t") or datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }


def fetch_quote(stock):
    response = requests.get(
        build_quote_url(stock["code"]),
        params={"token": TOKEN},
        timeout=5,
    )
    response.raise_for_status()
    return stock["code"], parse_quote_payload(response.json(), stock)


def fetch_quotes():
    global latest_quotes, last_update_time, update_error

    reload_stocks_if_changed()
    logger.info("开始获取行情数据")
    new_quotes = {}
    failures = []
    with ThreadPoolExecutor(max_workers=min(QUOTE_REQUEST_WORKERS, len(stocks))) as executor:
        futures = [(stock, executor.submit(fetch_quote, stock)) for stock in stocks]
        for stock, future in futures:
            try:
                code, quote = future.result()
                new_quotes[code] = quote
                logger.info(
                    "%s: 最新=%s, 昨收=%s, 涨跌=%+.2f%%",
                    stock["name"],
                    quote["last_price"],
                    quote["prev_close"],
                    quote["change_pct"],
                )
            except (requests.RequestException, QuoteParseError, ValueError) as exc:
                failures.append(f"{stock['name']}({stock['code']}): {exc}")
                logger.error("获取 %s 失败: %s", stock["name"], exc)

    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    merged_quotes = merge_quotes(latest_quotes, new_quotes, stocks, timestamp)
    if not merged_quotes:
        update_error = "本轮未获取到有效行情: " + "; ".join(failures)
        logger.error(update_error)
        return

    latest_quotes = merged_quotes
    last_update_time = timestamp
    with history_lock:
        append_quote_history(quote_history, merged_quotes, last_update_time)
    update_error = f"本轮有 {len(failures)} 只股票更新失败" if failures else None
    logger.info("行情更新完成，共获取 %s 只股票数据", len(new_quotes))


def background_updater(interval=QUOTE_UPDATE_SECONDS):
    while True:
        started_at = time.monotonic()
        fetch_quotes()
        time.sleep(max(0, interval - (time.monotonic() - started_at)))


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", stocks=stocks)


@app.route("/api/quotes")
def api_quotes():
    quotes_list = list(latest_quotes.values())
    sorted_quotes = sorted(quotes_list, key=lambda quote: quote.get("change_pct", -999), reverse=True)
    with history_lock:
        history_snapshot = {code: list(points) for code, points in quote_history.items()}
    response = jsonify(
        {
            "quotes": sorted_quotes,
            "history": history_snapshot,
            "last_update": last_update_time,
            "error": update_error,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def start_background():
    thread = threading.Thread(target=background_updater, daemon=True)
    thread.start()
    logger.info("后台行情更新线程已启动，刷新间隔 %.0f 秒", QUOTE_UPDATE_SECONDS)


if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=5000, debug=False)
