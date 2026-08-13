# 启动后端（FastAPI + Uvicorn），后台运行，日志重定向到 backend.log
# 用法: powershell -File scripts/start-backend.ps1 [-Reload]
# 停止: powershell -File scripts/stop-backend.ps1

param(
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "找不到虚拟环境: $Python 。请先创建 .venv 并安装依赖。"
    exit 1
}

$LogFile = Join-Path $RepoRoot "backend.log"
$PidFile = Join-Path $RepoRoot "backend.pid"

if (Test-Path $PidFile) {
    $ExistingPid = Get-Content $PidFile
    if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) {
        Write-Error "后端似乎已在运行 (PID=$ExistingPid)。先执行 scripts/stop-backend.ps1 再重新启动。"
        exit 1
    }
}

$UvicornArgs = "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
if ($Reload) {
    $UvicornArgs += " --reload"
}

# 用 WMI（Win32_Process.Create）而不是 Start-Process 拉起子进程：
# Start-Process 创建的子进程仍然挂在调用者所在的 Windows Job Object 下——
# 如果调用者本身是个"退出时连带杀掉所有子孙进程"的临时进程（例如自动化
# 工具单次调用出的 shell），子进程会跟着一起被回收，表现为"脚本明明打印
# 了启动成功，几秒后进程却不见了"。经由 WMI 服务（winmgmt）创建的进程
# 挂在 WmiPrvSE.exe 下，不受调用者所在 Job 影响，调用者退出后依然存活。
$CmdLine = "cmd.exe /c `"`"$Python`" $UvicornArgs > `"$LogFile`" 2>&1`""
# cmd.exe 这里没有可继承的控制台，WMI 默认会给它新开一个可见窗口——
# 不显式隐藏的话，用户手一关那个窗口，里面的 uvicorn 就跟着被杀掉。
$StartupInfo = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine               = $CmdLine
    CurrentDirectory          = $RepoRoot
    ProcessStartupInformation = $StartupInfo
}
if ($result.ReturnValue -ne 0) {
    Write-Error "启动失败，Win32_Process.Create 返回码: $($result.ReturnValue)"
    exit 1
}
$NewPid = $result.ProcessId
$NewPid | Out-File -FilePath $PidFile -Encoding ascii -NoNewline

Write-Host "后端已在后台启动 (PID=$NewPid)，日志: $LogFile"
Write-Host "停止: powershell -File scripts/stop-backend.ps1"
