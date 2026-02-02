@echo off
setlocal

REM Creates .venv and installs deps (no global installs).
python scripts\bootstrap.py
if %errorlevel% neq 0 exit /b %errorlevel%
echo.
echo OK. Run: scripts\mock-nvr.cmd --help
