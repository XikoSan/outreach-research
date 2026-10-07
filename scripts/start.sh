#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ! -x .venv/bin/python || ! -f frontend/dist/index.html ]]; then
  echo "Сначала выполните установку из README.md" >&2
  exit 1
fi
port="${PORT:-8000}"
.venv/bin/python - "$port" <<'PYTHON'
import socket,sys
with socket.socket() as s:
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try:s.bind(('127.0.0.1',int(sys.argv[1])))
    except OSError:sys.exit('Порт занят: остановите предыдущий запуск или задайте PORT=8001')
PYTHON
.venv/bin/alembic upgrade head
api_pid=''
worker_pid=''
cleanup() {
  trap - EXIT INT TERM
  [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
  [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM
.venv/bin/python -m app.worker &
worker_pid=$!
.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port "$port" &
api_pid=$!
echo "Интерфейс: http://127.0.0.1:$port • Ctrl+C останавливает сервер и worker"
wait -n "$api_pid" "$worker_pid"
