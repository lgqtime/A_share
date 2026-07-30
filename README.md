# 深市主板指标筛选与交易信号

这是一个独立的深市主板技术指标筛选、单股技术分析、严格次日收益回测和每日交易信号工具。项目使用公开行情接口，不需要 AkShare、模型密钥或 `tradingagents-astock` 的其他文件。

项目只读取行情、生成候选和发送 PushPlus 提醒，绝不连接券商或提交委托；买卖由使用者自行决定。

## 功能与入口

| 功能 | 入口 | 默认地址/用途 |
| --- | --- | --- |
| 日常选股与指标筛选 | `szse_quant_app.py` | `http://localhost:8504` |
| 单股技术分析 | `stock_analysis_app.py` | `http://localhost:8507` |
| 交互式回测 | `strategy_backtest/backtest_app.py` | `http://localhost:8501` |
| 固定规则命令行回测 | `strategy_backtest/run_backtest.py` | 导出 Excel 回测报表 |
| 每日滚动参数优化 | `strategy_backtest/rolling_parameter_optimizer.py` | 写入参数与回测 JSON |
| 每日后台交易信号 | `daily_trading_runner.py` | 收盘预测与次日盘中监控 |

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
|- daily_trading_runner.py                 # 唯一后台任务入口
|- intraday_trigger_monitor.py             # 次日盘中监控
|- run_daily_runner.cmd                    # 后台任务命令入口
|- install_daily_runner_tasks.ps1          # Windows 计划任务安装脚本
|- strategy_backtest/
|  |- backtest_app.py                      # 交互式回测页面
|  |- backtest_core.py                     # 回测计算核心
|  |- factor_batch.py                      # 批量因子计算
|  |- run_backtest.py                      # 固定规则命令行回测
|  |- warm_factor_cache.py                 # 回测因子预热
|  |- rolling_parameter_optimizer.py       # 每日滚动参数优化
|  |- runtime_strategy.py                  # 与预测模块共用的策略定义
|  |- szse_quant_app.py                    # 旧版界面兼容快照，不是每日优化策略来源
|  `- outputs/input_data/                  # 严格次日收益输入文件
`- tests/                                  # 自动化测试
```

`data_cache/`、`strategy_backtest/data_cache/`、`daily_trading_outputs/` 和 `strategy_backtest/outputs/rolling_parameter_updates/` 是本机运行数据。删除缓存不会删除代码或股票池，但下次运行会重新联网下载；每日滚动参数 JSON 建议保留。

如需强制重新拉取行情，可在页面启用“强制刷新”，或对支持该选项的命令行使用 `--force-refresh`。

## 安装

前置条件：Windows PowerShell、Python 3.10 或更高版本，以及 `uv` 命令行工具。

在项目根目录执行：

```powershell
uv sync --locked
uv run --locked python --version
uv run --locked python -c "import pandas, streamlit; print(streamlit.__version__)"
```

`uv sync --locked` 会按 `uv.lock` 创建 `.venv` 并安装固定版本的依赖。不要复制其他机器上的 `.venv`；虚拟环境与本机 Python 路径相关。迁移项目后请重新执行该命令。

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

### 每日滚动参数优化

滚动优化固定当前默认的勾选项与风险过滤，仅对已启用的 RSI、换手率、量比、涨幅、KDJ 金叉出现时间和 MACD 红蓝线区间进行坐标搜索。它只使用已有严格次日收益的实际交易日，避免将尚未验证的当日信号带入优化。

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer --lookback-days 30
```

可以用 `--as-of-date YYYY-MM-DD` 重跑历史日期，用 `--minimum-prediction-days N` 设置候选参数所需的最少有效预测天数。默认值为回看交易日的 20%，且不少于 3 天，避免因单次预测造成参数跳变。

未传入初始参数时，脚本自动读取当前运行日期之前最新的日期参数 JSON；首次运行则使用程序默认参数。也可手动指定上一份参数文件：

```powershell
uv run --locked python -m strategy_backtest.rolling_parameter_optimizer `
  --lookback-days 20 `
  --starter-json strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_YYYY-MM-DD.json
```

结果写入 `strategy_backtest/outputs/rolling_parameter_updates/`：

- `rolling_parameter_optimization_YYYY-MM-DD.json` 保存最佳参数、初始参数、固定筛选条件、数据窗口和优化摘要，供下一日继承。
- `rolling_parameter_optimization_current.json` 是固定当前参数文件，供每日预测读取。
- `rolling_parameter_backtest_YYYY-MM-DD.json` 保存完整回测报告；`worksheets` 中按“优化汇总”“参数对比”“每日回测”“优化路径”“数据问题”保留列名和逐行数据。

JSON 使用 UTF-8，日期采用 `YYYY-MM-DD`，缺失值写为 `null`；滚动优化报告不再生成 Excel 文件。

## 每日后台交易信号

`daily_trading_runner.py` 是唯一后台入口。已实现并验证中国法定休市日保护：即使工作日任务取得陈旧行情，也不会将上一交易日的候选或报价写成当天实时结果。

### 默认计划

| Windows 任务 | 中国时间 | 工作内容 |
| --- | --- | --- |
| `SZSE Quant Daily After Close` | 工作日 17:00 | 更新深市主板股票池和历史收益，用最近 30 个实际交易日优化参数，生成预测及不经风险过滤的前 50 名。 |
| `SZSE Quant Morning Monitor` | 工作日 09:28 | 读取与预测日期一致的归档前 50 名并监控至 09:45；达到条件即时推送，09:45 固定推送候选池跌幅最大的股票。 |

预测阶段会覆盖以下当前文件，同时保留按日期归档版本：

- `strategy_backtest/outputs/rolling_parameter_updates/rolling_parameter_optimization_current.json`
- `前 50 名（含所属行业）.csv`
- `每日交易信号.csv`

“前 50 名（含所属行业）”按全部已启用指标的得分排序，不应用风险过滤，也不要求每项指标同时满足，确保用于次日实时检测的候选池稳定提供 50 只。严格“得分最高的前 10 只股票”仍要求全部条件和风险过滤，因此第一名可能为空。单只股票日线或因子获取失败时，会从当天评分、前 50 和实时候选排除，原因写入归档，不会中断整批任务。

盘中规则先比较精确跌幅，跌幅更深优先；相同时再按 `评分排名`、股票代码排序。触发阈值为 `<= -8.5%`，每只首次触发的候选股立即推送但绝不下单。无论是否触发，09:45 都会额外推送当刻跌幅最大的候选；未触发时消息会明确说明。

### 行情来源与额度

股票池刷新不会继承 Windows 的系统代理设置。它优先从深交所公开接口更新；该接口临时不可用时，程序使用智兔单股基础信息接口依次校验最近已验证的本地股票池并更新简称。包月版不使用全量列表或批量实时接口：

- 备用校验不会发现新上市股票；待深交所接口恢复后才纳入。
- 智兔不提供深交所官方行业字段，因此沿用既有官方行业映射；映射为空时明确写为“未知”。
- 所有请求（含重试）使用串行限速器，最高 480 次/分钟；每只股票最多请求 3 次，低于包月版每分钟 1,000 次限制。
- 单只校验失败时保留该股票上次已校验的代码、简称和行业。整批成功率低于 95% 或成功数不足 1,000 时，放弃本次替换并使用 7 个自然日内的本地校验池。没有可用旧文件或旧文件过期时，流程停止而不是静默使用陈旧数据。
- 实际来源、失败代码、未知行业代码、请求数和失败原因写入 `运行状态.json`，并在预测 PushPlus 消息中标注。

当前包月 token 使用 `/hs/real/ssjy/{code}` 单股实时接口，每 60 秒完整扫描 50 只股票，即每分钟 50 次、每个交易日上午约 900 次（包括 09:45 的完整快照）。实时数据本身按分钟更新，更短轮询没有有效增益。根目录 `.env` 至少需要：

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

`--no-push` 不会把通知错误标记为已发送。不要在下一交易日 09:45 前随意补跑旧收盘任务，因为它会覆盖当前候选。

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

默认使用 S4U 后台登录，不保存 Windows 密码；锁屏或注销后仍可运行。收盘任务设置错过时间后尽快启动，早盘任务不会跨日补跑，避免把已错过窗口的旧候选用于下一交易日。两个任务均配置 5 分钟间隔重试、忽略重复实例和最长 6 小时运行时间；收盘任务最多重试 12 次。

若只希望登录期间运行：

```powershell
.\install_daily_runner_tasks.ps1 -Action Install -OnlyWhenLoggedOn
```

若默认 S4U 安装显示“Access is denied”，请以管理员身份运行 PowerShell 重试；也可使用 `-OnlyWhenLoggedOn`。后者不要求管理员权限，锁屏期间仍会运行，但注销 Windows 后会等待下次登录。

卸载任务：

```powershell
.\install_daily_runner_tasks.ps1 -Action Uninstall
```

### 数据可靠性与行业口径

- 股票池、历史收益、CSV 和参数文件均使用唯一临时文件后原子替换，异常中断不会留下半写入的当前文件，也不会因旧临时文件占用阻塞下次运行。
- 每日候选、因子、收益、参数、信号和日志均归档到 `daily_trading_outputs/archive/YYYY-MM-DD/`。
- 若 17:00 收盘数据尚未确认，任务不会覆盖候选，并将以失败状态交给计划任务每 5 分钟重试。
- 少量日线因子失败时，任务仍以其余有效股票排名；全体因子不可用、数据日期不匹配或有效股票不足以形成前 50 时才停止并保留旧候选。
- 盘中只读取与 `每日交易信号.csv` 中预测日期匹配的归档候选。实时行情必须带有当日 09:28 后、且距当前轮询不超过 90 秒的时间戳；09:45 固定报告只接受 09:45 分钟的完整候选池行情。节假日或陈旧报价不会触发，也不会写成“无信号”；待监控信号会保留到下一实际开市日。
- 若程序在 09:45 行情可确认时限后启动，会写入“监控窗口错过”，不会伪造盘中结果。网络异常时由计划任务重试；运行锁具备进程所有权，旧进程不会删除新进程的锁。

`深交所数据.xlsx` 的“主板公司”工作表由深交所官方 A 股列表刷新生成，其中“所属行业”直接使用官方字段 `sshymc`。该字段是证监会行业门类口径（例如 `C 制造业`、`I 信息技术`），不能与申万、中信等细分行业口径混合比较。每日收盘任务会先刷新股票池，再将实际使用的同一版本归档到 `daily_trading_outputs/archive/YYYY-MM-DD/深交所数据.xlsx`；当天预测、前 50 候选和实时监控均沿用这一字段，不再进行二次行业映射。官方刷新要求代码、简称、官方行业完整且代码唯一；无效或重复代码会中止本次刷新，不覆盖现有股票池。盘中候选文件缺少“所属行业”列或有空行业时，监控会拒绝启动，不会静默使用人工映射或其他行业体系替代。

推荐以完整项目目录、`.venv`、`run_daily_runner.cmd` 和计划任务一起部署，而不是只使用单个 EXE，因为历史数据、缓存、日志、`.env` 和每日输出都需要长期可写目录。入口支持 `--project-dir <目录>`，迁移或制作独立部署包时应把该参数指向项目数据目录，而不是打包目录。

## 验证

安装完成后可运行全部自动化测试：

```powershell
uv run --locked python -m unittest discover -s tests -v
```

仅验证每日后台相关模块：

```powershell
.\.venv\Scripts\python.exe -m unittest tests\test_daily_trading_runner.py tests\test_intraday_trigger_monitor.py tests\test_screening_presets.py tests\test_rolling_parameter_optimizer.py -v
```
