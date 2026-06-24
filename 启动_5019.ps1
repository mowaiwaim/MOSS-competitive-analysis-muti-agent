$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "未发现 .venv，正在创建虚拟环境..."
    python -m venv .venv
}

$env:PORT = "5019"
$env:MOSS_DATABASE = Join-Path $Root "data\app_5019_clean.db"

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 5019
