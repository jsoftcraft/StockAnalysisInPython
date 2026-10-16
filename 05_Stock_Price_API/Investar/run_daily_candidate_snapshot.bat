@echo off
setlocal

set "REPO_ROOT=C:\GitHub\StockAnalysisInPython"
set "PYTHON=C:\GitHub\zunseoya\.venv-win32-py39\Scripts\python.exe"
set "SCRIPT=%REPO_ROOT%\05_Stock_Price_API\Investar\run_daily_trailing_stop_job.py"
set "LOG_DIR=%REPO_ROOT%\05_Stock_Price_API\Investar\logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%I in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "LOG_FILE=%LOG_DIR%\candidate_snapshot_%STAMP%.log"

pushd "%REPO_ROOT%"
"%PYTHON%" "%SCRIPT%" --market KOSPI >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%