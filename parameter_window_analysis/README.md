# 参数窗口分析

`analyze_windows.py` 在独立目录中运行每日参数优化器，比较截至 2026-08-06 的 30 至 5 个实际交易日窗口。它不会写入或覆盖 `strategy_backtest/outputs/rolling_parameter_updates/` 中的生产参数文件。

运行：

```powershell
.\.venv\Scripts\python.exe .\parameter_window_analysis\analyze_windows.py
```

输出位于 `parameter_window_analysis/results/`：

- `窗口收益汇总.csv`：每个窗口的最佳结果。`平均每日收益率（%）` 按整个窗口的实际交易日数做复利折算，未形成预测的交易日按 0% 收益计入；`有效预测日平均收益率（%）` 仅按实际形成预测的交易日折算。
- `最佳参数明细.csv`：逐窗口、逐参数的最优区间。
- `optimization_runs/lookback_XX_days/`：每个窗口的完整优化参数与回测 JSON。
- `失败窗口.csv` 和 `运行清单.json`：执行状态与输入记录。

每个窗口使用单独的输出目录，因此都从程序默认参数独立开始，不会继承其他窗口的结果。脚本会一次性准备最大 30 日窗口所需的历史和因子数据，再将其按日期裁剪给每个较短窗口；参数坐标搜索与结果写入仍直接复用每日优化模块的逻辑。

执行中断后直接重跑同一命令即可：已有的窗口 JSON 会被复用，只有缺失窗口才会重新优化。若只需从现有 JSON 重建汇总文件，执行：

```powershell
.\.venv\Scripts\python.exe .\parameter_window_analysis\analyze_windows.py --summarize-only
```
