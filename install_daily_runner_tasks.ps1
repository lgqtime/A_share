#requires -version 5.1
<#
.SYNOPSIS
Installs the A-share daily data preparation and PushPlus summary tasks.

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
$workflowTaskNames = @(
    "A-Share Daily Data Preparation",
    "A-Share Daily PushPlus Summary"
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

if ($Action -eq "Show") {
    foreach ($taskName in @($workflowTaskNames + $retiredAiTaskName + $monitorTaskName + $legacyTaskName)) {
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
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
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

$definitions = @(
    [pscustomobject]@{
        Name = "A-Share Daily Data Preparation"
        Action = New-WorkflowAction "collect"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "01:00"
        Description = "At 01:00 on weekdays, persist the prior actual A-share trading day's screening data without sending a message."
    },
    [pscustomobject]@{
        Name = "A-Share Daily PushPlus Summary"
        Action = New-WorkflowAction "send"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "09:00"
        Description = "At 09:00 on trading days, send the persisted prior-trading-day risk-filtered candidates through PushPlus."
    }
)

Remove-LegacyAfterCloseTask
Remove-RetiredAiAnalysisTask
foreach ($definition in $definitions) {
    if ($PSCmdlet.ShouldProcess($definition.Name, "install or update scheduled task")) {
        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action $definition.Action `
            -Trigger $definition.Trigger `
            -Settings $settings `
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
