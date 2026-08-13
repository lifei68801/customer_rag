# 停止 scripts/start-backend.ps1 启动的后台后端进程

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "backend.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "未找到 backend.pid，后端可能未通过 start-backend.ps1 启动。"
    exit 0
}

$ProcId = Get-Content $PidFile
if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) {
    taskkill /PID $ProcId /T /F | Out-Null
    Write-Host "已停止后端 (PID=$ProcId)"
} else {
    Write-Host "进程 (PID=$ProcId) 已不存在，跳过。"
}

Remove-Item $PidFile -ErrorAction SilentlyContinue
