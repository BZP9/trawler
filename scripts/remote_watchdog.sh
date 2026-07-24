#!/usr/bin/env bash
# Runs ON the remote box (inside tmux, started by offload.sh run).
# Keeps a run-bundle job alive until every row is done:
#   - checks every 60s; if the run died (crash, early-stop, reboot) it
#     waits for the model server to be healthy, then relaunches (resumable)
#   - prints a status line every minute — `tmux attach -t trawler-<job>` to watch
#   - honors an INTERRUPT flag ($JOBS_DIR/$JOB/INTERRUPT, dropped by
#     `offload.sh interrupt`): interrupts the current pass and exits 2 (no
#     relaunch, no requeue) — the ok rows already committed stay; the queue
#     parks the job in interrupted/; re-enqueue/import resumes from them.
#   - self-preempts (exit 3) when a strictly higher-prio task is waiting in
#     the queue: checked once per minute; run-bundle commits per row, so at
#     most the in-flight rows are lost and rerun on resume. Only applies when
#     running under the queue (active task file exists); direct `offload.sh
#     run` jobs are never preempted.
#
# Exit codes: 0 = complete (all rows ok); 1 = stalled (no progress);
#             2 = interrupted (INTERRUPT flag); 3 = preempted (higher-prio
#             task waiting). The queue routes on these.
#
# Usage: remote_watchdog.sh <jobs_dir> <job_id> <env_name> <url> [run-bundle args...]
set -u

JOBS_DIR="$1"; JOB="$2"; ENVNAME="$3"; URL="$4"; shift 4
DB="$JOBS_DIR/$JOB/job.sqlite"
INTERRUPT="$JOBS_DIR/$JOB/INTERRUPT"
QUEUE="$JOBS_DIR/queue"
MY_TASK="$QUEUE/active/$JOB.task"
TOTAL=$(sqlite3 "$DB" "SELECT value FROM job_meta WHERE key='row_count'")

log() { echo "[watchdog $(date +%H:%M:%S)] $*"; }

# Kill the current pass INCLUDING the real run-bundle processes. $RUN_PID
# ($! of a background pipeline) is only the LAST pipeline stage (the log
# reader); `wait` on it waits for the WHOLE job, so killing just $RUN_PID
# leaves uv/python run-bundle alive and the wait hangs forever (bug found
# live 2026-07-13: preemption froze the queue mid-pass). Kill by cmdline
# match on the job id — same pattern offload.sh interrupt uses externally.
_kill_pass() {
  pkill -f "run-bundle.*$JOB" 2>/dev/null || true
  kill "$RUN_PID" 2>/dev/null || true
}

# true when a strictly higher-prio task waits in the queue (prio line
# missing = 0, matching remote_queue.sh). No active task file = not running
# under the queue = never preempt.
_preempted() {
  [ -f "$MY_TASK" ] || return 1
  local my p t
  my=$(grep -m1 '^prio=' "$MY_TASK" 2>/dev/null | cut -d= -f2); my=${my:-0}
  for t in "$QUEUE"/*.task; do
    [ -f "$t" ] || continue
    p=$(grep -m1 '^prio=' "$t" 2>/dev/null | cut -d= -f2); p=${p:-0}
    [ "$p" -gt "$my" ] && return 0
  done
  return 1
}

prev_ok=-1
stale_passes=0
while :; do
  # graceful interrupt: consume the flag and exit 2 (distinct from 0=complete
  # and 1=stalled). The queue archives an exit-2 job to interrupted/ + logs it
  # as "interrupted" — NOT "complete" — and does not requeue it. Partial kept.
  if [ -f "$INTERRUPT" ]; then
    rm -f "$INTERRUPT"
    log "INTERRUPT requested — stopping (partial results kept, re-enqueue to resume)"
    exit 2
  fi
  if _preempted; then
    log "PREEMPTED — higher-priority task waiting; stopping (partial kept, auto-requeued)"
    exit 3
  fi
  ok=$(sqlite3 "$DB" "SELECT count(*) FROM results WHERE status='ok'" 2>/dev/null || echo 0)
  if [ "$ok" -ge "$TOTAL" ]; then
    log "COMPLETE $ok/$TOTAL — watchdog exiting"; exit 0
  fi
  # no-progress breaker: two consecutive full passes adding zero ok rows
  # means the remaining rows fail permanently — stop instead of burning
  # GPU on them forever. Inspect fails, fix, re-run to resume.
  if [ "$ok" -eq "$prev_ok" ]; then
    stale_passes=$((stale_passes + 1))
    if [ "$stale_passes" -ge 2 ]; then
      fail=$(sqlite3 "$DB" "SELECT count(*) FROM results WHERE status='fail'" 2>/dev/null || echo '?')
      log "NO PROGRESS across $stale_passes passes ($ok/$TOTAL ok, $fail permanently failing) — watchdog exiting; inspect fails and re-run"
      exit 1
    fi
  else
    stale_passes=0
  fi
  prev_ok=$ok
  # gate on server health so we don't burn the early-stop budget on a dead server
  until curl -s -m 4 "$URL/models" >/dev/null 2>&1; do
    log "server at $URL not healthy — waiting 30s"; sleep 30
  done
  log "starting run-bundle ($ok/$TOTAL ok so far)"
  env "$ENVNAME=$URL" ~/.local/bin/uv --project "$JOBS_DIR/Trawler" \
    run trawler run-bundle "$JOBS_DIR/$JOB" "$@" 2>&1 | while IFS= read -r l; do
      echo "$l"
    done &
  RUN_PID=$!
  # per-minute status while the run lives
  while kill -0 "$RUN_PID" 2>/dev/null; do
    if [ -f "$INTERRUPT" ]; then
      log "INTERRUPT requested — terminating current pass"
      _kill_pass
      break   # outer-loop top consumes the flag and exits 2
    fi
    if _preempted; then
      log "PREEMPTED — higher-priority task waiting; terminating current pass"
      _kill_pass
      wait "$RUN_PID" 2>/dev/null
      exit 3   # queue requeues the task (waiting, prio intact); committed rows survive
    fi
    sleep 60
    ok=$(sqlite3 "$DB" "SELECT count(*) FROM results WHERE status='ok'" 2>/dev/null || echo '?')
    fail=$(sqlite3 "$DB" "SELECT count(*) FROM results WHERE status='fail'" 2>/dev/null || echo '?')
    log "status $ok/$TOTAL ok, $fail fail"
  done
  wait "$RUN_PID" 2>/dev/null
  log "run-bundle exited — recheck in 10s (auto-recover if incomplete)"
  sleep 10
done
