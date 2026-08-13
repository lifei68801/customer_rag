# 停止 scripts/start-frontend.ps1 启动的后台前端进程

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "frontend.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "未找到 frontend.pid，前端可能未通过 start-frontend.ps1 启动。"
    exit 0
}

$ProcId = Get-Content $PidFile
if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) {
    taskkill /PID $ProcId /T /F | Out-Null
    Write-Host "已停止前端 (PID=$ProcId)"
} else {
    Write-Host "进程 (PID=$ProcId) 已不存在，跳过。"
}

Remove-Item $PidFile -ErrorAction SilentlyContinue
