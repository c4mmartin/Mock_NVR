@echo off
setlocal

REM Removes .venv (and optionally logs if you pass --remove-logs).
python scripts\uninstall.py --yes %*
