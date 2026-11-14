#!/usr/bin/env bash
# One-command demo launcher for the Mac (PRD §1 demo).
#
#   scripts/run_demo.sh            # = start: Bonsai :8080 + Argmax STT :50060 (start_all.sh), then the brain :8765
#   scripts/run_demo.sh status     # health of everything
#   scripts/run_demo.sh stop       # stops only what this script started
#
# start: reuses an already-healthy stack, starts the brain with the real Mac provider (--mac),
# waits for /health with bonsai:true and for the "planner warm" log line, then prints the console
# URL, the rehearsal command and an OFFLINE check (exit 3 if the internet is reachable: wifi is on).
# Logs and pids live in logs/ (gitignored). Env: BRAIN_PORT (8765), BRAIN_ARGS (extra args, e.g. --no-model),
# WAIT_SECONDS (180).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$REPO/logs"
STATE="$LOGS/run_demo.state"
BRAIN_PORT="${BRAIN_PORT:-8765}"
BRAIN_URL="http://127.0.0.1:$BRAIN_PORT"
BRAIN_LOG="$LOGS/brain.log"
BRAIN_PID="$LOGS/brain.pid"
WAIT_SECONDS="${WAIT_SECONDS:-180}"
mkdir -p "$LOGS"

say() { printf '%s\n' "$*"; }
die() { printf 'run_demo: %s\n' "$*" >&2; exit 1; }

started() { [ -f "$STATE" ] && grep -qx "$1" "$STATE"; }
mark() { started "$1" || printf '%s\n' "$1" >>"$STATE"; }

brain_health() { curl -s --noproxy '*' -m 2 "$BRAIN_URL/health" 2>/dev/null || true; }

wait_for() {  # description, seconds, command...
  local what="$1" secs="$2" i
  shift 2
  for ((i = 0; i < secs; i++)); do
    if "$@"; then say "  ok: $what (${i}s)"; return 0; fi
    sleep 1
  done
  say "  FAILED: $what not ready after ${secs}s"
  return 1
}

bonsai_ready() { brain_health | grep -q '"bonsai":true'; }
warm_logged() { grep -q "planner warm" "$BRAIN_LOG" 2>/dev/null; }
warm_failed() { grep -q "planner warmup failed" "$BRAIN_LOG" 2>/dev/null; }
warm_done() { warm_logged || warm_failed; }

start_stack() {
  say "== Bonsai + Argmax (start_all.sh)"
  if "$REPO/start_all.sh" status >/dev/null 2>&1; then
    say "  already healthy: reusing (run_demo stop will not stop it)"
    return 0
  fi
  "$REPO/start_all.sh" start || die "start_all.sh failed; see logs/run_*.log (or './start_all.sh stop' and retry)"
  mark stack
}

start_brain() {
  say "== Brain on $BRAIN_URL"
  if [ -n "$(brain_health)" ]; then
    say "  already running: reusing (run_demo stop will not stop it)"
    return 0
  fi
  # bash 3.2 (macOS /bin/bash) safe: no mapfile, no empty-array expansion under set -u.
  # The venv's python directly (not `uv run`), so the pid file is the server itself.
  if [ -x "$REPO/.venv/bin/python" ]; then
    set -- "$REPO/.venv/bin/python" -m brain.server
  else
    set -- uv run --offline siriously-brain
  fi
  # shellcheck disable=SC2086  # BRAIN_ARGS is intentionally word-split (e.g. "--no-model --log-level debug")
  set -- "$@" --mac --port "$BRAIN_PORT" ${BRAIN_ARGS:-}
  # cd first, then background a simple command: $! is then nohup's pid, which execs the server.
  (
    cd "$REPO" || exit 1
    nohup "$@" >"$BRAIN_LOG" 2>&1 &
    printf '%s\n' "$!" >"$BRAIN_PID"
  )
  mark brain
  say "  started pid $(cat "$BRAIN_PID"), log $BRAIN_LOG"
  wait_for "brain /health" "$WAIT_SECONDS" sh -c "curl -sf --noproxy '*' -m 2 '$BRAIN_URL/health' >/dev/null" \
    || { tail -n 20 "$BRAIN_LOG"; die "brain did not come up"; }
  wait_for "brain sees Bonsai (bonsai:true)" "$WAIT_SECONDS" bonsai_ready || die "brain cannot reach Bonsai :8080"
  wait_for "planner warmup finished" "$WAIT_SECONDS" warm_done || say "  WARNING: no 'planner warm' line yet"
  if warm_failed && ! warm_logged; then
    say "  WARNING: planner warmup failed (see $BRAIN_LOG); first planner command may be slow"
  else
    say "  $(grep -m1 -o 'planner warm ([0-9]* ms)' "$BRAIN_LOG" || true)"
  fi
}

offline_check() {
  say "== Offline check"
  if curl -s -o /dev/null -m 3 https://example.com; then
    say ""
    say "  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    say "  !!  WIFI IS ON: https://example.com is reachable.           !!"
    say "  !!  The demo must run offline. Turn wifi off and re-check:  !!"
    say "  !!      scripts/run_demo.sh status                          !!"
    say "  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    return 3
  fi
  say "  ok: internet unreachable (offline)"
}

banner() {
  say ""
  say "Console:    $BRAIN_URL/console"
  say "Rehearse:   uv run python scripts/demo/rehearse.py --live"
  say "Events:     uv run python -m brain.cli --watch --history 20"
  say "Stop:       scripts/run_demo.sh stop"
}

do_start() {
  start_stack
  start_brain
  banner
  offline_check
}

do_status() {
  local rc=0
  say "== Stack"
  "$REPO/start_all.sh" status || rc=1
  say "== Brain"
  local h
  h="$(brain_health)"
  if [ -n "$h" ]; then say "  $BRAIN_URL/health -> $h"; else say "  brain not responding on $BRAIN_URL"; rc=1; fi
  if [ -n "$h" ] && ! printf '%s' "$h" | grep -q '"bonsai":true'; then rc=1; fi
  offline_check || rc=$?
  return "$rc"
}

do_stop() {
  if started brain && [ -f "$BRAIN_PID" ]; then
    local pid
    pid="$(cat "$BRAIN_PID")"
    if kill "$pid" 2>/dev/null; then say "stopped brain (pid $pid)"; else say "brain pid $pid already gone"; fi
    rm -f "$BRAIN_PID"
  else
    say "brain: not started by run_demo; leaving it"
  fi
  if started stack; then
    "$REPO/start_all.sh" stop
  else
    say "stack: not started by run_demo; leaving it"
  fi
  rm -f "$STATE"
}

case "${1:-start}" in
  start) do_start ;;
  status) do_status ;;
  stop) do_stop ;;
  *) die "usage: $0 [start|status|stop]" ;;
esac
