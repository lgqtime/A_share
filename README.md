# 深市主板指标筛选与交易信号

这是一个独立的深市主板技术指标筛选、单股技术分析、严格次日收益回测、每日交易信号和 A 股联网证据分析工具。选股页面、回测和 AI 分析均在本项目 `.venv` 中运行；01:00/09:25 跨项目计划任务还需要一个可运行 `scheduled_ashare_workflow.py` 的 Python 环境，用于读取 A 股交易日历。

项目只读取行情、生成候选和发送 PushPlus 提醒，绝不连接券商或提交委托；买卖由使用者自行决定。

## 功能与入口

| 功能 | 入口 | 默认地址/用途 |
| --- | --- | --- |
| 日常选股与指标筛选 | `szse_quant_app.py` | `http://localhost:8504` |
| 单股技术分析 | `stock_analysis_app.py` | `http://localhost:8507` |
| 交互式回测 | `strategy_backtest/backtest_app.py` | `http://localhost:8501` |
| 固定规则命令行回测 | `strategy_backtest/run_backtest.py` | 导出 Excel 回测报表 |
| 单窗口滚动参数优化 | `strategy_backtest/rolling_parameter_optimizer.py` | 手动指定一个回看窗口并写入参数与回测 JSON |
| 动态参数融合 | `strategy_backtest/rolling_parameter_ensemble.py` | 融合 14、30、45 日窗口；每日任务的参数更新入口 |
| 每日后台交易信号 | `daily_trading_runner.py` | 收盘预测与次日盘中监控 |
| 独立 A 股联网证据分析 | `ai_agent.py` | 09:05 读取候选 CSV，生成当天 AI 前十和完整证据产物 |

主要文件和目录：

```text
IndicatorScreening_Asheres/
|- pyproject.toml                          # 项目依赖声明
|- uv.lock                                 # 锁定后的可复现依赖版本
|- 深交所数据.xlsx                         # 深市主板股票池
|- fetch_szse_data.py                      # 更新股票池
|- fetch_period_returns.py                 # 获取逐日涨跌幅和严格次日收益输入
|- warm_szse_quant_cache.py                # 预测模块行情预热
|- szse_quant_app.py                       # 日常选股页面
|- stock_analysis.py                       # 单股分析计算核心
|- stock_analysis_charts.py                # 单股分析图表核心
|- stock_analysis_app.py                   # 单股分析页面
|- daily_trading_runner.py                 # 原有选股与盘中监控后台入口
|- scheduled_ashare_workflow.py            # 跨项目凌晨处理和早间筛选推送入口
|- intraday_trigger_monitor.py             # 次日盘中监控
|- ai_agent.py                              # 独立联网证据分析入口
|- ai_agent_io.py                           # 候选读取、运行目录和清单
|- concept_discovery.py                     # DeepSeek 概念识别
|- tavily_hub.py                            # Tavily Hub 轮换 key 客户端
|- tavily_evidence.py                       # 五维证据检索与失败重试
|- evidence_evaluator.py                    # DeepSeek 证据评分与 JSON 校验
|- ranking.py                               # 程序侧门槛、风险封顶和前十排序
|- run_daily_runner.cmd                    # 后台任务命令入口
|- install_daily_runner_tasks.ps1          # Windows 计划任务安装脚本
|- strategy_backtest/
|  |- backtest_app.py                      # 交互式回测页面
|  |- backtest_core.py                     # 回测计算核心
|  |- factor_batch.py                      # 批量因子计算
|  |- run_backtest.py                      # 固定规则命令行回测
|  |- warm_factor_cache.py                 # 回测因子预热
|  |- rolling_parameter_optimizer.py       # 单窗口滚动参数优化
|  |- rolling_parameter_ensemble.py        # 14/30/45 日动态参数融合
|  |- runtime_strategy.py                  # 与预测模块共用的策略定义
|  |- szse_quant_app.py                    # 旧版界面兼容快照，不是每日优化策略来源
|  `- outputs/input_data/                  # 严格次日收益输入文件
`- tests/                                  # 自动化测试
```

`data_cache/`、`strategy_backtest/data_cache/`、`daily_trading_outputs/` 和 `strategy_backtest/outputs/rolling_parameter_updates/` 是本机运行数据。删除缓存不会删除代码或股票池，但下次运行会重新联网下载；每日参数 JSON、融合快照和收益历史建议长期保留。

如需强制重新拉取行情，可在页面启用“强制刷新”，或对支持该选项的命令行使用 `--force-refresh`。

## 端到端业务流程

```text
深交所股票池/历史数据
        |
        v
01:00  数据准备：处理前一实际交易日，生成归档、前50和风险过滤前10
        |
        +--> 09:05  独立 AI：读取根目录“前 50 名（含所属行业）.csv”的全部有效行
        |             概念识别 -> 五维 Tavily Hub 证据 -> DeepSeek 评分 -> 程序侧排序
        |             输出当天独立运行目录和 top10_recommendations.csv
        |
        +--> 09:25  合并推送：风险过滤前10 + 当天 AI 前10 + 两者交集 -> PushPlus
        |
        `--> 09:28  既有盘中监控：读取日期匹配的归档候选，持续到 10:05
```

01:00 和 09:25 只在 A 股交易日继续执行；周末和法定节假日由交易日历判定后跳过。09:05 的 AI 分析不读取历史 AI 结果，也不依赖 01:00 的 AI 分析；它只读取当天根目录候选 CSV。当前安装脚本将 09:05 安排在每个工作日，但 `ai_agent.py` 本身不查询交易日历，因此工作日节假日也会运行；如不需要节假日分析，应在任务计划程序中禁用当日任务。09:25 使用前一实际交易日的风险过滤 CSV，同时只接受当天 `run_manifest.json` 中声明的最新有效 AI 批次。所有消息仅供研究和提醒，不会连接券商或提交交易委托。

### 关键数据文件与保存策略

以下路径均相对项目根目录。迁移或备份项目时，应连同这些文件和目录一起保留；不要只复制代码或单个可执行文件。

| 路径 | 作用 | 写入与读取方 | 保存建议 |
| --- | --- | --- | --- |
| `深交所数据.xlsx` | 深市主板股票池，包含股票代码、简称和深交所官方行业字段。 | 每日收盘任务先刷新；选股、回测和优化读取。 | **应保留**。如需刷新，使用每日任务或官方股票池刷新流程，不建议手工改列。 |
| `strategy_backtest/outputs/input_data/深市主板每日涨跌幅_滚动更新.xlsx` | 唯一的滚动严格次日收益数据库；其中“每日涨跌幅明细”和“失败明细”决定哪些历史日期和股票可被验证。 | 每日收盘任务增量写入；Streamlit 回测、单窗口优化和 14/30/45 日融合读取。 | **应备份并长期保留**。删除后须从历史收益文件重新初始化或重新下载，融合器无法凭空补回过去收益。 |
| `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_current.json` | 当前生效的融合参数，是每日预测使用的唯一当前参数入口。交互式回测页面不会自动加载它。 | 完整融合成功后原子写入；每日预测读取。 | **应保留，勿手工编辑**。不要在正常融合日用单窗口优化器覆盖它。 |
| `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_YYYY-MM-DD.json` | 指定验证日的已发布参数版本，供归档、恢复和追溯。 | 融合器成功发布时写入；收盘任务归档并在恢复模式读取。 | **应长期保留**，可据此核对某日实际使用的参数。 |
| `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_ensemble/return_history.json` | 每日 14、30、45 日窗口的原始总收益率历史；五日评分和动态权重的直接输入。 | 融合器导入或更新；下一次融合读取。 | **必须长期保留**。删除后须重新导入矩阵或按历史日期补跑；恢复完整前，当日融合不能发布。 |
| `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_ensemble/window_snapshots/` 与 `window_reports/` | 按日期、窗口和输入指纹保存的不可变优化快照及组件报告，用于审计和复用，避免重复优化。 | 融合器写入并复用。 | **建议长期保留**。行情、股票池或策略代码变化时会创建新版本，而不是覆盖旧版本。 |
| `daily_trading_outputs/archive/YYYY-MM-DD/` | 每日不可变归档：实际股票池、参数、前 50 候选、因子、收益、交易信号、运行状态及盘中结果。 | 每日收盘和盘中任务写入；恢复预测和盘中监控按日期读取。 | **应长期保留**，是问题排查、历史复盘和中断恢复的依据。 |
| `daily_trading_outputs/scheduled_analysis/YYYY-MM-DD/data_preparation.json` | 01:00 数据准备状态，记录前一实际交易日、风险过滤候选 CSV、候选数和运行日志。 | `scheduled_ashare_workflow.py` 写入；09:25 推送读取。 | **应保留到消息确认发送后**，建议与归档一起长期保留。 |
| `daily_trading_outputs/scheduled_analysis/YYYY-MM-DD/pushplus_combined_message.html` | 09:25 实际生成的 HTML 消息，包含风险过滤候选、AI 前十和交集。 | 09:25 合并推送写入；PushPlus 发送同一内容。 | **建议保留**，用于核对发送内容。 |
| `daily_trading_outputs/scheduled_analysis/YYYY-MM-DD/pushplus_combined_send_state.json` | 09:25 发送状态、时间、标题和消息文件路径。 | 发送成功后写入；重复运行默认读取。 | **应保留**；使用 `--force` 才会重复发送。 |
| `ai_agent_outputs/YYYYMMDD/HHMMSS/` | 09:05 AI 独立运行目录；包含 `run_manifest.json`、`all_rankings.csv`、`top10_recommendations.csv`、两阶段 JSONL/CSV 和 raw 证据。 | `ai_agent.py` 每次运行新建；09:25 读取当天最新有效目录。 | **应保留**，不要把不同时间目录合并覆盖。 |
| `前 50 名（含所属行业）.csv`、`每日交易信号.csv`、`daily_trading_outputs/每日预测第一名.csv`、`daily_trading_outputs/每日实时检测结果.csv` | 当前批次的便捷入口；完整历史以对应日期归档为准。 | 每日任务覆盖写入；盘中监控优先读取日期匹配的归档候选。 | 可重新生成，但不要把它们当作历史记录或手工替换归档文件。 |
| `data_cache/szse_quant/YYYY-MM-DD/` | 当日逐股票日线与因子缓存；恢复预测时要求与归档日期严格匹配。 | 收盘预测写入；恢复模式读取。 | 可清理，但会增加联网请求；清理后不能仅凭本地缓存恢复对应日期的预测。 |
| `strategy_backtest/data_cache/strategy_factors/` 与 `data_cache/period_returns/` | 分别缓存回测/优化因子和历史收益抓取结果，减少重复计算与网络请求。 | 回测、优化和收益抓取工具读写。 | 可清理，功能不变，但下一次回测、优化或抓取会明显变慢并重新联网。 |

参数、收益工作簿和每日归档均使用临时文件完成写入后再原子替换。任务中断时不要删除这些正式文件；先查看当天 `daily_trading_outputs/archive/YYYY-MM-DD/运行状态.json` 和日志，再按“每日后台交易信号”中的恢复或补跑命令处理。

## 安装

前置条件：Windows PowerShell、Python 3.10 或更高版本，以及 `uv` 命令行工具。

在项目根目录执行：

```powershell
uv sync --locked
uv run --locked python --version
uv run --locked python -c "import pandas, streamlit; print(streamlit.__version__)"
```

`uv sync --locked` 会按 `uv.lock` 创建 `.venv` 并安装固定版本的依赖。不要复制其他机器上的 `.venv`；虚拟环境与本机 Python 路径相关。迁移项目后请重新执行该命令。

根目录 `.env` 只保存本机密钥和额度配置，禁止提交 Git 或复制到日志：

```dotenv
DEEPSEEK_API_KEY=sk-...
TAVILY_HUB_API_KEY1=thb-...
TAVILY_HUB_API_KEY2=thb-...
TAVILY_HUB_API_KEY3=thb-...
TAVILY_HUB_API_KEY4=thb-...
TAVILY_HUB_API_KEY5=thb-...
TAVILY_HUB_API_KEY6=thb-...
zhituapi=...
PushPlusapi=...
```

AI 分析使用 `TAVILY_HUB_API_KEY1` 至 `TAVILY_HUB_API_KEY6`，通过 `https://tavily.sharyuke.com/api/proxy/search` 轮换请求；旧的 `TAVILY_API_KEY` 不参与当前 AI 流程。PushPlus 兼容旧字段 `PushPlus_token`，但优先使用 `PushPlusapi`。`.env` 缺少 `DEEPSEEK_API_KEY` 或全部 Tavily Hub key 时，09:05 任务会失败并由计划任务重试。

## 日常选股

首次使用或需要更新股票池时：

```powershell
uv run --locked python fetch_szse_data.py
```

启动选股页面：

```powershell
uv run --locked streamlit run szse_quant_app.py --server.port 8504
```

在浏览器中打开 `http://localhost:8504`，选择“筛选截至日期”后，依次点击“获取行情并计算因子”和“开始筛选”。如果选择非交易日，应用使用此前最近一个有效交易日的数据。

首次全量扫描前也可预热最近 120 个交易日的数据：

```powershell
uv run --locked python warm_szse_quant_cache.py --as-of-date YYYY-MM-DD
```

数据写入 `data_cache/szse_quant/<截至日期>`。同一截至日期且未超过“行情缓存有效小时数”时会直接复用；新上市或停牌后不足 120 个实际交易日的股票无法计算完整技术指标，因此不会写入可复用缓存。

筛选使用 MA、MACD、KDJ 和可配置的成交、换手、量比、涨幅等条件。KDJ 默认参数为 `(89,3,3)`，金叉出现时间可设为距今 `0-10` 个交易日的闭区间，默认 `1-3`，其中 `0` 表示当前选股日。健康金叉要求发生时 K、D、J 都低于 20、金叉日 J 线上行、无顶背离且其后未出现死叉；该条件计 1.5 分。风险过滤中的“K线形状”默认选中十字星、倒 T 字星、吊颈线、长上影线阳线和极端大阳线，任一形态在最近 3 个实际交易日出现即剔除该股票。

## 单股技术分析

```powershell
uv run --locked streamlit run stock_analysis_app.py --server.port 8507
```

打开 `http://localhost:8507` 后，可输入 `000001`、`000001.SZ`、`600000` 或 `SH600000` 等代码格式。页面从公开接口获取前复权日线，展示 K 线、MA5、MA20、MACD（12、26、9）和 KDJ 图表。KDJ 的 N、M1、M2 参数可调；系统会为所选参数额外获取历史日线进行预热，避免从展示区间重新开始计算。

## 严格次日收益数据

交互回测和滚动优化以逐日涨跌幅工作簿中的严格相邻交易日收益作为真实结果。手动更新该输入时执行：

```powershell
uv run --locked python fetch_period_returns.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```

脚本默认生成 `深市主板每日涨跌幅_<开始日期>_<结束日期>.xlsx`，包含“每日涨跌幅”“每日涨跌幅明细”“区间汇总”和“失败明细”工作表：

- “每日涨跌幅”按股票逐行、实际交易日逐列展示前复权涨跌幅，按由已展示日收益复利计算的“总涨跌幅（%）”降序排列。
- “每日涨跌幅明细”保留逐日长表；“区间汇总”保留首尾收盘价计算的区间涨跌幅。
- 为让区间首个交易日也有涨跌幅，脚本会额外获取开始日前的日线作为基准。
- 完整日线缓存到 `data_cache/period_returns`；传入 `--cache-hours 0` 可关闭缓存。

将生成的工作簿放入 `strategy_backtest/outputs/input_data/`，或在回测、优化命令中通过 `--returns-workbook` 显式指定。

## 回测

回测核心与每日滚动优化通过 `strategy_backtest/runtime_strategy.py` 复用根目录 `szse_quant_app.py` 的 MA、MACD、KDJ（89、3、3）、风险 K 线和 `score_and_select` 定义，因此优化后的参数与每日预测使用同一套策略逻辑。

### 日期范围与缓存补齐

交互回测以页面中选择的起止日期为准，只使用同时具有选股日和下一市场交易日的日期。运行回测时，程序会检查该选定范围所需的长历史和因子缓存：

- 已覆盖所选范围的有效长历史缓存会直接复用。
- 缓存缺少所选范围前段或后段时，只联网下载缺失区间，和已有数据合并、去重后写入当前范围的长历史缓存。
- 所选日期缺少的全量因子会计算并写入因子缓存；已有且与历史令牌匹配的因子会复用。
- 严格次日收益工作簿仍是本地输入，页面不会为它联网下载；请按上一节先准备覆盖所选范围的收益文件。

因此不需要因切换回测日期范围手工清除或预热缓存。首次运行或需要主动预热整个收益工作簿覆盖范围时，仍可执行：

```powershell
uv run --locked python strategy_backtest/warm_factor_cache.py
```

### 交互式回测

```powershell
uv run --locked streamlit run strategy_backtest/backtest_app.py --server.port 8501
```

页面的指标、默认参数、风险过滤和“低位企稳后的平台突破”预设与预测模块一致。KDJ 金叉出现时间可设为距今 0 至 10 个交易日，包含两端，默认 1 至 3。选择收益文件中的实际选股日期范围后，页面按当前勾选条件每天取排名第一只股票，并展示正确率、未预测天数、累计收益和实际预测日明细（含预测日期）。

回测只认可“每日涨跌幅明细”中下一市场交易日、且前一交易日恰好为选股日的记录。停牌后跨日复牌的累计涨跌幅不会被误认为次日收益；无信号或缺少严格收益的日期都计为未预测天数，收益按 0% 处理。

无法获取完整历史行情、截至回测末日不足 120 根日线、因子计算失败或严格次日收益无法匹配的股票，会写入数据问题并从当天相关计算中排除，不会中断其他股票的回测。

### 固定规则命令行回测

固定规则为：站上 5 日线、MA5 高于 MA20、MACD 多头、KDJ 健康金叉距今 1 至 3 个交易日、日成交额不低于 1 亿元、流通市值为 50 亿至 200 亿元、全部风险过滤启用，并仅保留满足全部条件的股票。每天只买入排序第一只；同分时按股票池原始序号升序。

```powershell
uv run --locked python strategy_backtest/run_backtest.py
```

首次全量运行会下载所需前复权日 K（含成交额、换手率）到 `strategy_backtest/data_cache`，之后优先使用缓存。默认 0.25 秒全局请求间隔下，首次全量运行通常约需 6 至 12 分钟；缓存命中时只进行本地因子计算和筛选。快速小范围检查：

```powershell
uv run --locked python strategy_backtest/run_backtest.py --max-companies 20
```

Excel 报表包含：

- `汇总`：首行给出 `预测正确天数/预测天数，总收益率`，并单独列出未预测天数。
- `每日回测`：保留所有选股日，无信号日也保留并按 0% 收益计。
- `数据问题`：记录历史获取、因子计算和严格次日收益匹配问题。

### 单窗口滚动参数优化

`strategy_backtest.rolling_parameter_optimizer` 保留为单窗口工具，适合实验、参数扫描或需要固定回看长度的回测。它固定当前默认的勾选项与风险过滤，仅对已启用的 RSI、换手率、量比、涨幅、KDJ 金叉出现时间和 MACD 红蓝线区间进行坐标搜索。它只使用已有严格次日收益的实际交易日，避免将尚未验证的当日信号带入优化。

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer --lookback-days 7
```

默认回看窗口为 14 个实际可验证选股日。可以用 `--as-of-date YYYY-MM-DD` 重跑历史日期，用 `--minimum-prediction-days N` 设置候选参数所需的最少有效预测天数。默认值为回看交易日的 20%，且不少于 3 天，避免因单次预测造成参数跳变。

未传入初始参数时，脚本自动读取当前运行日期之前最新的日期参数 JSON；首次运行则使用程序默认参数。也可手动指定上一份参数文件：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer `
  --lookback-days 20 `
  --starter-json strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_YYYY-MM-DD.json
```

单窗口工具会直接写入 `rolling_parameter_optimization_YYYY-MM-DD.json` 和 `rolling_parameter_optimization_current.json`。因此，日常已经启用动态融合时，不要在同一交易日再用该命令发布参数；否则会暂时以单窗口结果覆盖融合参数，直到下一次融合任务成功发布。

### 动态参数融合（每日任务使用）

`strategy_backtest.rolling_parameter_ensemble` 是每日收盘任务的参数更新入口。它以确认收盘收益后的最新实际验证交易日 `T` 为截止日，分别取得 14、30、45 日窗口的优化结果，再将融合参数用于 `T+1` 的开盘后选股和盘中监控。它不会改变筛选项、风险过滤或每日预测的参数文件路径；每日预测仍读取原有的 `rolling_parameter_optimization_current.json`。交互式回测页面不会自动读取该 JSON，而是使用页面控件中选定的参数，因此两者的结果只能在参数、日期范围和收益文件一致时比较。

#### 权重计算

对每个窗口，融合器取最近连续 5 个不晚于 `T` 的实际验证交易日的**原始总收益率**，记为 `R14`、`R30`、`R45`。每组评分严格按以下规则计算：

1. `S = mean(R) / std(R)`，标准差使用样本标准差 `ddof=1`；标准差为 0 时评分为 0。
2. 对评分应用 ReLU：负评分强制为 0。
3. 对截断后的评分施加固定覆盖调整：`C14 = ReLU(S14) × 0.65`、`C30 = ReLU(S30) × 0.85`、`C45 = ReLU(S45) × 1.20`。较长的 45 日窗口因此是策略的稳定底座，14 日窗口仍能快速响应但不会过度主导权重。
4. 对每个窗口的最优参数进行范围检查。涨跌幅必须在 `[-5.5, 10.5]`、RSI 在 `[20, 120]`、换手率在 `[2.5, 10.5]`、量比在 `[0.8, 7]`；任一项的任一端点超出对应闭区间时，该窗口的惩罚系数 `P_i = 0.1`，否则 `P_i = 1.0`。`D_i = C_i × P_i`。
5. 不使用 Softmax。`Total = D14 + D30 + D45` 大于 0 时，`W_i = D_i / Total`。惩罚在归一化前执行，因此最终权重始终合计 100%。
6. 三个惩罚后得分都为 0 时，三个权重均为 `1/3`，保持策略参与而不因临时弱势全部空仓。

所有可调区间按三个权重逐端点线性加权。KDJ 金叉出现时间是整数区间：左端点向下取整，右端点向上取整；融合后再通过原有参数范围校验。融合后收益不会由三个组件收益线性推导，因此审计报告不会伪造“融合总收益率”。

每份融合审计报告在 `ensemble.scores` 中同时保存原始夏普评分、ReLU 评分、覆盖调整系数、越界字段、惩罚系数、惩罚后得分和最终权重。

#### 自动运行与手动运行

`daily_trading_runner.py --mode after-close` 会在确认 `T` 的收盘收益写入滚动工作簿后自动调用融合器。定时任务的触发时间、归档流程和页面参数路径不变。

手动以最新可验证交易日运行：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_ensemble
```

手动指定截止日和数据源：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_ensemble `
  --returns-workbook strategy_backtest/outputs/input_data/深市主板每日涨跌幅_滚动更新.xlsx `
  --stock-pool 深交所数据.xlsx `
  --output-dir strategy_backtest/outputs/rolling_parameter_updates `
  --as-of-date YYYY-MM-DD
```

融合器固定使用 14、30、45 日三个窗口，不接受 `--lookback-days`。可继续传递 `--cache-hours`、`--workers`、`--factor-workers`、`--minimum-prediction-days`、`--max-passes` 等优化器选项。`--force-refresh` 只会刷新缺失快照所需的行情；已保存的同一输入版本快照不会被覆盖。

首次接入或迁移旧数据时，可只导入此前参数扫描生成的收益率矩阵，不运行耗时优化：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_ensemble `
  --output-dir strategy_backtest/outputs/rolling_parameter_updates `
  --import-return-matrix optimizer_lookback_sweep/20260810_175119/收益率矩阵.csv
```

导入只写入收益历史，不会更改当前参数文件。矩阵列必须是 `YYYY-MM-DD` 验证日期，行中只读取 14、30、45 日窗口；UTF-8 BOM 编码也可直接读取。

#### 缓存、审计与安全发布

融合运行数据保存在 `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_ensemble/`：

```text
rolling_parameter_ensemble/
|- return_history.json                         # 每日 14/30/45 原始收益率长期缓存
|- window_snapshots/YYYY-MM-DD/                # 日期、窗口、输入指纹对应的不可变快照
|- window_reports/YYYY-MM-DD/                  # 不可变的组件优化报告副本
|- window_runs/lookback_14|30|45/              # 单窗口优化的可复用工作目录
`- rolling_parameter_ensemble_YYYY-MM-DD.json # 五日收益、评分、权重和融合参数审计报告
```

快照指纹包含收益工作簿、股票池、策略定义和优化器代码的内容哈希。相同日期、窗口和输入直接复用，避免重复计算；行情、股票池或策略修订后会新建并列版本，不会改写旧快照或其组件报告。

当窗口集合发生变化，或最近五个验证交易日缺少某个窗口的收益历史时，融合器会在发布前自动回填缺失的 `14/30/45` 日快照和收益记录。这次初始化可能明显慢于普通日任务；回填完成后，后续任务只优化新的验证日，并复用已有快照和历史数据。

只有当三组当前窗口快照、最近五个实际验证日的收益和最终参数校验全部通过后，融合器才会原子写入以下兼容文件：

- `rolling_parameter_optimization_YYYY-MM-DD.json`：当天融合参数，供归档和恢复使用。
- `rolling_parameter_optimization_current.json`：当前融合参数，供每日预测读取；交互式回测页面不自动读取此文件。

任一窗口失败、截至日期前不足五个已完成验证交易日、五日收益缺失或参数校验失败时，两个兼容文件均不会被覆盖。`--as-of-date` 是截至日期：融合器会使用该日期及以前最近的实际验证交易日作为 `T`，所以可安全用于周末运行和历史补跑。融合器返回失败后，收盘定时任务按既有机制重试；已保存的窗口快照和收益记录会在重试时复用。为防止补跑旧数据，若当前参数文件记录的最后验证交易日比 `T` 更新，融合器会拒绝覆盖；单窗口文件即使运行日期较晚、但最后验证交易日与 `T` 相同，仍可被融合参数替换。

JSON 使用 UTF-8，日期采用 `YYYY-MM-DD`，缺失值写为 `null`；单窗口优化和融合审计报告均不再生成 Excel 文件。

## 每日后台交易信号

`daily_trading_runner.py` 负责原有选股和盘中监控。`scheduled_ashare_workflow.py` 负责跨项目的凌晨数据处理和早间 PushPlus 候选汇总；它使用 A 股交易日历，不会仅按工作日猜测法定节假日。

### 默认计划

| Windows 任务 | 中国时间 | 工作内容 |
| --- | --- | --- |
| `A-Share Daily Data Preparation` | 工作日 01:00 | 仅在 A 股交易日运行，处理前一实际交易日（周一自动取周五）的选股数据并归档；保留风险过滤，且不要求同时满足全部勾选条件，不发送消息。 |
| `A-Share Daily AI Evidence Analysis` | 工作日 09:05 | 使用当前项目的 `.venv` 从零执行 `ai_agent.py`，生成当天独立 AI 联网证据分析结果。 |
| `A-Share Daily Combined PushPlus Summary` | 工作日 09:25 | 仅在 A 股交易日运行，发送前一实际交易日的风险过滤候选（代码、名称、扣分项）、当天 AI 前十（代码、名称、主概念、个股得分、模型结论、风险等级、推荐等级、关键风险），以及两份前十名单的交集。当天 AI 结果未完成或无效时会明确提示，绝不回退发送历史 AI 结果。 |
| `SZSE Quant Morning Monitor` | 工作日 09:28 | 读取与预测日期一致的归档前 50 名并监控至 10:05；达到条件即时推送，10:05 固定推送候选池跌幅最大的股票。 |
```powershell
.\.venv\Scripts\python.exe .\daily_trading_runner.py --project-dir "D:\IndicatorScreening_Asheres" --mode monitor
```
预测阶段会覆盖以下当前文件，同时保留按日期归档版本：

- `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_current.json`
- `前 50 名（含所属行业）.csv`
- `每日交易信号.csv`

“前 50 名（含所属行业）”按全部已启用指标的得分排序，不应用风险过滤，也不要求每项指标同时满足，确保用于次日实时检测的候选池稳定提供 50 只。凌晨任务另存“风险过滤后得分前10.csv”：它不要求每项勾选条件同时满足，但会剔除命中任一已启用风险项的股票；全部被剔除时文件保留表头且 09:25 合并推送会明确显示无风险过滤候选。单只股票日线或因子获取失败时，会从当天评分、前 50 和实时候选排除，原因写入归档，不会中断整批任务。

盘中规则先比较精确跌幅，跌幅更深优先；相同时再按 `评分排名`、股票代码排序。触发阈值为 `<= -8.5%`，每只首次触发的候选股立即推送但绝不下单。无论是否触发，10:05 都会额外推送当刻跌幅最大的候选；未触发时消息会明确说明。

### 行情来源与额度

股票池刷新不会继承 Windows 的系统代理设置。它优先从深交所公开接口更新；该接口临时不可用时，程序使用智兔单股基础信息接口依次校验最近已验证的本地股票池并更新简称。包月版不使用全量列表或批量实时接口：

- 备用校验不会发现新上市股票；待深交所接口恢复后才纳入。
- 智兔不提供深交所官方行业字段，因此沿用既有官方行业映射；映射为空时明确写为“未知”。
- 所有请求（含重试）使用串行限速器，最高 480 次/分钟；每只股票最多请求 3 次，低于包月版每分钟 1,000 次限制。
- 单只校验失败时保留该股票上次已校验的代码、简称和行业。整批成功率低于 95% 或成功数不足 1,000 时，放弃本次替换并使用 7 个自然日内的本地校验池。没有可用旧文件或旧文件过期时，流程停止而不是静默使用陈旧数据。
- 实际来源、失败代码、未知行业代码、请求数和失败原因写入 `运行状态.json`，并在预测 PushPlus 消息中标注。

当前包月 token 使用 `/hs/real/ssjy/{code}` 单股实时接口，每 60 秒完整扫描 50 只股票，即每分钟 50 次、09:28-10:05 监控窗口约 1,900 次（包括 10:05 的完整快照）。实时数据本身按分钟更新，更短轮询没有有效增益。根目录 `.env` 至少需要：

```text
zhituapi=<智兔 token>
PushPlusapi=<PushPlus token>
zhituapi_daily_limit=unlimited
zhituapi_rate_limit_per_minute=1000
```

兼容旧字段 `PushPlus_token`，但优先使用 `PushPlusapi`。`.env` 不应提交到 Git。无需设置 `zhituapi_batch_quotes`；未设置时程序固定使用包月版单股接口。只有升级到支持批量接口的套餐后，才设置 `zhituapi_batch_quotes=true`。若更换成有限日额度 token，请将 `zhituapi_daily_limit` 设为实际正整数，程序会在预计超额时拒绝启动监控。

### 手动运行与恢复

先查看入口，不访问行情也不写文件：

```powershell
.\run_daily_runner.cmd --help
```

`run_daily_runner.cmd` 固定使用脚本所在目录的 `.venv` 和入口，并自动传入 `--project-dir <项目根目录>`；无论从哪个工作目录调用，都不会读错 `.env`、历史数据和输出目录。直接调用 Python 时同样要显式传入：

```powershell
.\.venv\Scripts\python.exe .\daily_trading_runner.py `
  --project-dir 'D:\IndicatorScreening_Asheres' --mode after-close --no-push
```

补跑收盘任务时可禁用推送：

```powershell
.\run_daily_runner.cmd --mode after-close --as-of-date YYYY-MM-DD --no-push
```

`--no-push` 不会把通知错误标记为已发送。不要在下一交易日 10:05 前随意补跑旧收盘任务，因为它会覆盖当前候选。

收盘预测产生的因子错误可在以下位置查看：

- `daily_trading_outputs/archive/YYYY-MM-DD/每日因子错误.csv`
- `daily_trading_outputs/archive/YYYY-MM-DD/运行状态.json` 的 `excluded_factor_codes`

这些股票只在当天候选中排除，不会从股票池、历史收益或日线缓存删除；下一交易日会再次尝试。收益缺失保留在“失败明细”中，并从滚动优化和当天候选排除，其余股票继续运行。

若收盘任务完成参数优化和因子抓取后、生成候选前异常退出，可用本地缓存恢复预测：

```powershell
.\run_daily_runner.cmd --mode recover-prediction --as-of-date YYYY-MM-DD
```

恢复模式只读取同日归档股票池、滚动收益工作簿、带日期的参数文件和 `data_cache/szse_quant/YYYY-MM-DD` 中的因子缓存。它会排除错误缓存、过期因子和格式不兼容缓存，再将当天日期版参数发布为固定参数文件；正常运行时不要使用该模式。

### 安装 Windows 计划任务

在项目根目录打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_daily_runner_tasks.ps1 -Action Install
```

安装后检查：

```powershell
.\install_daily_runner_tasks.ps1 -Action Show
```

默认使用与既有 `09:28` 监控一致的 Interactive 登录主体，因此任务会在 Windows 用户登录时运行，锁屏不受影响；注销后会等待下次登录。01:00 数据准备和 09:05 AI 分析均配置 15 分钟间隔重试，09:25 PushPlus 推送配置 5 分钟间隔重试；三者均忽略重复实例且最长运行 9 小时。安装时会精确注销历史的 `A-Share Top 10 AI Analysis` 04:00 任务和旧的 `A-Share Daily PushPlus Summary` 09:00 任务；`09:28` 监控不会被安装脚本改动。

安装脚本默认将 `..\A_Share_investment_Agent\.venv\Scripts\python.exe` 作为工作流任务解释器，将当前项目 `.venv\Scripts\python.exe` 作为 09:05 AI 解释器。若目录结构不同，请显式传入 `-AgentProjectDirectory`、`-AgentPythonPath` 和 `-AiAnalysisPythonPath`；09:05 AI 读取当前项目根目录 `.env`，01:00/09:25 工作流也必须能读取同一项目数据目录。

若要在注销后继续运行，可在管理员 PowerShell 中使用 S4U：

```powershell
.\install_daily_runner_tasks.ps1 -Action Install -RunWhetherLoggedOnOrNot
```

凌晨任务状态保存在 `daily_trading_outputs/scheduled_analysis/YYYY-MM-DD/data_preparation.json`，其中记录风险过滤后候选 CSV 路径和候选数；09:25 合并推送只读取该状态文件指定的 CSV，并且只接受 `ai_agent_outputs/YYYYMMDD/` 下运行清单日期等于当天、同时包含 `top10_recommendations.csv` 的最新 AI 批次，不会读取历史 AI 结果或回退到前 50 名。消息会展示 AI 前十的关键风险，并单列 AI 前十与风险过滤候选前十的交集。

卸载任务：

```powershell
.\install_daily_runner_tasks.ps1 -Action Uninstall
```

### 数据可靠性与行业口径

- 股票池、历史收益、CSV 和参数文件均使用唯一临时文件后原子替换，异常中断不会留下半写入的当前文件，也不会因旧临时文件占用阻塞下次运行。
- 每日候选、因子、收益、参数、信号和日志均归档到 `daily_trading_outputs/archive/YYYY-MM-DD/`。
- 若前一实际交易日的收盘数据尚未确认，01:00 数据准备任务不会写入完成状态，并由计划任务每 15 分钟重试。
- 少量日线因子失败时，任务仍以其余有效股票排名；全体因子不可用、数据日期不匹配或有效股票不足以形成前 50 时才停止并保留旧候选。
- 盘中只读取与 `每日交易信号.csv` 中预测日期匹配的归档候选。实时行情必须带有当日 09:28 后、且距当前轮询不超过 90 秒的时间戳；10:05 固定报告只接受 10:04-10:05 分钟的完整候选池行情。节假日或陈旧报价不会触发，也不会写成“无信号”；待监控信号会保留到下一实际开市日。
- 若程序在 10:05 行情可确认时限后启动，会写入“监控窗口错过”，不会伪造盘中结果。网络异常时由计划任务重试；运行锁具备进程所有权，旧进程不会删除新进程的锁。

`深交所数据.xlsx` 的“主板公司”工作表由深交所官方 A 股列表刷新生成，其中“所属行业”直接使用官方字段 `sshymc`。该字段是证监会行业门类口径（例如 `C 制造业`、`I 信息技术`），不能与申万、中信等细分行业口径混合比较。每日收盘任务会先刷新股票池，再将实际使用的同一版本归档到 `daily_trading_outputs/archive/YYYY-MM-DD/深交所数据.xlsx`；当天预测、前 50 候选和实时监控均沿用这一字段，不再进行二次行业映射。官方刷新要求代码、简称、官方行业完整且代码唯一；无效或重复代码会中止本次刷新，不覆盖现有股票池。盘中候选文件缺少“所属行业”列或有空行业时，监控会拒绝启动，不会静默使用人工映射或其他行业体系替代。

推荐以完整项目目录、`.venv`、`run_daily_runner.cmd` 和计划任务一起部署，而不是只使用单个 EXE，因为历史数据、缓存、日志、`.env` 和每日输出都需要长期可写目录。入口支持 `--project-dir <目录>`，迁移或制作独立部署包时应把该参数指向项目数据目录，而不是打包目录。

## 验证

安装完成后可运行全部自动化测试（测试套件使用 pytest；若环境未安装，先执行 `uv pip install --python .\.venv\Scripts\python.exe pytest`）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

仅验证每日后台相关模块：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_daily_trading_runner.py tests\test_intraday_trigger_monitor.py tests\test_screening_presets.py tests\test_rolling_parameter_optimizer.py tests\test_rolling_parameter_ensemble.py
```

## 独立 A 股联网证据分析

`ai_agent.py` 只在工作日 09:05 的独立计划任务中运行，不接入每日交易或盘中监控。它固定读取项目根目录的 `前 50 名（含所属行业）.csv`，只使用所有有效行中的 `股票代码` 和 `股票名称`；文件名不限制候选数量，行业和技术指标不会送入 AI。当前安装脚本调用的是这个入口，不是旧的 `scheduled_ashare_workflow.py --mode analyze` 批量分析路径。

AI 运行依赖安装和密钥配置见上文“安装”。至少需要 `DEEPSEEK_API_KEY` 和一个 `TAVILY_HUB_API_KEY1` 至 `TAVILY_HUB_API_KEY6`；不要把真实值写入 README、测试断言或日志。

安装依赖并运行：

```powershell
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python.exe ai_agent.py
```

### AI 分析阶段

1. **读取与清洗候选**：只要求 `股票代码`、`股票名称` 两列；股票代码统一为六位字符串，空值、非法代码、空名称和重复代码会被忽略，并写入 `run_manifest.json`。有效行有多少就分析多少，不假设一定是 50 只。
2. **概念识别**：DeepSeek 使用标准 `function` 工具调用请求 Tavily Hub 搜索，概念提示词要求使用市场通用的稳定主题简称，最多返回五个概念，并避免行业、地域、指数和宽泛标签。程序启动时会先执行一次联网能力预检。
3. **五维证据检索**：按 `board_strength`、`stock_funds`、`stock_news`、`market_analysis`、`stock_risk` 检索公开财经资料；无主概念时板块维度标记为跳过。每个维度最多保留五条证据，并记录查询、来源、发布时间、抓取时间和唯一 `evidence_id`。
4. **证据评分**：DeepSeek 只能依据带 `evidence_id` 的证据包，输出个股分、板块分、结论、风险等级、关键风险和证据引用。无效 JSON 会进行一次 JSON-only 修复，修复仍失败则该股标记为“分析失败”，不会中断其他股票。
5. **程序侧排序**：基础分为 `板块得分 × 0.6 + 个股得分 × 0.4`。至少三项证据状态为 `success/empty` 且风险维度完成时通过证据门槛；未通过时最终分最高封顶 6，带有效证据的重大风险最高封顶 4，分析失败最终分为 0。推荐等级只有 `建议关注`、`观察`、`不建议` 三类，最终按分数、证据门槛、风险等级和代码稳定排序，取前十。

### AI 输出与失败处理

每次运行均创建 `ai_agent_outputs/YYYYMMDD/HHMMSS/` 独立目录，不读取或写入 AI 缓存。主要文件如下：

| 文件 | 内容 |
| --- | --- |
| `candidates_normalized.csv` | 实际参与分析的有效候选 |
| `step1_concepts.jsonl`、`step1_concepts_summary.csv` | 概念原始响应和摘要 |
| `step2_search_results.jsonl`、`step2_search_results.csv` | 五维证据状态、证据 ID 和检索结果 |
| `all_rankings.csv` | 全部有效候选的评分、风险、门槛和排序 |
| `top10_recommendations.csv` | 排名最高的最多十只股票 |
| `run_manifest.json` | 输入文件、运行时间、版本、忽略行、阶段失败和输出文件清单 |

Tavily Hub key 按请求轮换；普通网络或 429/5xx 错误按短退避重试。`stock_news` 如果最终状态仍为 `failed`，会每隔 60 秒重试，直到成功，因此一次 AI 运行可能长时间等待。请不要在运行中手工删除输出目录或重复启动同一批次；先查看 `run_manifest.json` 和 raw 目录判断是否为单只股票失败。密钥、证据原文和 API 响应只保存在本机输出，分享结果时应先脱敏。

### 09:25 消息取数规则

合并消息由 `scheduled_ashare_workflow.py --mode send-combined` 生成。它先读取 `data_preparation.json` 指定的前一实际交易日 `风险过滤后得分前10.csv`，再在当天 `ai_agent_outputs/YYYYMMDD/` 中选择 `run_manifest.json` 日期匹配且包含 `top10_recommendations.csv` 的最新有效批次。消息包括风险过滤候选、AI 前十（含关键风险），以及按六位股票代码计算的两组前十交集；AI 结果不可用时不会回退到历史批次，会明确显示不可用原因。已发送状态存在时重复运行默认跳过，只有显式 `--force` 才会再次发送。
