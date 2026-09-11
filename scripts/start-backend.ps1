# 启动后端（FastAPI + Uvicorn），后台运行，日志重定向到 backend.log
# 启动前会先停止已在运行的旧进程，避免端口占用或残留旧代码在跑
# 用法: powershell -File scripts/start-backend.ps1 [-Reload]
# 停止: powershell -File scripts/stop-backend.ps1

param(
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

& (Join-Path $PSScriptRoot "stop-backend.ps1")
# 给操作系统一点时间真正释放端口，避免新进程绑定 0.0.0.0:8000 时撞上
# 旧进程刚退出、端口还没完全放出来的极短窗口期
Start-Sleep -Milliseconds 500

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "找不到虚拟环境: $Python 。请先创建 .venv 并安装依赖。"
    exit 1
}

$LogFile = Join-Path $RepoRoot "backend.log"
$PidFile = Join-Path $RepoRoot "backend.pid"
$Port = 8000

$UvicornArgs = "-m uvicorn app.main:app --host 0.0.0.0 --port $Port"
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

# Win32_Process.Create 返回 0 只说明"进程创建成功"，**不代表 uvicorn 起来了**。
# 最常见的失败是端口被别的进程占着：uvicorn 绑定 0.0.0.0:8000 失败、立刻退出，
# 而脚本已经打印了"已在后台启动"——那句话是假的，用户拿着它去刷页面，得到的
# 是一个指不到原因的连接错误。
#
# **判据是"我们起的这个进程还活着"，不是"端口在监听"**：绑定失败的场景里端口
# 恰恰是在监听的（被占用它的那个进程监听着），只探端口会把失败报成成功。
# $NewPid 是 cmd.exe 的 pid，python 一退出它就跟着退出，所以拿它当存活信号。
# 判据是"占着这个端口的进程是不是我们刚起的那个的后代"。
#
# 光问"端口在监听吗"或者"有 HTTP 响应吗"都不够——绑定失败的场景里，端口正被
# **别人**占着、别人也在应答。实测踩过：用 `python -m http.server 8000` 占住端口
# 再跑本脚本，HTTP 探测拿到占位进程的 404 就报了成功。探测必须回答"应答的是谁"。
function Test-OwnedByUs {
    param([int]$RootPid, [int]$Port)
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $false }
    # 顺着父进程链往上找：uvicorn 是 cmd.exe（$RootPid）的子进程。
    $cur = $conn.OwningProcess
    for ($hop = 0; $hop -lt 6; $hop++) {
        if ($cur -eq $RootPid) { return $true }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $cur" -ErrorAction SilentlyContinue
        if (-not $proc -or $proc.ParentProcessId -eq 0) { return $false }
        $cur = $proc.ParentProcessId
    }
    return $false
}

$Ready = $false
for ($i = 0; $i -lt 40; $i++) {
    if (Test-OwnedByUs -RootPid $NewPid -Port $Port) { $Ready = $true; break }
    if (-not (Get-Process -Id $NewPid -ErrorAction SilentlyContinue)) {
        break   # 我们起的进程没了：多半是绑定失败，走下面的报错分支
    }
    Start-Sleep -Milliseconds 500
}

if (-not $Ready) {
    Write-Host "后端没能起来（等了 20 秒）。日志末尾："
    if (Test-Path $LogFile) { Get-Content -Path $LogFile -Tail 15 | ForEach-Object { Write-Host "  $_" } }
    else { Write-Host "  （日志文件还没生成：$LogFile）" }
    $Occupier = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($Occupier) {
        $OccProc = Get-Process -Id $Occupier.OwningProcess -ErrorAction SilentlyContinue
        Write-Host "端口 $Port 正被 PID=$($Occupier.OwningProcess) ($($OccProc.ProcessName)) 占用。"
        Write-Host "它不在 backend.pid 里，所以 stop-backend.ps1 停不掉它——确认可以停的话："
        Write-Host "  taskkill /PID $($Occupier.OwningProcess) /F"
    }
    exit 1
}

Write-Host "后端已在后台启动 (PID=$NewPid)，日志: $LogFile"
Write-Host "访问地址: http://localhost:$Port  (API 文档: http://localhost:$Port/docs)"
Write-Host "停止: powershell -File scripts/stop-backend.ps1"
