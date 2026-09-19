param(
    [ValidateRange(1, 65535)][int]$Port = 5016,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

Push-Location -LiteralPath $ProjectRoot
try {
    $NeedsInstall = $Install.IsPresent
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
        # 每个项目使用独立环境，首次启动才安装依赖。
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python virtual environment.' }
        $NeedsInstall = $true
    }
    if ($NeedsInstall) {
        & $PythonPath -m pip install -r requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }

    # 数据库默认位于项目 data/app.db；自定义位置由应用配置读取。
    & $PythonPath -m flask --app app run --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'MOSS exited with an error.' }
}
finally {
    Pop-Location
}
