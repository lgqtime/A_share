@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "PROJECT_ROOT=%~dp0."
set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
set "RUNNER=%PROJECT_DIR%daily_trading_runner.py"

if not exist "%PYTHON_EXE%" (
    echo ERROR: Python runtime not found: "%PYTHON_EXE%"
    exit /b 1
)

if not exist "%RUNNER%" (
    echo ERROR: Runner not found: "%RUNNER%"
    exit /b 1
)

"%PYTHON_EXE%" "%RUNNER%" --project-dir "%PROJECT_ROOT%" %*
exit /b %ERRORLEVEL%
