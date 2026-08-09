# 风险过滤候选早间推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 01:00 保存风险过滤后的前 10 候选，09:00 仅推送候选代码、名称和扣分项，并撤销 04:00 AI 定时任务。

**架构：** `daily_trading_runner.py` 在其批处理候选分支显式关闭“满足全部条件”，但继续传入既有风险项。`scheduled_ashare_workflow.py` 将归档的 `风险过滤后得分前10.csv` 记录为凌晨产物，09:00 仅读取该文件；空 CSV 表示无候选而不是失败。安装脚本只管理 01:00 和 09:00 两个 A-Share 任务，并精确注销历史 04:00 任务。

**技术栈：** Python 3、pandas、unittest、Windows Task Scheduler、PowerShell 5.1。

---

### 任务 1：筛选候选语义

**文件：**
- 修改：`D:\IndicatorScreening_Asheres\tests\test_daily_trading_runner.py`
- 修改：`D:\IndicatorScreening_Asheres\daily_trading_runner.py`

- [ ] **步骤 1：编写失败的测试**

在现有 `build_prediction()` 测试中断言第一次 `score_and_select()` 调用带有 `require_all=False`，并保留原有风险项参数。

- [ ] **步骤 2：运行测试验证失败**

运行：`D:\IndicatorScreening_Asheres\.venv\Scripts\python.exe -m unittest tests.test_daily_trading_runner.DailyTradingRunnerTests.test_prediction_uses_fixed_parameter_file_and_writes_top_fifty`

预期：失败，因为当前首次调用继承 UI 默认的 `require_all=True`。

- [ ] **步骤 3：编写最少实现代码**

在 `build_prediction()` 中构造仅用于风险筛选批处理分支的 `score_options`，将 `require_all` 覆盖为 `False`；保留 `selected_risks`。

- [ ] **步骤 4：运行测试验证通过**

运行相同命令，预期：PASS。

### 任务 2：九点候选消息

**文件：**
- 修改：`D:\IndicatorScreening_Asheres\tests\test_scheduled_ashare_workflow.py`
- 修改：`D:\IndicatorScreening_Asheres\scheduled_ashare_workflow.py`

- [ ] **步骤 1：编写失败的测试**

覆盖风险过滤候选 CSV 的 0 行、1 行和 10 行情况；断言 HTML 只含代码、名称、`未满足条件（扣分项）`，并在零行时说明风险过滤后无候选。覆盖 `run_send()` 只接受 `data_preparation.json` 的候选文件而不读取 `analysis.json`。

- [ ] **步骤 2：运行测试验证失败**

运行：`D:\IndicatorScreening_Asheres\.venv\Scripts\python.exe -m unittest tests.test_scheduled_ashare_workflow`

预期：失败，因为当前读取 `analysis.json` 且要求恰好十行。

- [ ] **步骤 3：编写最少实现代码**

新增允许空候选文件的读取函数；收集阶段记录 `风险过滤后得分前10.csv`，发送阶段只读取该路径，写出三列 HTML，且绝不回退到前 50 或 AI 结果。移除 CLI 的 `analyze` 公开模式和不再使用的 AI 工作流路径。

- [ ] **步骤 4：运行测试验证通过**

运行相同命令，预期：PASS。

### 任务 3：任务定义与说明

**文件：**
- 修改：`D:\IndicatorScreening_Asheres\install_daily_runner_tasks.ps1`
- 修改：`D:\IndicatorScreening_Asheres\README.md`

- [ ] **步骤 1：编写失败的静态验证**

用 PowerShell 读取安装脚本，断言不存在 04:00 定义，包含精确的历史任务注销，并保留 `SZSE Quant Morning Monitor` 只读展示。

- [ ] **步骤 2：运行验证确认失败**

运行该静态验证，预期：失败，因为当前仍注册 04:00。

- [ ] **步骤 3：编写最少实现代码**

从 A-Share 管理集合和定义数组移除 04:00；在 Install 与 Uninstall 路径精确注销 `A-Share Top 10 AI Analysis`。README 改为说明 01:00 风险过滤候选、09:00 三列摘要、保留 09:28 监控。

- [ ] **步骤 4：执行安装脚本和验证**

运行：`powershell -ExecutionPolicy Bypass -File D:\IndicatorScreening_Asheres\install_daily_runner_tasks.ps1 -Action Install`

预期：只更新 01:00、09:00 A-Share 任务并删除精确的 04:00 任务；不修改 09:28。
