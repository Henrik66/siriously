#!/usr/bin/env bash
# Stop the llama-server that start.sh launched on PORT (default 8080). Never touches servers it did not start.
set -euo pipefail
PORT="${PORT:-8080}"
PIDFILE="$(cd "$(dirname "$0")/../.." && pwd)/logs/bonsai-server-$PORT.pid"
[ -f "$PIDFILE" ] || { echo "no pid file for port $PORT; nothing to stop"; exit 0; }
pid="$(cat "$PIDFILE")"
if ps -p "$pid" -o comm= 2>/dev/null | grep -q llama-server; then
  kill "$pid" && echo "stopped llama-server pid $pid (port $PORT)"
else
  echo "pid $pid is not a running llama-server; removing stale pid file"
fi
rm -f "$PIDFILE"
