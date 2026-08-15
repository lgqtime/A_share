#requires -version 5.1
<#
.SYNOPSIS
Installs the A-share daily data preparation, AI analysis, and PushPlus summary tasks.

.DESCRIPTION
The workflow tasks start on weekdays, but scheduled_ashare_workflow.py checks
the actual A-share trading calendar before doing any work.  It uses the prior
actual trading day at 01:00, so Monday naturally processes Friday and public
holidays are skipped.  The existing 09:28 monitor is displayed but never
registered, changed, or removed by this script.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Uninstall", "Show")]
    [string]$Action = "Install",

    [string]$ProjectDirectory,

    [string]$AgentProjectDirectory,

    [string]$AgentPythonPath,

    [string]$AiAnalysisPythonPath,

    [string]$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,

    # Keep compatibility with earlier installer invocations.  Interactive is
    # already the default because this Windows account can register it without
    # elevation, matching the existing 09:28 monitor task.
    [switch]$OnlyWhenLoggedOn,

    # S4U runs when logged off but normally requires an elevated installation.
    [switch]$RunWhetherLoggedOnOrNot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$legacyTaskName = "SZSE Quant Daily After Close"
$monitorTaskName = "SZSE Quant Morning Monitor"
$retiredAiTaskName = "A-Share Top 10 AI Analysis"
$retiredPushplusTaskName = "A-Share Daily PushPlus Summary"
$workflowTaskNames = @(
    "A-Share Daily Data Preparation",
    "A-Share Daily AI Evidence Analysis",
    "A-Share Daily Combined PushPlus Summary"
)

function Show-Task([string]$TaskName) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Not installed: $TaskName"
        return
    }
    $info = Get-ScheduledTaskInfo -InputObject $task
    [pscustomobject]@{
        TaskName = $task.TaskName
        State = $task.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = $info.LastTaskResult
        NextRunTime = $info.NextRunTime
    }
}

function Test-LegacyAfterCloseTask($Task) {
    $actionText = @(
        $Task.Actions | ForEach-Object { "{0} {1}" -f $_.Execute, $_.Arguments }
    ) -join " "
    $hasLegacyAction = $actionText -match "daily_trading_runner\.py"
    $hasFivePmTrigger = @(
        $Task.Triggers | Where-Object { [string]$_.StartBoundary -match "T17:00:" }
    ).Count -gt 0
    return $hasLegacyAction -and $hasFivePmTrigger
}

function Remove-LegacyAfterCloseTask {
    $task = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return
    }
    if (-not (Test-LegacyAfterCloseTask $task)) {
        Write-Warning "The task '$legacyTaskName' does not match the verified 17:00 legacy definition; leaving it unchanged."
        return
    }
    if ($PSCmdlet.ShouldProcess($legacyTaskName, "remove verified 17:00 legacy task")) {
        Unregister-ScheduledTask -InputObject $task -Confirm:$false
        Write-Host "Removed legacy 17:00 task: $legacyTaskName"
    }
}

function Test-RetiredAiAnalysisTask($Task) {
    $actionText = @(
        $Task.Actions | ForEach-Object { "{0} {1}" -f $_.Execute, $_.Arguments }
    ) -join " "
    $hasWorkflowAction = $actionText -match "scheduled_ashare_workflow\.py"
    $hasAnalyzeMode = $actionText -match "--mode\s+analyze\b"
    $hasFourAmTrigger = @(
        $Task.Triggers | Where-Object { [string]$_.StartBoundary -match "T04:00:" }
    ).Count -gt 0
    return $hasWorkflowAction -and $hasAnalyzeMode -and $hasFourAmTrigger
}

function Remove-RetiredAiAnalysisTask {
    $task = Get-ScheduledTask `
        -TaskName $retiredAiTaskName `
        -TaskPath "\" `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return
    }
    if (-not (Test-RetiredAiAnalysisTask $task)) {
        Write-Warning "The task '$retiredAiTaskName' does not match the verified 04:00 AI definition; leaving it unchanged."
        return
    }
    if ($PSCmdlet.ShouldProcess($retiredAiTaskName, "remove retired 04:00 AI analysis task")) {
        Unregister-ScheduledTask -InputObject $task -Confirm:$false
        Write-Host "Removed retired 04:00 task: $retiredAiTaskName"
    }
}

function Test-RetiredPushplusSummaryTask($Task) {
    $actionText = @(
        $Task.Actions | ForEach-Object { "{0} {1}" -f $_.Execute, $_.Arguments }
    ) -join " "
    $hasWorkflowAction = $actionText -match "scheduled_ashare_workflow\.py"
    $hasSendMode = $actionText -match '--mode\s+send(?=\s|$|")'
    $hasNineAmTrigger = @(
        $Task.Triggers | Where-Object { [string]$_.StartBoundary -match "T09:00:" }
    ).Count -gt 0
    return $hasWorkflowAction -and $hasSendMode -and $hasNineAmTrigger
}

function Remove-RetiredPushplusSummaryTask {
    $task = Get-ScheduledTask `
        -TaskName $retiredPushplusTaskName `
        -TaskPath "\" `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return
    }
    if (-not (Test-RetiredPushplusSummaryTask $task)) {
        Write-Warning "The task '$retiredPushplusTaskName' does not match the verified 09:00 PushPlus definition; leaving it unchanged."
        return
    }
    if ($PSCmdlet.ShouldProcess($retiredPushplusTaskName, "remove verified 09:00 PushPlus summary task")) {
        Unregister-ScheduledTask -InputObject $task -Confirm:$false
        Write-Host "Removed retired 09:00 task: $retiredPushplusTaskName"
    }
}

if ($Action -eq "Show") {
    foreach ($taskName in @($workflowTaskNames + $retiredPushplusTaskName + $retiredAiTaskName + $monitorTaskName + $legacyTaskName)) {
        Show-Task $taskName
    }
    return
}

if ($Action -eq "Uninstall") {
    foreach ($taskName in $workflowTaskNames) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "Not installed: $taskName"
            continue
        }
        if ($PSCmdlet.ShouldProcess($taskName, "remove scheduled task")) {
            Unregister-ScheduledTask -InputObject $task -Confirm:$false
            Write-Host "Removed: $taskName"
        }
    }
    Remove-LegacyAfterCloseTask
    Remove-RetiredAiAnalysisTask
    Remove-RetiredPushplusSummaryTask
    Write-Host "The existing 09:28 monitor task was left untouched."
    return
}

if ([string]::IsNullOrWhiteSpace($ProjectDirectory)) {
    $ProjectDirectory = $PSScriptRoot
}
$projectPath = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$workflowPath = Join-Path -Path $projectPath -ChildPath "scheduled_ashare_workflow.py"
if (-not (Test-Path -LiteralPath $workflowPath -PathType Leaf)) {
    throw "scheduled_ashare_workflow.py was not found in: $projectPath"
}
$aiAnalysisPath = Join-Path -Path $projectPath -ChildPath "ai_agent.py"
if (-not (Test-Path -LiteralPath $aiAnalysisPath -PathType Leaf)) {
    throw "ai_agent.py was not found in: $projectPath"
}
if ([string]::IsNullOrWhiteSpace($AiAnalysisPythonPath)) {
    $AiAnalysisPythonPath = Join-Path -Path $projectPath -ChildPath ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $AiAnalysisPythonPath -PathType Leaf)) {
    throw "AI analysis Python executable was not found: $AiAnalysisPythonPath"
}
$aiAnalysisPythonExecutable = (Resolve-Path -LiteralPath $AiAnalysisPythonPath).Path

if ([string]::IsNullOrWhiteSpace($AgentProjectDirectory)) {
    $AgentProjectDirectory = Join-Path -Path (Split-Path -Path $projectPath -Parent) -ChildPath "A_Share_investment_Agent"
}
$agentProjectPath = (Resolve-Path -LiteralPath $AgentProjectDirectory).Path

if ([string]::IsNullOrWhiteSpace($AgentPythonPath)) {
    $AgentPythonPath = Join-Path -Path $agentProjectPath -ChildPath ".venv\Scripts\python.exe"
}
$agentPythonExecutable = (Resolve-Path -LiteralPath $AgentPythonPath).Path
if (-not (Test-Path -LiteralPath $agentPythonExecutable -PathType Leaf)) {
    throw "A-share AI Python executable was not found: $AgentPythonPath"
}

if ($OnlyWhenLoggedOn -and $RunWhetherLoggedOnOrNot) {
    throw "OnlyWhenLoggedOn and RunWhetherLoggedOnOrNot cannot be used together."
}
$logonType = if ($RunWhetherLoggedOnOrNot) { "S4U" } else { "Interactive" }
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $logonType -RunLevel Limited
$defaultSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 9)
$pushplusSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 9)
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

function New-WorkflowAction([string]$Mode) {
    $arguments = '"{0}" --indicator-project-dir "{1}" --agent-project-dir "{2}" --mode {3}' -f `
        $workflowPath, $projectPath, $agentProjectPath, $Mode
    return New-ScheduledTaskAction `
        -Execute $agentPythonExecutable `
        -Argument $arguments `
        -WorkingDirectory $agentProjectPath
}

function New-AiAgentAction {
    $arguments = '"{0}"' -f $aiAnalysisPath
    return New-ScheduledTaskAction `
        -Execute $aiAnalysisPythonExecutable `
        -Argument $arguments `
        -WorkingDirectory $projectPath
}

$definitions = @(
    [pscustomobject]@{
        Name = "A-Share Daily Data Preparation"
        Action = New-WorkflowAction "collect"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "01:00"
        Settings = $defaultSettings
        Description = "At 01:00 on weekdays, persist the prior actual A-share trading day's screening data without sending a message."
    },
    [pscustomobject]@{
        Name = "A-Share Daily AI Evidence Analysis"
        Action = New-AiAgentAction
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "09:05"
        Settings = $defaultSettings
        Description = "At 09:05 on weekdays, run the current project's independent AI evidence analysis from scratch."
    },
    [pscustomobject]@{
        Name = "A-Share Daily Combined PushPlus Summary"
        Action = New-WorkflowAction "send-combined"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "09:25"
        Settings = $pushplusSettings
        Description = "At 09:25 on trading days, send risk-filtered candidates, the current day's AI top-ten summary, and their intersection through PushPlus."
    }
)

Remove-LegacyAfterCloseTask
Remove-RetiredAiAnalysisTask
Remove-RetiredPushplusSummaryTask
foreach ($definition in $definitions) {
    if ($PSCmdlet.ShouldProcess($definition.Name, "install or update scheduled task")) {
        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action $definition.Action `
            -Trigger $definition.Trigger `
            -Settings $definition.Settings `
            -Principal $principal `
            -Description $definition.Description `
            -Force `
            -ErrorAction Stop | Out-Null
        Write-Host "Installed: $($definition.Name)"
    }
}

Write-Host "Logon type: $logonType"
Write-Host "The existing 09:28 monitor task was left untouched."
Write-Host "Use -Action Show to verify all task times."
