# 早间 AI 合并推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将早间 AI 证据分析安排在 09:05，并在 09:25 向 PushPlus 发送风险过滤候选、当天 AI 前十及二者交集的合并摘要。

**架构：** `scheduled_ashare_workflow.py` 保留原 `send` 入口，并新增 `send-combined`。新模式沿用上一实际交易日的风险过滤候选，同时只从调度日的 `ai_agent_outputs/YYYYMMDD` 读取带有效清单的最新 AI 运行结果；当天结果不可用时明确说明而不读取历史目录。安装脚本改为注册 09:05 AI 运行和 09:25 合并推送，并受保护地注销原 09:00 任务。

**技术栈：** Python 3、pandas、unittest、PowerShell ScheduledTasks。

---

### 任务 1：定义并测试合并消息行为

**文件：**
- 修改：`tests/test_scheduled_ashare_workflow.py`
- 修改：`scheduled_ashare_workflow.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_run_send_combined_reads_only_current_day_ai_top_ten(self) -> None:
    # Create a valid current-day run and an older run. Assert the sent HTML
    # includes current-day rows only and contains the requested AI fields and key risk.
    ...

def test_run_send_combined_does_not_fall_back_to_prior_day_ai_output(self) -> None:
    # Create only yesterday's valid run. Assert the send succeeds with the
    # explicit unavailable notice and does not include yesterday's stock.
    ...
```

- [ ] **步骤 2：运行测试验证失败**

运行：`..\A_Share_investment_Agent\.venv\Scripts\python.exe -m unittest tests.test_scheduled_ashare_workflow -v`

预期：FAIL，原因是 `run_send_combined` 尚未定义。

- [ ] **步骤 3：编写最少实现代码**

```python
def read_latest_completed_ai_top_ten(indicator_project: Path, scheduled_day: date) -> list[dict[str, str]]:
    # Require a manifest whose run_at date equals scheduled_day and read only
    # the requested columns from that run's top10 CSV.
    ...

def run_send_combined(... ) -> int:
    # Compose the existing screening section with the current-day AI section.
    ...
```

- [ ] **步骤 4：运行测试验证通过**

运行：`..\A_Share_investment_Agent\.venv\Scripts\python.exe -m unittest tests.test_scheduled_ashare_workflow -v`

预期：PASS。

### 任务 2：更新计划任务安装定义

**文件：**
- 修改：`install_daily_runner_tasks.ps1`
- 修改：`tests/test_scheduled_ashare_workflow.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_installer_schedules_ai_at_0905_and_combined_push_at_0925(self) -> None:
    installer = Path(workflow.__file__).with_name("install_daily_runner_tasks.ps1").read_text(encoding="utf-8")
    self.assertIn('"A-Share Daily AI Evidence Analysis"', installer)
    self.assertIn('"A-Share Daily Combined PushPlus Summary"', installer)
    self.assertIn('-At "09:05"', installer)
    self.assertIn('-At "09:25"', installer)
    self.assertIn('New-WorkflowAction "send-combined"', installer)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`..\A_Share_investment_Agent\.venv\Scripts\python.exe -m unittest tests.test_scheduled_ashare_workflow -v`

预期：FAIL，安装脚本尚未定义新任务。

- [ ] **步骤 3：编写最少实现代码**

```powershell
$workflowTaskNames = @(
    "A-Share Daily Data Preparation",
    "A-Share Daily AI Evidence Analysis",
    "A-Share Daily Combined PushPlus Summary"
)

Register-ScheduledTask -TaskName "A-Share Daily AI Evidence Analysis" -Action (New-AiAgentAction) -Trigger (New-WeekdayTrigger -At "09:05") ...
Register-ScheduledTask -TaskName "A-Share Daily Combined PushPlus Summary" -Action (New-WorkflowAction "send-combined") -Trigger (New-WeekdayTrigger -At "09:25") ...
```

- [ ] **步骤 4：运行测试验证通过**

运行：`..\A_Share_investment_Agent\.venv\Scripts\python.exe -m unittest tests.test_scheduled_ashare_workflow -v`

预期：PASS。

### 任务 3：更新操作文档并验证已安装任务

**文件：**
- 修改：`README.md`

- [ ] **步骤 1：更新默认计划表和状态说明**

```markdown
| `A-Share Daily AI Evidence Analysis` | 工作日 09:05 | 使用当前项目环境运行 `ai_agent.py`。 |
| `A-Share Daily Combined PushPlus Summary` | 工作日 09:25 | 发送风险过滤候选、当天 AI 前十的指定字段及二者交集；当天 AI 结果不可用时明确说明。 |
```

- [ ] **步骤 2：运行完整验证**

运行：`..\A_Share_investment_Agent\.venv\Scripts\python.exe -m unittest discover -s tests -v`

预期：PASS。

- [ ] **步骤 3：安装并检查 Windows 任务**

运行：`.\install_daily_runner_tasks.ps1 -Action Install`，随后运行 `.\install_daily_runner_tasks.ps1 -Action Show`。

预期：显示 01:00、09:05、09:25 三项任务；旧 `A-Share Daily PushPlus Summary` 不存在；`SZSE Quant Morning Monitor` 未被本脚本改动。
