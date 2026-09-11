#!/usr/bin/env bash
# 启动后端（FastAPI + Uvicorn），后台运行，日志重定向到 backend.log
# 启动前会先停止已在运行的旧进程，避免端口占用或残留旧代码在跑
# 用法: bash scripts/start-backend.sh [--reload]
# 停止: bash scripts/stop-backend.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

bash "$REPO_ROOT/scripts/stop-backend.sh"
# 给操作系统一点时间真正释放端口，避免新进程绑定端口时撞上旧进程刚退出、
# 端口还没完全放出来的极短窗口期
sleep 0.5

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
PORT=8000

UVICORN_ARGS=(-m uvicorn app.main:app --host 0.0.0.0 --port "$PORT")
if [[ "$RELOAD" -eq 1 ]]; then
    UVICORN_ARGS+=(--reload)
fi

# setsid 让进程成为新会话的组长（PID == PGID），停止脚本据此用负 PID 杀掉整个进程组（含 --reload 产生的子进程）
setsid "$PYTHON" "${UVICORN_ARGS[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"
disown

# 后台拉起成功不代表 uvicorn 起来了。最常见的失败是端口被别的进程占着：
# 绑定失败、立刻退出，而脚本已经打印了"已在后台启动"——那句话是假的。
#
# 判据是"我们起的这个进程还活着"，不是"端口在监听"：绑定失败的场景里端口
# 恰恰是在监听的（被占用它的那个进程监听着），只探端口会把失败报成成功。
# 判据是"占着这个端口的进程是不是我们刚起的那个"。光问"端口在监听吗"或者
# "有 HTTP 响应吗"都不够——绑定失败的场景里端口正被**别人**占着、别人也在应答。
# 实测在 Windows 那一侧踩过：用 `python -m http.server 8000` 占住端口再跑脚本，
# HTTP 探测拿到占位进程的 404 就报了成功。探测必须回答"应答的是谁"。
port_owned_by_us() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -qx "$PID" && return 0
        # setsid 起的进程组：组内任一成员占着端口也算我们的
        for owner in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do
            [[ "$(ps -o pgid= -p "$owner" 2>/dev/null | tr -d ' ')" == "$PID" ]] && return 0
        done
    fi
    return 1
}

READY=0
for _ in $(seq 1 40); do
    if port_owned_by_us; then
        READY=1
        break
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        break   # 我们起的进程没了：多半是绑定失败
    fi
    sleep 0.5
done

if [[ "$READY" -ne 1 ]]; then
    echo "后端没能起来（等了 20 秒）。日志末尾："
    if [[ -f "$LOG_FILE" ]]; then
        tail -n 15 "$LOG_FILE" | sed 's/^/  /'
    else
        echo "  （日志文件还没生成：$LOG_FILE）"
    fi
    echo "端口 $PORT 的占用情况（若有）："
    (command -v lsof >/dev/null && lsof -iTCP:"$PORT" -sTCP:LISTEN) 2>/dev/null || echo "  查不到（没有 lsof）"
    exit 1
fi

echo "后端已在后台启动 (PID=$PID)，日志: $LOG_FILE"
echo "访问地址: http://localhost:$PORT  (API 文档: http://localhost:$PORT/docs)"
echo "停止: bash scripts/stop-backend.sh"
