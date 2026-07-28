# 固定策略下一交易日回测

该目录是独立模块，不修改项目根目录的 `szse_quant_app.py`。其中的
`szse_quant_app.py` 是策略逻辑快照，回测复用其 MA、MACD、KDJ(89,3,3)、风险
K 线及 `score_and_select` 的定义。

固定规则如下：

- 站上 5 日线
- MA5 高于 MA20
- MACD 多头
- KDJ 健康金叉距今 1 至 3 个交易日
- 日成交额大于等于 1 亿元
- 流通市值 50 亿至 200 亿元
- 仅保留满足全部条件的股票
- 全部风险过滤规则启用
- 每天仅买入排序第一只；同分按股票池原始序号升序

运行：

```powershell
uv run --locked python strategy_backtest/run_backtest.py
```

第一次运行会为每只股票下载一次长历史前复权日 K（含成交额、换手率）并保存在
`strategy_backtest/data_cache`。这避免了按 55 个交易日重复请求同一只股票。默认
0.25 秒全局请求间隔下，首次全量运行通常约 6 至 12 分钟；之后命中缓存时只进行本地
因子计算和筛选。

输出 Excel 报表含三张表：

- `汇总`：首行给出 `预测正确天数/预测天数，总收益率`，并单独给出未预测天数。
- `每日回测`：包含所有 55 个选股日；无信号日同样保留，收益按 0% 计。
- `数据问题`：历史获取、因子计算或严格次日收益无法匹配的记录。

严格次日收益只认 `每日涨跌幅明细` 中“下一市场交易日”且“前一交易日恰为选股日”
的记录。停牌后跨日复牌的累计涨跌幅不会误当作第二天收益。无信号和缺失严格收益都
计入未预测天数，并按 0% 收益处理。

无法获取完整历史行情、或截至回测末日不足 120 根日线的股票会自动剔除，不影响其余
股票的回测。已下载成功的历史会保留在缓存中，重试时只会补齐失败项。

快速小范围检查可使用：

```powershell
uv run --locked python strategy_backtest/run_backtest.py --max-companies 20
```

## 交互回测页

浏览器交互回测页复用根目录 `szse_quant_app.py` 的指标、默认值、风险过滤和
“低位企稳后的平台突破”预设；KDJ 金叉出现时间可设为距今 0 至 10 个交易日的区间，
包含两端，默认 1 至 3（0 表示当前选股日）。可选择收益文件中的实际选股日期区间，按当前勾选条件每日取排名第一只股票，
展示正确率、未预测天数、累计收益和实际预测日明细（含预测日期）。

```powershell
uv run --locked streamlit run strategy_backtest/backtest_app.py --server.port 8501
```

首次使用可先预热完整因子缓存；长历史已存在时不会重新联网。预热后调整日期范围、
指标或风险过滤只会读取本地因子并重新筛选。

```powershell
uv run --locked python strategy_backtest/warm_factor_cache.py
```

## 每日滚动参数优化

`rolling_parameter_optimizer.py` 会固定当前默认的勾选项与风险过滤，仅对已启用的 RSI、换手率、量比、涨幅、KDJ 金叉出现时间和 MACD 红蓝线区间做坐标搜索。它只使用已有严格次日收益的实际交易日，因此不会把尚未验证的当日信号带入优化。

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer --lookback-days 20
```

结果会保存到 `strategy_backtest/outputs/rolling_parameter_updates`：同一运行日期生成两份 JSON 文件。`rolling_parameter_optimization_YYYY-MM-DD.json` 是供下一交易日继承的参数文件；`rolling_parameter_backtest_YYYY-MM-DD.json` 是完整回测报告，按“优化汇总”“参数对比”“每日回测”“优化路径”“数据问题”保存原报表内容，每个工作表均包含 `columns` 和 `rows`。未传入初始参数时，脚本会自动读取当前运行日期之前最新的参数 JSON；首次运行则使用程序默认参数。要手动指定起点，可传入上一份参数 JSON：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer --lookback-days 20 --starter-json strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_2026-07-27.json
```

可用 `--as-of-date YYYY-MM-DD` 指定运行日期，`--minimum-prediction-days N` 调整候选参数至少需要的有效预测天数。默认值为回看交易日的 20%，且不少于 3 天，避免单次预测造成参数跳变。
