#!/usr/bin/env bash
# 停止 scripts/start-frontend.sh 启动的后台前端进程
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_ROOT/frontend.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "未找到 frontend.pid，前端可能未通过 start-frontend.sh 启动。"
    exit 0
fi

PID="$(cat "$PID_FILE")"

# Git Bash/MSYS 下，bash 内置的 kill -0 只认得 MSYS 自己进程树里的 PID，
# 检测不到由原生 Windows 方式（比如 start-frontend.ps1 经 WMI）拉起的进程——
# 明明还活着也会报 "No such process"，实测过。tasklist/taskkill 直接查
# Windows 内核的 PID 表，不管进程是被谁创建的都认得出来，所以只要这两个
# 命令存在（即运行在 Windows 上）就优先用它们；真正的纯 POSIX 环境
# （Linux/macOS，没有 tasklist/taskkill）再退回原来的 kill -0/-TERM 方案。
if command -v tasklist >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
    if tasklist //FI "PID eq $PID" //NH 2>/dev/null | grep -q "$PID"; then
        taskkill //PID "$PID" //T //F >/dev/null 2>&1
        echo "已停止前端 (PID=$PID)"
    else
        echo "进程 (PID=$PID) 已不存在，跳过。"
    fi
else
    if kill -0 "$PID" 2>/dev/null; then
        kill -TERM -- "-$PID" 2>/dev/null || kill -TERM "$PID"
        echo "已停止前端 (PID=$PID)"
    else
        echo "进程 (PID=$PID) 已不存在，跳过。"
    fi
fi

rm -f "$PID_FILE"
