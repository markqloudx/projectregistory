$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:APP_CONFIG_PATH = "config/app_config.local.yaml"
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
