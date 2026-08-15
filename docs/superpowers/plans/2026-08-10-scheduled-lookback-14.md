# 01:00 定时参数窗口改为 14 日实现计划

> **面向 AI 代理的工作者：** 执行时使用测试先行方式，先锁定每日流程传给优化器的参数，再做最小实现变更。

**目标：** 让工作日 01:00 的数据准备任务每次调用滚动参数优化器时使用 14 个实际交易日。

**架构：** Windows 定时任务调用 `scheduled_ashare_workflow.py --mode collect`，该流程再调用 `daily_trading_runner.py` 的 `run_after_close`。后者构建优化器参数列表，显式传入 `--lookback-days`；将该实参从 `30` 改为 `14` 即可，不改变任务调度配置。

**技术栈：** Python、unittest、Windows Task Scheduler。

---

### 任务 1：锁定每日流程的优化器窗口

**文件：**
- 修改：`tests/test_daily_trading_runner.py`
- 修改：`daily_trading_runner.py:1462-1463`

- [x] **步骤 1：编写失败的测试**

在 `test_after_close` 的优化器调用断言中，验证传入参数包含连续项：

```python
assert optimizer_arguments[optimizer_arguments.index("--lookback-days") + 1] == "14"
```

- [x] **步骤 2：运行针对性测试并确认失败**

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_daily_trading_runner -v
```

预期：与 `--lookback-days` 值相关的断言失败，因为当前生产代码传入 `"30"`。

- [x] **步骤 3：最小实现变更**

将 `daily_trading_runner.py` 中优化器调用参数改为：

```python
"--lookback-days",
"14",
```

- [x] **步骤 4：运行针对性测试并确认通过**

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_daily_trading_runner -v
```

预期：所有每日任务测试通过。

### 任务 2：核验调度行为未被意外改变

**文件：**
- 读取：`install_daily_runner_tasks.ps1`
- 读取：Windows 任务 `A-Share Daily Data Preparation`

- [x] **步骤 1：检查任务触发时间与入口**

运行：

```powershell
Get-ScheduledTask -TaskName 'A-Share Daily Data Preparation'
```

预期：任务仍在工作日 01:00 触发，入口仍为 `scheduled_ashare_workflow.py --mode collect`。

- [ ] **步骤 2：执行完整相关测试和静态检查**

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_daily_trading_runner tests.test_rolling_parameter_optimizer -v
.\.venv\Scripts\python.exe -m py_compile daily_trading_runner.py strategy_backtest\rolling_parameter_optimizer.py
git diff --check -- daily_trading_runner.py tests/test_daily_trading_runner.py
```

预期：测试与编译均成功，且 diff 无空白错误。
