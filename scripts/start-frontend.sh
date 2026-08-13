#!/usr/bin/env bash
# 启动前端（Vite + React 开发服务器），后台运行，日志重定向到 frontend.log
# 用法: bash scripts/start-frontend.sh
# 停止: bash scripts/stop-frontend.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"
cd "$FRONTEND_DIR"

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "未发现 node_modules，先执行 npm install ..."
    npm install
fi

LOG_FILE="$REPO_ROOT/frontend.log"
PID_FILE="$REPO_ROOT/frontend.pid"

if [[ -f "$PID_FILE" ]]; then
    EXISTING_PID="$(cat "$PID_FILE")"
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "前端似乎已在运行 (PID=$EXISTING_PID)。先执行 scripts/stop-frontend.sh 再重新启动。" >&2
        exit 1
    fi
fi

# setsid 让进程成为新会话的组长（PID == PGID），停止脚本据此用负 PID 杀掉整个进程组（含 npm 派生的 vite 子进程）
setsid npm run dev > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"
disown

echo "前端已在后台启动 (PID=$PID)，日志: $LOG_FILE"
echo "停止: bash scripts/stop-frontend.sh"
