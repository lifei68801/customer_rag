#!/usr/bin/env bash
# 停止 scripts/start-backend.sh 启动的后台后端进程
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_ROOT/backend.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "未找到 backend.pid，后端可能未通过 start-backend.sh 启动。"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill -TERM -- "-$PID" 2>/dev/null || kill -TERM "$PID"
    echo "已停止后端 (PID=$PID)"
else
    echo "进程 (PID=$PID) 已不存在，跳过。"
fi

rm -f "$PID_FILE"
