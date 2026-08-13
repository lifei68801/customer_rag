# 启动前端（Vite + React 开发服务器），后台运行，日志重定向到 frontend.log
# 启动前会先停止已在运行的旧进程，避免端口占用或残留旧代码在跑
# 用法: powershell -File scripts/start-frontend.ps1
# 停止: powershell -File scripts/stop-frontend.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$FrontendDir = Join-Path $RepoRoot "frontend"
Set-Location $FrontendDir

& (Join-Path $PSScriptRoot "stop-frontend.ps1")
# 给操作系统一点时间真正释放端口，避免新进程绑定时撞上旧进程刚退出、
# 端口还没完全放出来的极短窗口期
Start-Sleep -Milliseconds 500

if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Host "未发现 node_modules，先执行 npm install ..."
    npm install
}

$LogFile = Join-Path $RepoRoot "frontend.log"
$PidFile = Join-Path $RepoRoot "frontend.pid"

# 用 WMI（Win32_Process.Create）而不是 Start-Process 拉起子进程：
# Start-Process 创建的子进程仍然挂在调用者所在的 Windows Job Object 下——
# 如果调用者本身是个"退出时连带杀掉所有子孙进程"的临时进程（例如自动化
# 工具单次调用出的 shell），子进程会跟着一起被回收，表现为"脚本明明打印
# 了启动成功，几秒后进程却不见了"。经由 WMI 服务（winmgmt）创建的进程
# 挂在 WmiPrvSE.exe 下，不受调用者所在 Job 影响，调用者退出后依然存活。
$CmdLine = "cmd.exe /c `"npm run dev > `"$LogFile`" 2>&1`""
# cmd.exe 这里没有可继承的控制台，WMI 默认会给它新开一个可见窗口——
# 不显式隐藏的话，用户手一关那个窗口，里面的 vite 就跟着被杀掉。
$StartupInfo = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine               = $CmdLine
    CurrentDirectory          = $FrontendDir
    ProcessStartupInformation = $StartupInfo
}
if ($result.ReturnValue -ne 0) {
    Write-Error "启动失败，Win32_Process.Create 返回码: $($result.ReturnValue)"
    exit 1
}
$NewPid = $result.ProcessId
$NewPid | Out-File -FilePath $PidFile -Encoding ascii -NoNewline

Write-Host "前端已在后台启动 (PID=$NewPid)，日志: $LogFile"

# Vite 实际监听的端口可能因为默认的 5173 被占用而自动往后跳（5174、5175...），
# 所以访问地址不能硬编码，去日志里等它自己打印出的地址为准。实测哪怕输出被
# 重定向到文件，vite 仍然会带 ANSI 颜色控制码（\x1b[1m 等）——不剥掉的话
# "\S+" 会把控制码也当成 URL 的一部分吞进去，取到的地址点不开。
$AccessUrl = $null
$AnsiEscapePattern = [char]27 + "\[[0-9;]*[a-zA-Z]"
for ($i = 0; $i -lt 30; $i++) {
    if (Test-Path $LogFile) {
        $CleanContent = (Get-Content -Path $LogFile -Raw -ErrorAction SilentlyContinue) -replace $AnsiEscapePattern, ""
        if ($CleanContent -match "http://\S+") {
            $AccessUrl = $Matches[0]
            break
        }
    }
    Start-Sleep -Milliseconds 500
}
if ($AccessUrl) {
    Write-Host "访问地址: $AccessUrl"
} else {
    Write-Host "访问地址: 暂未在日志中检测到（默认应为 http://localhost:5173），请查看 $LogFile"
}
Write-Host "停止: powershell -File scripts/stop-frontend.ps1"
