#requires -version 5.1
<#!
.SYNOPSIS
Installs, removes, or displays the two Windows Task Scheduler jobs used by
daily_trading_runner.py.

.DESCRIPTION
The jobs run from the project directory with its local .venv Python runtime.
They never contain API tokens; the runner reads .env at runtime.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("Install", "Uninstall", "Show")]
    [string]$Action = "Install",

    [string]$ProjectDirectory,

    [string]$PythonPath,

    [string]$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,

    # Use an interactive token instead of the default S4U background token.
    [switch]$OnlyWhenLoggedOn
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskNames = @(
    "SZSE Quant Daily After Close",
    "SZSE Quant Morning Monitor"
)

function Show-RunnerTasks {
    foreach ($taskName in $taskNames) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "Not installed: $taskName"
            continue
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
}

if ($Action -eq "Uninstall") {
    foreach ($taskName in $taskNames) {
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
    return
}

if ($Action -eq "Show") {
    Show-RunnerTasks
    return
}

if ([string]::IsNullOrWhiteSpace($ProjectDirectory)) {
    $ProjectDirectory = $PSScriptRoot
}

$projectPath = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$runnerPath = Join-Path -Path $projectPath -ChildPath "daily_trading_runner.py"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "daily_trading_runner.py was not found in: $projectPath"
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path -Path $projectPath -ChildPath ".venv\Scripts\python.exe"
}
$pythonExecutable = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $PythonPath"
}

$logonType = if ($OnlyWhenLoggedOn) { "Interactive" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $logonType -RunLevel Limited
$afterCloseSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$monitorSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 12 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

function New-RunnerAction([string]$Mode) {
    $arguments = '"{0}" --project-dir "{1}" --mode {2}' -f $runnerPath, $projectPath, $Mode
    return New-ScheduledTaskAction `
        -Execute $pythonExecutable `
        -Argument $arguments `
        -WorkingDirectory $projectPath
}

$definitions = @(
    [pscustomobject]@{
        Name = "SZSE Quant Daily After Close"
        Action = New-RunnerAction "after-close"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "17:00"
        Settings = $afterCloseSettings
        Description = "Runs the Shenzhen mainboard daily optimization and prediction pipeline at 17:00 on weekdays."
    },
    [pscustomobject]@{
        Name = "SZSE Quant Morning Monitor"
        Action = New-RunnerAction "monitor"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At "09:28"
        Settings = $monitorSettings
        Description = "Runs the Shenzhen mainboard real-time monitor from 09:28 through the fixed 09:45 report on weekdays."
    }
)

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
Write-Host "Use -Action Show to verify the scheduled tasks."
