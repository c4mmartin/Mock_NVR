# Creates .venv and installs deps (no global installs).
python scripts\bootstrap.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "OK. Run: scripts\mock-nvr.ps1 --help"
