$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python -m compileall -q app governance_worker tests
python -m pytest -q
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
