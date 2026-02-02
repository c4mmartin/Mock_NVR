# Cross-platform launcher: delegates to scripts\mock-nvr.py which can
# auto-create .venv and install requirements.
$Root = Join-Path $PSScriptRoot ".."
& python (Join-Path $Root "scripts\mock-nvr.py") @args

