#!/usr/bin/env bash
# Runs ON the remote box in tmux session 'trawler-queue-<remote-name>' (started
# by offload.sh enqueue; named per remote so two users can share one box).
# Priority job queue: one active job at a time at full worker count — on one
# GPU, sequential-at-full-concurrency beats parallel jobs. Each job is run via
# remote_watchdog.sh (health-gated auto-recover until every row is ok), then
# the next task starts automatically.
#
# Task files: $JOBS_DIR/queue/<job>.task
#   line1: job id   line2: extra run-bundle args   line3: endpoint url
#   lines 4+ are labeled key=value:
#     prio=N       signed int priority (enqueue -p N; missing = 0)
#     retry=N      retry count (added on first stall, incremented each time)
#     retry_at=TS  unix timestamp for the next auto-retry
#   (legacy pre-priority files had bare retry/retry_at on lines 4/5 — parsed
#    as a fallback so a deploy doesn't strand cooling jobs.)
#
# Scheduling: highest prio first; FIFO (task file mtime) within a priority
# level. The watchdog preempts the ACTIVE job (exit 3) when a strictly
# higher-prio task is waiting — the preempted task moves back to waiting with
# its prio and mtime intact, and resumes later from its committed rows.
#
# On watchdog failure: job moves to queue/cooling/ for 30min, then auto-retries
# (infinite retries — no give-up). queue/stuck/ is only for malformed tasks.
#
# Usage: remote_queue.sh <jobs_dir> <workers>
set -u
JOBS_DIR="$1"; WORKERS="$2"
QUEUE="$JOBS_DIR/queue"
COOLDOWN=1800   # 30 minutes
mkdir -p "$QUEUE/done" "$QUEUE/cooling" "$QUEUE/active"
log() { echo "[queue $(date +%H:%M:%S)] $*"; }
log "queue runner up — workers=$WORKERS, watching $QUEUE"

# labeled-line getter with legacy positional fallback (old task files carried
# bare retry / retry_at on lines 4 / 5, before priorities existed)
_meta() {  # _meta <file> <key> <legacy_line_no>
  local v; v=$(grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2)
  [ -z "$v" ] && [ -n "${3:-}" ] && v=$(sed -n "${3}p" "$1" 2>/dev/null | grep -E '^-?[0-9]+$' || true)
  echo "$v"
}
_prio() { local p; p=$(_meta "$1" prio ""); echo "${p:-0}"; }

_requeue_cooled() {
  for ct in "$QUEUE/cooling/"*.task; do
    [ -f "$ct" ] || continue
    retry_at=$(_meta "$ct" retry_at 5)
    [ -n "$retry_at" ] && [ "$(date +%s)" -ge "$retry_at" ] || continue
    job=$(sed -n 1p "$ct")
    retry=$(_meta "$ct" retry 4)
    log "cooling done for $job (retry #${retry:-1}) — moving back to queue"
    mv "$ct" "$QUEUE/"
  done
}

# highest prio wins; strict > keeps the OLDEST task at each level (FIFO)
_pick_task() {
  local t p best="" pick=""
  for t in $(ls -tr "$QUEUE"/*.task 2>/dev/null); do
    p=$(_prio "$t")
    if [ -z "$best" ] || [ "$p" -gt "$best" ]; then best=$p; pick=$t; fi
  done
  echo "$pick"
}

while :; do
  _requeue_cooled
  task=$(_pick_task)
  if [ -z "$task" ]; then sleep 30; continue; fi
  job=$(sed -n 1p "$task"); args=$(sed -n 2p "$task"); url=$(sed -n 3p "$task")
  envname=$(grep -o 'base_url_env = "[^"]*"' "$JOBS_DIR/$job/job.toml" | cut -d'"' -f2)
  if [ -z "$envname" ] || [ ! -f "$JOBS_DIR/$job/job.sqlite" ]; then
    log "task $job malformed/missing job dir — parking as stuck"
    mkdir -p "$QUEUE/stuck"
    mv "$task" "$QUEUE/stuck/"; continue
  fi
  log ">>> job $job (prio=$(_prio "$task"), workers=$WORKERS, args: $args)"
  mv "$task" "$QUEUE/active/"
  active_task="$QUEUE/active/$(basename "$task")"
  rm -f "$JOBS_DIR/$job/INTERRUPT"   # clear a stale flag from a prior interrupt-while-waiting
  rc=0
  "$JOBS_DIR/Trawler/scripts/remote_watchdog.sh" \
       "$JOBS_DIR" "$job" "$envname" "$url" --concurrency "$WORKERS" $args || rc=$?
  if [ "$rc" -eq 0 ]; then
    log "<<< job $job complete — advancing queue"
    mv "$active_task" "$QUEUE/done/"
  elif [ "$rc" -eq 2 ]; then
    # interrupted via `trawler interrupt` (INTERRUPT flag) — NOT complete, do
    # not requeue. Park in interrupted/ so it's distinct from a finished job.
    mkdir -p "$QUEUE/interrupted"
    mv "$active_task" "$QUEUE/interrupted/"
    log "<<< job $job interrupted — parked in interrupted/ (re-enqueue to resume)"
  elif [ "$rc" -eq 3 ]; then
    # preempted by a higher-priority task — back to waiting, prio + FIFO
    # position intact (mv preserves mtime). Resumes from committed rows once
    # it is the highest-priority waiter again.
    mv "$active_task" "$QUEUE/"
    log "<<< job $job preempted — requeued behind higher-priority work"
  else
    # watchdog gave up (no progress) — cool 30min then auto-retry (infinite)
    retry=$(_meta "$active_task" retry 4); retry=$(( ${retry:-0} + 1 ))
    retry_at=$(( $(date +%s) + COOLDOWN ))
    retry_hm=$(date -r "$retry_at" '+%H:%M' 2>/dev/null || date -d "@$retry_at" '+%H:%M' 2>/dev/null || echo "?")
    # rewrite task: lines 1-3 unchanged, labeled metadata refreshed
    { sed -n '1,3p' "$active_task"; echo "prio=$(_prio "$active_task")"; \
      echo "retry=$retry"; echo "retry_at=$retry_at"; } > "$active_task.tmp" \
      && mv "$active_task.tmp" "$active_task"
    mv "$active_task" "$QUEUE/cooling/"
    log "<<< job $job stalled (retry #$retry) — cooling 30min, will retry at $retry_hm"
  fi
done
