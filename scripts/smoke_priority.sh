#!/usr/bin/env bash
# Smoke test for the queue's priority scheduling + preemption routing.
# Runs FULLY LOCAL — no remote box, no model server. Like smoke_interrupt.sh
# it drives the REAL remote_queue.sh with a stub watchdog and asserts:
#
#   A. selection order: highest prio first, legacy no-prio task = prio 0,
#      FIFO otherwise (prio=10 runs before legacy before prio=-1)
#   B. exit 3 (preempted) -> task requeued to waiting (NOT interrupted/),
#      then re-run to completion
#   C. (by inspection) the real watchdog implements the preempt contract:
#      _preempted check + exit 3; the queue routes rc=3
#
#   bash scripts/smoke_priority.sh     # exits 0 on PASS, 1 on FAIL
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
QUEUE_SH="$HERE/remote_queue.sh"
WATCHDOG_SH="$HERE/remote_watchdog.sh"
fails=0

# --- C. static contract checks on the real scripts -----------------------------
grep -q '_preempted' "$WATCHDOG_SH" || { echo "FAIL: watchdog has no _preempted check"; fails=1; }
grep -q 'exit 3'     "$WATCHDOG_SH" || { echo "FAIL: watchdog never exits 3 (preempt)"; fails=1; }
# killing only $RUN_PID (last pipeline stage) leaves run-bundle alive and the
# subsequent `wait` hangs the queue forever — the kill must match run-bundle.
grep -q 'pkill -f "run-bundle' "$WATCHDOG_SH" || { echo "FAIL: watchdog kill misses run-bundle children (RUN_PID is only the logger)"; fails=1; }
grep -q 'rc" -eq 3'  "$QUEUE_SH"    || { echo "FAIL: queue does not route exit 3"; fails=1; }

_mkjob() {  # <jobs_dir> <job>
  mkdir -p "$1/$2"
  touch "$1/$2/job.sqlite"
  printf 'base_url_env = "SMOKE_URL"\n' > "$1/$2/job.toml"
}

# --- A. priority selection order ------------------------------------------------
tmp="$(mktemp -d)"; jobs="$tmp/jobs"
mkdir -p "$jobs/Trawler/scripts" "$jobs/queue"
for j in smoke-low smoke-mid smoke-high; do _mkjob "$jobs" "$j"; done
# stub watchdog records each run's job id, completes instantly
printf '#!/usr/bin/env bash\necho "$2" >> "$1/run_order"\nexit 0\n' \
  > "$jobs/Trawler/scripts/remote_watchdog.sh"
chmod +x "$jobs/Trawler/scripts/remote_watchdog.sh"
# FIFO mtimes: low is OLDEST — pure FIFO would run it first; prio must win.
# smoke-mid has NO prio line (legacy format) and must behave as prio=0.
printf '%s\n%s\n%s\nprio=-1\n' smoke-low  "" "http://x" > "$jobs/queue/smoke-low.task"
printf '%s\n%s\n%s\n'          smoke-mid  "" "http://x" > "$jobs/queue/smoke-mid.task"
printf '%s\n%s\n%s\nprio=10\n' smoke-high "" "http://x" > "$jobs/queue/smoke-high.task"
touch -t 202601010000 "$jobs/queue/smoke-low.task"
touch -t 202601010001 "$jobs/queue/smoke-mid.task"
touch -t 202601010002 "$jobs/queue/smoke-high.task"

"$QUEUE_SH" "$jobs" 1 > "$jobs/queue.log" 2>&1 &
pid=$!
for _ in $(seq 1 50); do
  [ "$(ls "$jobs/queue/done" 2>/dev/null | wc -l)" -ge 3 ] && break
  sleep 0.2
done
kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null

order=$(tr '\n' ' ' < "$jobs/run_order" 2>/dev/null | sed 's/ $//')
if [ "$order" = "smoke-high smoke-mid smoke-low" ]; then
  echo "PASS: priority order (10 > legacy/0 > -1, beats FIFO)"
else
  echo "FAIL: expected 'smoke-high smoke-mid smoke-low', got '$order'"
  fails=1
fi
rm -rf "$tmp"

# --- B. exit 3 -> requeue -> completes on re-run ---------------------------------
tmp="$(mktemp -d)"; jobs="$tmp/jobs"
mkdir -p "$jobs/Trawler/scripts" "$jobs/queue"
_mkjob "$jobs" smoke-pre
# stub: first run reports preempted (exit 3), second run completes
printf '#!/usr/bin/env bash\nif [ ! -f "$1/ran_once" ]; then touch "$1/ran_once"; exit 3; fi\nexit 0\n' \
  > "$jobs/Trawler/scripts/remote_watchdog.sh"
chmod +x "$jobs/Trawler/scripts/remote_watchdog.sh"
printf '%s\n%s\n%s\nprio=5\n' smoke-pre "" "http://x" > "$jobs/queue/smoke-pre.task"

"$QUEUE_SH" "$jobs" 1 > "$jobs/queue.log" 2>&1 &
pid=$!
for _ in $(seq 1 50); do
  [ -f "$jobs/queue/done/smoke-pre.task" ] && break
  sleep 0.2
done
kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null

if [ -f "$jobs/queue/done/smoke-pre.task" ] && grep -q "preempted — requeued" "$jobs/queue.log"; then
  echo "PASS: exit 3 -> requeued (log says preempted) -> completed on re-run"
else
  echo "FAIL: exit-3 routing (done? $(ls "$jobs/queue/done" 2>/dev/null); log:)"
  tail -5 "$jobs/queue.log" | sed 's/^/  /'
  fails=1
fi
if [ -f "$jobs/queue/interrupted/smoke-pre.task" ]; then
  echo "FAIL: preempted task wrongly parked in interrupted/"
  fails=1
fi
rm -rf "$tmp"

if [ "$fails" -eq 0 ]; then echo "SMOKE OK"; exit 0; fi
echo "SMOKE FAILED"; exit 1
