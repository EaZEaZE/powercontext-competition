#!/usr/bin/env bash
# 启动已配置的 Dify 1.10.1 源码服务与嵌入式 seekdb 桥接。
# 前置：PostgreSQL/Redis 已运行，Dify API/Web 配置已完成，Sandbox 已构建。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/.env"

: "${DIFY_SRC:?请在 .env 中设置 DIFY_SRC}"
: "${DIFY_SANDBOX_DIR:?请在 .env 中设置 DIFY_SANDBOX_DIR}"
: "${SEEKDB_DIR:?请在 .env 中设置 SEEKDB_DIR}"

DIFY_API_HOST="${DIFY_API_HOST:-0.0.0.0}"
DIFY_API_PORT="${DIFY_API_PORT:-5001}"
DIFY_WEB_PORT="${DIFY_WEB_PORT:-3000}"
SEEKDB_DATABASE="${SEEKDB_DATABASE:-test}"
SEEKDB_BRIDGE_PORT="${SEEKDB_BRIDGE_PORT:-18080}"
UV_BIN="${UV_BIN:-uv}"
PNPM_BIN="${PNPM_BIN:-pnpm}"
RUNTIME_DIR="${RUNTIME_DIR:-$ROOT_DIR/.runtime}"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR" "$SEEKDB_DIR"

start_service() {
    local name="$1"
    local workdir="$2"
    shift 2
    local pidfile="$PID_DIR/$name.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "[$name] 已运行，PID $(cat "$pidfile")"
        return
    fi
    (
        cd "$workdir"
        nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
        echo $! > "$pidfile"
    )
    echo "[$name] 已启动，日志 $LOG_DIR/$name.log"
}

[[ -f "$DIFY_SRC/api/.env" ]] || { echo "缺少 $DIFY_SRC/api/.env"; exit 1; }
[[ -f "$DIFY_SRC/web/.env.local" ]] || { echo "缺少 $DIFY_SRC/web/.env.local"; exit 1; }
[[ -x "$DIFY_SANDBOX_DIR/main" ]] || { echo "缺少可执行文件 $DIFY_SANDBOX_DIR/main"; exit 1; }

# 嵌入式 seekdb 桥接
BRIDGE_VENV="$ROOT_DIR/bridge/.venv"
if [[ ! -x "$BRIDGE_VENV/bin/python" ]]; then
    python3 -m venv "$BRIDGE_VENV"
fi
"$BRIDGE_VENV/bin/pip" install -r "$ROOT_DIR/bridge/requirements.txt"
start_service seekdb-bridge "$ROOT_DIR/bridge" \
    env SEEKDB_DIR="$SEEKDB_DIR" SEEKDB_DATABASE="$SEEKDB_DATABASE" PORT="$SEEKDB_BRIDGE_PORT" \
    "$BRIDGE_VENV/bin/python" "$ROOT_DIR/bridge/seekdb_bridge.py"

# Dify Sandbox、API、Worker、Web
start_service dify-sandbox "$DIFY_SANDBOX_DIR" "$DIFY_SANDBOX_DIR/main"

if [[ ! -d "$DIFY_SRC/api/.venv" ]]; then
    (cd "$DIFY_SRC/api" && "$UV_BIN" sync)
fi
(cd "$DIFY_SRC/api" && "$UV_BIN" run flask db upgrade)
start_service dify-api "$DIFY_SRC/api" \
    "$UV_BIN" run flask run --host "$DIFY_API_HOST" --port "$DIFY_API_PORT"
start_service dify-worker "$DIFY_SRC/api" \
    "$UV_BIN" run celery -A app.celery worker -P gevent -c 4 --loglevel INFO

if [[ ! -d "$DIFY_SRC/web/node_modules" ]]; then
    (cd "$DIFY_SRC/web" && "$PNPM_BIN" install)
fi
start_service dify-web "$DIFY_SRC/web" \
    "$PNPM_BIN" exec next dev -H "$DIFY_API_HOST" -p "$DIFY_WEB_PORT"

echo
echo "Dify Web: http://127.0.0.1:$DIFY_WEB_PORT"
echo "Dify API: http://127.0.0.1:$DIFY_API_PORT"
echo "seekdb bridge: http://127.0.0.1:$SEEKDB_BRIDGE_PORT/health"
echo "导入 DSL: $ROOT_DIR/dsl/oceanbase-sql-helper.yml"
