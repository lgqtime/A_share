@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Python environment not found: "%PYTHON%"
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$listener = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($listener) { $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listener.OwningProcess); if ($process.CommandLine -notmatch '(?i)web_monitor\.py') { Write-Error 'Port 5000 is occupied by a process other than web_monitor.py.'; exit 1 }; Stop-Process -Id $listener.OwningProcess; Start-Sleep -Milliseconds 500 }"
if errorlevel 1 exit /b 1

start "Web Monitor" /B "%PYTHON%" "%ROOT%web_monitor.py"
echo Web monitor restarted. Current web_monitor.py and CSV will be loaded.
