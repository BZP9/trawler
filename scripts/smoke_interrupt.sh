#!/usr/bin/env bash
# Smoke test for the offload queue's exit-code routing + the interrupt contract.
# Runs FULLY LOCAL — no remote box, no model server, no uv/trawler. It fakes a
# job dir + a stub watchdog whose exit code we control, drives the REAL
# remote_queue.sh, and asserts the task lands in the right queue subdir with the
# right log line. This guards the interrupt feature without touching a live run.
#
#   bash scripts/smoke_interrupt.sh     # exits 0 on PASS, 1 on FAIL
#
# Contract under test (remote_queue.sh routes on the watchdog's exit code):
#   0 = complete     -> queue/done/        + log "complete"
#   1 = stalled      -> queue/cooling/     + log "stalled"
#   2 = interrupted  -> queue/interrupted/ + log "interrupted"   (via INTERRUPT flag)
#   (3 = preempted -> requeued to waiting; covered by smoke_priority.sh)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
QUEUE_SH="$HERE/remote_queue.sh"
WATCHDOG_SH="$HERE/remote_watchdog.sh"
fails=0

# --- static contract checks on the real watchdog ------------------------------
# (the watchdog can't run locally — it hard-codes ~/.local/bin/uv and curls a
# model server — so we assert its interrupt contract by inspection.)
grep -q 'INTERRUPT' "$WATCHDOG_SH" || { echo "FAIL: watchdog has no INTERRUPT flag"; fails=1; }
grep -q 'exit 2'    "$WATCHDOG_SH" || { echo "FAIL: watchdog never exits 2 (interrupt)"; fails=1; }

# --- dynamic routing test: drive the real queue with a stub watchdog ----------
run_case() {  # <name> <exit_code> <expect_subdir> <expect_log_substr>
  local name="$1" code="$2" subdir="$3" logsub="$4"
  local tmp; tmp="$(mktemp -d)"
  local jobs="$tmp/jobs" job="smoke-$name"
  mkdir -p "$jobs/Trawler/scripts" "$jobs/queue" "$jobs/$job"

  # queue only checks that job.sqlite EXISTS and that job.toml has base_url_env.
  touch "$jobs/$job/job.sqlite"
  printf 'base_url_env = "SMOKE_URL"\n' > "$jobs/$job/job.toml"

  # stub watchdog at the exact path remote_queue.sh invokes: exit with $code.
  printf '#!/usr/bin/env bash\nexit %s\n' "$code" \
    > "$jobs/Trawler/scripts/remote_watchdog.sh"
  chmod +x "$jobs/Trawler/scripts/remote_watchdog.sh"

  # task file: line1 job, line2 args, line3 url
  printf '%s\n%s\n%s\n' "$job" "" "http://x" > "$jobs/queue/$job.task"

  "$QUEUE_SH" "$jobs" 1 > "$jobs/queue.log" 2>&1 &
  local pid=$!
  # wait until the task lands in a terminal subdir (or timeout ~10s)
  local landed=""
  for _ in $(seq 1 50); do
    for d in done cooling interrupted stuck; do
      [ -f "$jobs/queue/$d/$job.task" ] && { landed="$d"; break; }
    done
    [ -n "$landed" ] && break
    sleep 0.2
  done
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null

  if [ "$landed" = "$subdir" ]; then
    echo "PASS: exit $code -> $subdir/"
  else
    echo "FAIL: exit $code expected $subdir/, landed in '${landed:-<none>}'"
    fails=1
  fi
  if ! grep -q "$logsub" "$jobs/queue.log"; then
    echo "  FAIL: queue.log missing '$logsub'"
    fails=1
  fi
  rm -rf "$tmp"
}

run_case complete  0 done        "complete"
run_case stalled   1 cooling     "stalled"
run_case interrupt 2 interrupted "interrupted"

if [ "$fails" -eq 0 ]; then
  echo "SMOKE OK"
  exit 0
fi
echo "SMOKE FAILED"
exit 1
