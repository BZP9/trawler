#!/usr/bin/env bash
# One-shot status of a trawler offload run on a named remote GPU box.
# Config: Trawler/.env (see .env.example) — named remotes via TRAWLER_REMOTES.
#
# Usage: remote_status.sh [-r remote-name] [job-id]
#        default remote = first in TRAWLER_REMOTES; default job = newest on remote
set -euo pipefail

source "$(dirname "$0")/remote_env.sh"

NAME=""
if [ "${1:-}" = "--all" ]; then           # sweep every named remote, every job dir
  IFS=',' read -ra _names <<< "${TRAWLER_REMOTES:-}"
  for n in "${_names[@]}"; do
    resolve_remote "$n" || continue
    for j in $(ssh "$REMOTE_SSH" "ls -d $REMOTE_JOBS/*/ 2>/dev/null | xargs -n1 basename" 2>/dev/null | grep -v '^Trawler$'); do
      "$0" -r "$n" "$j" | grep -E "^(remote|job|progress|process|eta):"
      echo
    done
  done
  exit 0
fi
if [ "${1:-}" = "-r" ]; then NAME="$2"; shift 2; fi
resolve_remote "$NAME"

JOB="${1:-}"
if [ -z "$JOB" ]; then
  JOB=$(ssh "$REMOTE_SSH" "ls -td $REMOTE_JOBS/*/ 2>/dev/null | xargs -n1 basename 2>/dev/null | grep -v '^Trawler\$' | head -1" 2>/dev/null)
  [ -z "$JOB" ] && { echo "no job dirs on remote '$REMOTE_NAME'"; exit 1; }
fi

echo "remote:   $REMOTE_NAME ($REMOTE_SSH)"
# one ssh round-trip: DATA line for parsing + display lines
OUT=$(ssh "$REMOTE_SSH" "
  DB=$REMOTE_JOBS/$JOB/job.sqlite
  [ -f \$DB ] || { echo 'no job.sqlite for $JOB'; exit 1; }
  ok=\$(sqlite3 \$DB \"SELECT count(*) FROM results WHERE status='ok'\")
  fail=\$(sqlite3 \$DB \"SELECT count(*) FROM results WHERE status='fail'\")
  total=\$(sqlite3 \$DB \"SELECT value FROM job_meta WHERE key='row_count'\")
  pid=\$(pgrep -f \"run-bundle.*$JOB\" | head -1)   # match THIS job only — boxes can run several
  parked=\$([ -f $REMOTE_JOBS/queue/interrupted/$JOB.task ] && echo yes)
  # rows/min over the last 10 completed rows (count*60 / span from oldest-in-window to now)
  rate=\$(sqlite3 \$DB \"SELECT printf('%.1f', count(*)*60.0/max(60,(strftime('%s','now')-strftime('%s',min(updated_at)))))
                         FROM (SELECT updated_at FROM results WHERE status='ok' ORDER BY updated_at DESC LIMIT 10)\")
  # status-model: live process wins; else parked in queue/interrupted/ →
  # interrupted; else leave stage as-is (sync_job_row won't touch status).
  if [ -n \"\$pid\" ]; then st=running; elif [ -n \"\$parked\" ]; then st=interrupted; else st=; fi
  echo \"DATA \$ok \$fail \$st\"
  pct=\$(awk -v o=\$ok -v t=\$total 'BEGIN{printf \"%.1f\", t? o*100/t:0}')
  echo \"job:      $JOB\"
  echo \"progress: \$ok/\$total ok (\$pct%), \$fail fail\"
  if [ -n \"\$pid\" ]; then echo \"process:  RUNNING (pid \$pid, ~\$rate rows/min)\"; else echo 'process:  NOT RUNNING'; fi
  if [ -n \"\$pid\" ] && [ \"\$rate\" != \"0.0\" ] && [ \"\$ok\" -lt \"\$total\" ]; then
    left=\$(( total - ok ))
    eta_min=\$(echo \"\$left \$rate\" | awk '{printf \"%.0f\", \$1/\$2}')
    if [ \"\$eta_min\" -ge 60 ]; then eta=\"\$(( eta_min/60 ))h \$(( eta_min%60 ))m\"; else eta=\"\${eta_min}m\"; fi
    echo \"eta:      ~\$eta (\$left rows left)\"
  fi
  echo ''
  echo 'log:'
  # prefer queue.log (active session) if it references this job; fall back to per-job log
  if grep -q \"$JOB\" $REMOTE_JOBS/queue.log 2>/dev/null; then
    grep -E \"$JOB|watchdog|run-bundle\" $REMOTE_JOBS/queue.log 2>/dev/null | tail -3 | sed 's/^/  /'
  else
    tail -3 $REMOTE_JOBS/$JOB.log 2>/dev/null | sed 's/^/  /'
  fi
")
echo "$OUT" | grep -v '^DATA '
# write what we just observed back to the control plane (gen._gen_log)
read -r _ ok fail state <<< "$(echo "$OUT" | grep '^DATA ' | head -1)"
sync_job_row "$JOB" "$ok" "$fail" "${state:-stalled}"

# queue summary — always show so the whole pipeline is visible
ssh "$REMOTE_SSH" "
  QUEUE=$REMOTE_JOBS/queue
  echo ''
  waiting=\$(ls \"\$QUEUE\"/*.task 2>/dev/null | wc -l | tr -d ' ')
  done_c=\$(ls \"\$QUEUE/done\" 2>/dev/null | wc -l | tr -d ' ')
  cooling_c=\$(ls \"\$QUEUE/cooling\" 2>/dev/null | wc -l | tr -d ' ')
  TMUX_BIN=\$(command -v tmux || ls /opt/homebrew/bin/tmux 2>/dev/null || true)
  runner=\$({ [ -n \"\$TMUX_BIN\" ] && \$TMUX_BIN has-session -t trawler-queue-$REMOTE_NAME 2>/dev/null && echo 'UP'; } || echo 'DOWN')
  if [ \"\$runner\" = 'UP' ] && ! pgrep -f \"remote_queue.sh $REMOTE_JOBS \" >/dev/null 2>&1; then
    runner='UP(NAME-COLLISION)'
  fi
  active_c=\$(ls \"\$QUEUE/active\" 2>/dev/null | wc -l | tr -d ' ')
  echo \"queue:    runner=\$runner  active=\$active_c  waiting=\$waiting  cooling=\$cooling_c  done=\$done_c\"
  while IFS= read -r at; do
    jb=\$(sed -n 1p \"\$at\"); echo \"  active:  \$jb (RUNNING)\"
  done < <(find \"\$QUEUE/active\" -name '*.task' 2>/dev/null)
  while IFS= read -r ct; do
    [ -f \"\$ct\" ] || continue
    jb=\$(sed -n 1p \"\$ct\"); retry=\$(sed -n 4p \"\$ct\"); retry_at=\$(sed -n 5p \"\$ct\")
    now=\$(date +%s); left=\$(( retry_at - now ))
    [ \$left -lt 0 ] && left=0
    echo \"  cooling: \$jb retry=#\$retry retry_in=\$(( left/60 ))min\"
  done < <(find \"\$QUEUE/cooling\" -name '*.task' 2>/dev/null)
  for wt in \"\$QUEUE/\"*.task; do
    [ -f \"\$wt\" ] || continue
    jb=\$(sed -n 1p \"\$wt\"); retry=\$(sed -n 4p \"\$wt\")
    echo \"  waiting: \$jb\${retry:+ (retry #\$retry)}\"
  done" 2>/dev/null || true
