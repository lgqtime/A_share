# SimpleIndicatorScreening_ASharess

独立的深市主板技术指标选股工具。它不依赖 `tradingagents-astock` 的其他文件。

## 文件说明

- `fetch_szse_data.py`：从深交所公开 JSON 接口生成 `深交所数据.xlsx`。
- `szse_quant_app.py`：Streamlit 选股界面，读取同目录的 Excel。
- `fetch_period_returns.py`：读取“主板公司”股票池并导出指定区间内每个交易日的前复权涨跌幅。
- `stock_analysis.py`：独立单股分析的数据获取与 MA、MACD、KDJ 指标计算核心。
- `stock_analysis_charts.py`：独立单股分析的 Altair 三面板图表构建函数。
- `stock_analysis_app.py`：独立单股分析 Streamlit 页面。
- `深交所数据.xlsx`：已随目录提供的股票池；需要更新时可重新生成。
- `.venv`：本机已创建的虚拟环境。

## 运行

在本目录打开 PowerShell 后执行：

```powershell
uv sync --locked
uv run --locked python fetch_szse_data.py
uv run --locked streamlit run szse_quant_app.py --server.port 8504
```

浏览器访问 `http://localhost:8504`。首次使用时先点击“获取行情并计算因子”，再点击“开始筛选”。
可在侧边栏选择“筛选截至日期”；选择非交易日时，应用会使用此前最近一个有效交易日的数据。

## 独立单股技术分析

该页面与选股程序相互独立，输入沪深 A 股代码后会从公开行情接口获取前复权日线，
计算 MA5、MA20、MACD（12、26、9）与 KDJ（默认 89、3、3），并在浏览器中展示 K 线、
MACD 和 KDJ 三个共享日期轴的图表。页面可调整 KDJ 的 N、M1、M2 参数；系统会按所选
参数额外获取历史日线进行预热，避免从展示区间重新开始计算。

```powershell
uv sync --locked
uv run --locked streamlit run stock_analysis_app.py --server.port 8507
```

浏览器访问 `http://localhost:8507`。支持 `000001`、`600000`、`000001.SZ`、`SH600000`
等代码格式。

## 预热行情缓存

首次全量扫描前可预先下载选股应用所需的最近 120 个交易日数据。KDJ 固定使用
`(89,3,3)` 参数，120 根实际交易日同时为其 RSV 与平滑计算保留了完整预热窗口：

```powershell
uv run --locked python warm_szse_quant_cache.py --as-of-date 2026-07-25
```

数据会写入 `data_cache/szse_quant/<截至日期>`，同一截至日期且未超过“行情缓存有效小时数”时，应用会直接复用。新上市或停牌后不足 120 个实际交易日的股票无法计算完整技术指标，不会写入可复用缓存。KDJ 金叉出现时间可设为距今 `0-10` 个交易日的区间，包含两端，默认 `1-3`；`0` 表示当前选股日。金叉需发生在 K、D、J 均低于 20 的超卖区、金叉日 J 线上行、无顶背离，且其后未出现死叉；该条件计 1.5 分。风险过滤新增“K线形状”列表，默认选中十字星、倒T字星、吊颈线、长上影线阳线和极端大阳线；任一形态在最近 3 个实际交易日内出现即剔除该股票。

## 每日涨跌幅

```powershell
uv run --locked python fetch_period_returns.py --start-date 2026-07-01 --end-date 2026-07-23
```

脚本默认生成 `深市主板每日涨跌幅_<开始日期>_<结束日期>.xlsx`，其中包含“每日涨跌幅”“每日涨跌幅明细”“区间汇总”和“失败明细”工作表。“每日涨跌幅”是主表：每行一只股票，每个实际交易日占一列，单元格记录该日按前复权收盘价相对前一实际交易日计算的涨跌幅；主表按“总涨跌幅（%）”降序排列，且该值由所有已展示的每日涨跌幅复利计算。“每日涨跌幅明细”保留逐日长表，区间汇总保留首尾收盘价计算的区间涨跌幅。为了让区间内第一个交易日也能得到涨跌幅，脚本会额外获取开始日前的日线作为基准。相同区间的完整日线会缓存到 `data_cache/period_returns`，下次运行可直接复用；传入 `--cache-hours 0` 可关闭缓存。

转移到另一台电脑后，建议执行一次 `uv sync --locked` 重建 `.venv`。行情使用公开数据接口，不需要 AkShare 或模型密钥。
