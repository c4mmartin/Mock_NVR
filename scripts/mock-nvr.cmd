@echo off
setlocal

REM Cross-platform launcher: delegates to scripts\mock-nvr.py which can
REM auto-create .venv and install requirements.
set ROOT=%~dp0..
python "%ROOT%\scripts\mock-nvr.py" %*

