#!/usr/bin/env bash
# 启动后端（FastAPI + Uvicorn），后台运行，日志重定向到 backend.log
# 用法: bash scripts/start-backend.sh [--reload]
# 停止: bash scripts/stop-backend.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RELOAD=0
if [[ "${1:-}" == "--reload" ]]; then
    RELOAD=1
fi

PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "找不到虚拟环境: $PYTHON 。请先创建 .venv 并安装依赖。" >&2
    exit 1
fi

LOG_FILE="$REPO_ROOT/backend.log"
PID_FILE="$REPO_ROOT/backend.pid"

if [[ -f "$PID_FILE" ]]; then
    EXISTING_PID="$(cat "$PID_FILE")"
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "后端似乎已在运行 (PID=$EXISTING_PID)。先执行 scripts/stop-backend.sh 再重新启动。" >&2
        exit 1
    fi
fi

UVICORN_ARGS=(-m uvicorn app.main:app --host 0.0.0.0 --port 8000)
if [[ "$RELOAD" -eq 1 ]]; then
    UVICORN_ARGS+=(--reload)
fi

# setsid 让进程成为新会话的组长（PID == PGID），停止脚本据此用负 PID 杀掉整个进程组（含 --reload 产生的子进程）
setsid "$PYTHON" "${UVICORN_ARGS[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"
disown

echo "后端已在后台启动 (PID=$PID)，日志: $LOG_FILE"
echo "停止: bash scripts/stop-backend.sh"
