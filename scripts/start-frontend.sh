#!/usr/bin/env bash
# 启动前端（Vite + React 开发服务器），后台运行，日志重定向到 frontend.log
# 启动前会先停止已在运行的旧进程，避免端口占用或残留旧代码在跑
# 用法: bash scripts/start-frontend.sh
# 停止: bash scripts/stop-frontend.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"
cd "$FRONTEND_DIR"

bash "$REPO_ROOT/scripts/stop-frontend.sh"
# 给操作系统一点时间真正释放端口，避免新进程绑定时撞上旧进程刚退出、
# 端口还没完全放出来的极短窗口期
sleep 0.5

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "未发现 node_modules，先执行 npm install ..."
    npm install
fi

LOG_FILE="$REPO_ROOT/frontend.log"
PID_FILE="$REPO_ROOT/frontend.pid"

# setsid 让进程成为新会话的组长（PID == PGID），停止脚本据此用负 PID 杀掉整个进程组（含 npm 派生的 vite 子进程）
setsid npm run dev > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"
disown

echo "前端已在后台启动 (PID=$PID)，日志: $LOG_FILE"

# Vite 实际监听的端口可能因为默认的 5173 被占用而自动往后跳（5174、5175...），
# 所以访问地址不能硬编码，去日志里等它自己打印出的地址为准；先剥掉可能的
# ANSI 颜色控制码，再取日志里第一个 http:// 地址（就是启动横幅里的 Local 行）
ACCESS_URL=""
for _ in $(seq 1 30); do
    if [[ -f "$LOG_FILE" ]]; then
        ACCESS_URL="$(sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$LOG_FILE" 2>/dev/null | grep -oE 'http://[^[:space:]]+' | head -n1 || true)"
        if [[ -n "$ACCESS_URL" ]]; then
            break
        fi
    fi
    sleep 0.5
done
if [[ -n "$ACCESS_URL" ]]; then
    echo "访问地址: $ACCESS_URL"
else
    echo "访问地址: 暂未在日志中检测到（默认应为 http://localhost:5173），请查看 $LOG_FILE"
fi
echo "停止: bash scripts/stop-frontend.sh"
