#!/usr/bin/env bash
# Drive the offload loop against a named remote (see .env.example).
#
#   offload.sh push   [-r name] <job-id> [--with-repo]   # rsync job dir (and optionally trawler repo) over
#   offload.sh run    [-r name] <job-id> [run-bundle args...]   # start run-bundle detached (nohup)
#   offload.sh status [-r name] [job-id]                 # same as remote_status.sh
#   offload.sh pull   [-r name] <job-id>                 # rsync job.sqlite back
#   offload.sh import [-r name] <job-id>                 # pull + trawler import
#   offload.sh enqueue [-r name] <job-id> [-p N] [run-bundle args...]  # push + queue (prio N, default 0, higher first; preempts lower)
#   offload.sh interrupt [-r name] <job-id>              # interrupt a running/queued job (partial kept, resumable)
#   offload.sh queue  [-r name]                          # queue state: active job, waiting, done
#   offload.sh clean  [-r name] <job-id>                 # rm the remote's copy of a job dir (+ its queue tasks)
#   offload.sh models [-r name]                          # list model files + running servers on the box
#   offload.sh fetch-model [-r name] <hf-repo> <file>    # resumable download into the box's _MODELS dir
#
# Job dirs live locally in <repo>/output/jobs/<job-id>.
# Model dir per box: TRAWLER_REMOTE_<NAME>_MODELS (default ~/models).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
source "$HERE/remote_env.sh"

# stamp remote + stage into the job's gen._gen_log row (registered by bundle)
_stamp() {  # _stamp <job-id> <stage>
  psql "${TRAWLER_DSN:-$ROWINFER_DSN}" -q -c \
    "UPDATE gen._gen_log SET config = config || jsonb_build_object('remote', '$REMOTE_NAME', 'stage', '$2')
     WHERE config->>'job_id' = '$1' AND status = 'exported'" 2>/dev/null || true
}

CMD="${1:-}"; shift || true
NAME=""
if [ "${1:-}" = "-r" ]; then NAME="$2"; shift 2; fi
resolve_remote "$NAME"

JOB="${1:-}"; shift || true
LOCAL_JOB="$ROOT/output/jobs/$JOB"

case "$CMD" in
  push)
    [ -d "$LOCAL_JOB" ] || { echo "no local job dir $LOCAL_JOB" >&2; exit 1; }
    if [ "${1:-}" = "--with-repo" ]; then
      echo "[push] trawler repo -> $REMOTE_NAME"
      rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude output --exclude .env \
        "$ROOT/" "$REMOTE_SSH:$REMOTE_JOBS/Trawler/"
      ssh "$REMOTE_SSH" "cd $REMOTE_JOBS/Trawler && (command -v uv || echo ~/.local/bin/uv) >/dev/null; ~/.local/bin/uv sync -q || uv sync -q"
    fi
    echo "[push] $JOB -> $REMOTE_NAME"
    # job.sqlite is REMOTE-owned once the job has run there (it accumulates
    # results); overwriting it wipes progress. Ship it only if absent.
    rsync -a --exclude job.sqlite "$LOCAL_JOB/" "$REMOTE_SSH:$REMOTE_JOBS/$JOB/"
    rsync -a --ignore-existing "$LOCAL_JOB/job.sqlite" "$REMOTE_SSH:$REMOTE_JOBS/$JOB/"
    _stamp "$JOB" "pushed"
    ;;
  run)
    echo "[run] $JOB on $REMOTE_NAME (endpoint $REMOTE_URL) — extra args: $*"
    # Prefer tmux + watchdog: survives ssh drops, auto-recovers crashed runs,
    # prints status per minute (attach: tmux attach -t trawler-<job>).
    # Falls back to bare nohup when tmux is absent.
    scp -q "$HERE/remote_watchdog.sh" "$REMOTE_SSH:$REMOTE_JOBS/Trawler/scripts/remote_watchdog.sh" 2>/dev/null || true
    ssh "$REMOTE_SSH" "
      envname=\$(grep -o 'base_url_env = \"[^\"]*\"' $REMOTE_JOBS/$JOB/job.toml | cut -d'\"' -f2)
      [ -n \"\$envname\" ] || { echo 'no base_url_env in job.toml' >&2; exit 1; }
      chmod +x $REMOTE_JOBS/Trawler/scripts/remote_watchdog.sh
      rm -f $REMOTE_JOBS/$JOB/INTERRUPT   # clear a stale interrupt flag before a fresh run
      TMUX_BIN=\$(command -v tmux || ls /opt/homebrew/bin/tmux 2>/dev/null || true)
      if [ -n \"\$TMUX_BIN\" ]; then
        \$TMUX_BIN kill-session -t trawler-$JOB 2>/dev/null || true
        \$TMUX_BIN new-session -d -s trawler-$JOB \
          \"$REMOTE_JOBS/Trawler/scripts/remote_watchdog.sh $REMOTE_JOBS $JOB \$envname $REMOTE_URL $* 2>&1 | tee $REMOTE_JOBS/$JOB.log\"
        echo \"started in tmux session trawler-$JOB (watchdog, auto-recover), env \$envname=$REMOTE_URL\"
      else
        cd $REMOTE_JOBS/Trawler && \
        env \"\$envname=$REMOTE_URL\" \
        nohup ~/.local/bin/uv run trawler run-bundle $REMOTE_JOBS/$JOB $* \
        > $REMOTE_JOBS/$JOB.log 2>&1 & echo \"started (nohup, no tmux), env \$envname=$REMOTE_URL\"
      fi"
    _stamp "$JOB" "running"
    ;;
  status)
    exec "$HERE/remote_status.sh" ${NAME:+-r "$NAME"} ${JOB:+"$JOB"}
    ;;
  pull)
    # Check whether the job is CURRENTLY RUNNING before pulling: a queue/active
    # task file OR a live run-bundle process means the pull is a PARTIAL
    # snapshot. Match on the job id string, NOT on $REMOTE_JOBS — that's a
    # literal '~/...' path that won't tilde-expand inside the double-quoted
    # remote command (see the `interrupt` case above / MANUAL.md for the same
    # trap). pgrep doesn't need tmux's full-path treatment; tmux would.
    running=$(ssh "$REMOTE_SSH" "
      [ -f $REMOTE_JOBS/queue/active/$JOB.task ] && echo yes
      pgrep -f \"run-bundle.*$JOB\" >/dev/null 2>&1 && echo yes
      true" | grep -q yes && echo yes || echo no)
    if [ "$running" = "yes" ]; then
      echo "WARNING: $JOB is still RUNNING on $REMOTE_NAME — pulling a PARTIAL snapshot; import will mark it interrupted, re-pull later for more rows" >&2
    fi
    echo "[pull] $JOB/job.sqlite <- $REMOTE_NAME"
    rsync -a "$REMOTE_SSH:$REMOTE_JOBS/$JOB/job.sqlite" "$LOCAL_JOB/"
    ok=$(sqlite3 "$LOCAL_JOB/job.sqlite" "SELECT count(*) FROM results WHERE status='ok'" 2>/dev/null || echo "?")
    fail=$(sqlite3 "$LOCAL_JOB/job.sqlite" "SELECT count(*) FROM results WHERE status='fail'" 2>/dev/null || echo 0)
    sync_job_row "$JOB" "$ok" "$fail" "pulled"
    ;;
  import)
    "$0" pull ${NAME:+-r "$NAME"} "$JOB"
    cd "$ROOT" && uv run trawler import "output/jobs/$JOB"
    ;;
  enqueue)
    # -p N: signed priority (default 0). Higher runs first; a strictly higher
    # newcomer preempts the active job (it requeues and resumes later).
    PRIO=0
    if [ "${1:-}" = "-p" ] || [ "${1:-}" = "--priority" ]; then
      PRIO="${2:-}"; shift 2 || { echo "-p needs a value" >&2; exit 1; }
      [[ "$PRIO" =~ ^-?[0-9]+$ ]] || { echo "-p must be an integer, got '$PRIO'" >&2; exit 1; }
    fi
    # concurrency comes from TRAWLER_REMOTE_<NAME>_WORKERS — don't pass --concurrency
    if [ -d "$LOCAL_JOB" ]; then
      "$0" push ${NAME:+-r "$NAME"} "$JOB"
    else
      # No local dir is OK only if the job already lives on the box (e.g.
      # re-enqueue after `clean`). Otherwise queueing would park it as stuck.
      ssh "$REMOTE_SSH" "[ -f $REMOTE_JOBS/$JOB/job.toml ]" || {
        echo "no local job dir $LOCAL_JOB and no $JOB on $REMOTE_NAME — bundle first?" >&2
        exit 1
      }
    fi
    scp -q "$HERE/remote_watchdog.sh" "$HERE/remote_queue.sh" "$REMOTE_SSH:$REMOTE_JOBS/Trawler/scripts/" 2>/dev/null || true
    ssh "$REMOTE_SSH" "
      chmod +x $REMOTE_JOBS/Trawler/scripts/remote_queue.sh $REMOTE_JOBS/Trawler/scripts/remote_watchdog.sh
      mkdir -p $REMOTE_JOBS/queue/done
      # re-enqueue = this job is active again: purge stale task copies from any
      # prior cycle's terminal dirs so it appears in exactly one place (was:
      # a job could linger in done/ AND interrupted/ across interrupt cycles).
      rm -f $REMOTE_JOBS/queue/done/$JOB.task $REMOTE_JOBS/queue/cooling/$JOB.task \
            $REMOTE_JOBS/queue/interrupted/$JOB.task $REMOTE_JOBS/queue/stopped/$JOB.task \
            $REMOTE_JOBS/queue/stuck/$JOB.task
      printf '%s\n%s\n%s\nprio=%s\n' '$JOB' '$*' '$REMOTE_URL' '$PRIO' > $REMOTE_JOBS/queue/$JOB.task
      TMUX_BIN=\$(command -v tmux || ls /opt/homebrew/bin/tmux 2>/dev/null || true)
      [ -n \"\$TMUX_BIN\" ] || { echo 'no tmux on box — queue needs tmux' >&2; exit 1; }
      if \$TMUX_BIN has-session -t trawler-queue-$REMOTE_NAME 2>/dev/null; then
        # NAME collision guard: a session with this name exists, but is it
        # actually watching OUR jobs dir? Two people picking the same
        # REMOTE_NAME by accident would otherwise silently share (and starve)
        # each other's queue — the job just enqueued would never run.
        if ! pgrep -f \"remote_queue.sh $REMOTE_JOBS \" >/dev/null 2>&1; then
          echo \"WARNING: tmux session 'trawler-queue-$REMOTE_NAME' already exists on this box but is NOT watching $REMOTE_JOBS — someone else is using the same REMOTE_NAME ('$REMOTE_NAME'). Your job will NOT run until this is fixed. Pick a different name in your .env (TRAWLER_REMOTES) and re-enqueue.\" >&2
        fi
      else
        \$TMUX_BIN new-session -d -s trawler-queue-$REMOTE_NAME \
          \"$REMOTE_JOBS/Trawler/scripts/remote_queue.sh $REMOTE_JOBS $REMOTE_WORKERS 2>&1 | tee $REMOTE_JOBS/queue.log\"
        echo 'queue runner started (tmux session trawler-queue-$REMOTE_NAME)'
      fi
      echo \"enqueued $JOB (prio=$PRIO, workers=$REMOTE_WORKERS)\""
    _stamp "$JOB" "queued"
    ;;
  interrupt)
    [ -n "$JOB" ] || { echo "usage: offload.sh interrupt [-r name] <job-id>" >&2; exit 1; }
    echo "[interrupt] $JOB on $REMOTE_NAME"
    ssh "$REMOTE_SSH" "
      Q=$REMOTE_JOBS/queue
      # graceful: the watchdog (queue or direct-run) honors this flag and exits 2
      # — no relaunch, no requeue. Cleared before any future run of this job.
      touch $REMOTE_JOBS/$JOB/INTERRUPT 2>/dev/null || true
      # not-yet-running jobs have no watchdog: pull their task out of the queue
      # so it is never (re)selected.
      if [ -f \$Q/$JOB.task ] || [ -f \$Q/cooling/$JOB.task ]; then
        mkdir -p \$Q/interrupted
        mv \$Q/$JOB.task \$Q/cooling/$JOB.task \$Q/interrupted/ 2>/dev/null || true
        echo '  dequeued (was waiting/cooling)'
      fi
      [ -f \$Q/active/$JOB.task ] && echo '  active — watchdog signalled; job archives to interrupted/'
      # interrupt the in-flight pass; run-bundle commits per row, so this is a
      # safe resume point (re-enqueue/run picks up from the ok rows already written).
      # Match on the job id, not \$REMOTE_JOBS (a literal ~ that the remote shell
      # won't expand inside double quotes — the real cmdline has an absolute path).
      pkill -f \"run-bundle.*$JOB\" 2>/dev/null && echo '  killed running run-bundle' || echo '  no live run-bundle'
      echo '  interrupt issued — pull/import for partial results, enqueue to resume'
    "
    _stamp "$JOB" "interrupted"
    ;;
  queue)
    ssh "$REMOTE_SSH" "
      QUEUE=$REMOTE_JOBS/queue
      TMUX_BIN=\$(command -v tmux || ls /opt/homebrew/bin/tmux 2>/dev/null || true)
      runner=\$({ [ -n \"\$TMUX_BIN\" ] && \$TMUX_BIN has-session -t trawler-queue-$REMOTE_NAME 2>/dev/null && echo 'UP'; } || echo 'DOWN')
      if [ \"\$runner\" = 'UP' ] && ! pgrep -f \"remote_queue.sh $REMOTE_JOBS \" >/dev/null 2>&1; then
        runner='UP (NAME COLLISION: another jobs dir owns this session — pick a different REMOTE_NAME)'
      fi
      echo \"runner:   \$runner\"
      echo 'active:'
      found=0; while IFS= read -r t; do found=1
        jb=\$(sed -n 1p \"\$t\"); echo \"  \$jb (RUNNING)\"
      done < <(find \$QUEUE/active -name '*.task' 2>/dev/null); [ \$found -eq 0 ] && echo '  (none)'
      echo 'waiting:'
      found=0; while IFS= read -r t; do found=1
        jb=\$(sed -n 1p \"\$t\")
        pr=\$(grep -m1 '^prio=' \"\$t\" 2>/dev/null | cut -d= -f2)
        retry=\$(grep -m1 '^retry=' \"\$t\" 2>/dev/null | cut -d= -f2)
        echo \"  \$jb prio=\${pr:-0}\${retry:+ (retry #\$retry)}\"
      done < <(find \$QUEUE -maxdepth 1 -name '*.task' 2>/dev/null); [ \$found -eq 0 ] && echo '  (none)'
      echo 'cooling:'
      found=0; while IFS= read -r ct; do [ -f \"\$ct\" ] || continue; found=1
        jb=\$(sed -n 1p \"\$ct\")
        retry=\$(grep -m1 '^retry=' \"\$ct\" 2>/dev/null | cut -d= -f2); [ -z \"\$retry\" ] && retry=\$(sed -n 4p \"\$ct\")
        retry_at=\$(grep -m1 '^retry_at=' \"\$ct\" 2>/dev/null | cut -d= -f2); [ -z \"\$retry_at\" ] && retry_at=\$(sed -n 5p \"\$ct\")
        left=\$(( retry_at - \$(date +%s) )); [ \$left -lt 0 ] && left=0
        echo \"  \$jb retry=#\$retry in \$(( left/60 ))min\"
      done < <(find \$QUEUE/cooling -name '*.task' 2>/dev/null); [ \$found -eq 0 ] && echo '  (none)'
      echo 'done:'
      d=\$(ls \$QUEUE/done 2>/dev/null | tail -5 | sed 's/\.task\$//; s/^/  /'); echo \"\${d:-  (none)}\"
      st=\$(ls \$QUEUE/interrupted 2>/dev/null | tail -5 | sed 's/\.task\$//; s/^/  /')
      [ -n \"\$st\" ] && { echo 'interrupted:'; echo \"\$st\"; }
      s=\$(ls \$QUEUE/stuck 2>/dev/null | head -5 | sed 's/^/  /')
      [ -n \"\$s\" ] && { echo 'stuck (malformed):'; echo \"\$s\"; }
      echo ''
      echo 'log:'
      tail -4 $REMOTE_JOBS/queue.log 2>/dev/null | sed 's/^/  /'"
    ;;
  clean)
    [ -n "$JOB" ] || { echo "usage: offload.sh clean [-r name] <job-id>" >&2; exit 1; }
    echo "[clean] $JOB on $REMOTE_NAME"
    ssh "$REMOTE_SSH" "
      rm -rf $REMOTE_JOBS/$JOB
      rm -f $REMOTE_JOBS/queue/$JOB.task $REMOTE_JOBS/queue/active/$JOB.task \
            $REMOTE_JOBS/queue/cooling/$JOB.task $REMOTE_JOBS/queue/done/$JOB.task \
            $REMOTE_JOBS/queue/interrupted/$JOB.task 2>/dev/null || true
      echo '  removed $REMOTE_JOBS/$JOB (+ queue tasks)'
    "
    ;;
  models)
    echo "[models] $REMOTE_NAME ($REMOTE_MODELS)"
    ssh "$REMOTE_SSH" "
      ls -lh $REMOTE_MODELS/ 2>/dev/null || echo '  (empty — no $REMOTE_MODELS dir yet)'
      echo '--- model servers running ---'
      pgrep -fl 'llama-server|lms|LM Studio|ollama serve' 2>/dev/null | head -5 || true
      curl -s -m 3 $REMOTE_URL/models 2>/dev/null | head -c 300 && echo
    "
    ;;
  fetch-model)
    # JOB slot holds <hf-repo> here; next arg is <file>
    REPO="$JOB"; FILE="${1:-}"
    [ -n "$REPO" ] && [ -n "$FILE" ] || { echo "usage: offload.sh fetch-model [-r name] <hf-repo> <file>" >&2; exit 1; }
    echo "[fetch-model] $REPO/$FILE -> $REMOTE_NAME:$REMOTE_MODELS (resumable, detached)"
    ssh "$REMOTE_SSH" "mkdir -p $REMOTE_MODELS && \
      nohup curl -sL -C - -o $REMOTE_MODELS/$FILE \
        'https://huggingface.co/$REPO/resolve/main/$FILE' \
        > $REMOTE_MODELS/$FILE.download.log 2>&1 & echo download pid \$!"
    echo "check progress: scripts/offload.sh models${NAME:+ -r $NAME}"
    ;;
  *)
    sed -n '2,14p' "$0"; exit 1 ;;
esac
