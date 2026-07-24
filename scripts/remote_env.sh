# Shared resolver for named offload remotes. Source this, then call:
#   resolve_remote [name]
# Sets: REMOTE_NAME, REMOTE_SSH, REMOTE_JOBS, REMOTE_URL
# Reads Trawler/.env (see .env.example); pre-exported env vars win.

_trawler_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$_trawler_root/.env" ]; then
  # export .env values without clobbering vars already in the environment
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    [ -z "${!k:-}" ] && export "$k=$v"
  done < "$_trawler_root/.env"
fi

# Opportunistic control-plane sync: any script that just observed a job's
# remote counts writes them back to its gen._gen_log row (pending jobs only —
# import owns the final/complete status; this only moves between the
# pending statuses: exported/running/partial/interrupted).
#
# status-model (2026-07-14): the observed <stage> maps to a status column
# transition — 'running' (stage says the process is live) or 'interrupted'
# (stage says the job was found parked in queue/interrupted/). Any other
# stage value (e.g. 'stalled', 'pulled', 'queued') updates n_done/n_failed
# and config->>'stage' only, leaving status as whatever import/bundle last
# set it (never downgrades a 'complete'/'cleaned' row — the WHERE clause
# only matches rows still in a pending status).
sync_job_row() {  # sync_job_row <job-id> <ok> <fail> <stage>
  [ -n "${2:-}" ] && [ "${2}" != "?" ] || return 0
  local status_set=""
  case "${4:-}" in
    running)     status_set=", status = 'running'" ;;
    interrupted) status_set=", status = 'interrupted'" ;;
  esac
  psql "${TRAWLER_DSN:-${ROWINFER_DSN:-}}" -q -c \
    "UPDATE gen._gen_log
     SET n_done = $2, n_failed = ${3:-0}${status_set},
         config = config || jsonb_build_object('stage', '${4:-running}')
     WHERE config->>'job_id' = '$1'
       AND status IN ('exported','running','partial','interrupted')" 2>/dev/null || true
}

resolve_remote() {
  local name="${1:-}"
  local remotes="${TRAWLER_REMOTES:-}"
  if [ -z "$remotes" ]; then
    echo "TRAWLER_REMOTES not set — copy .env.example to .env and fill it in" >&2
    return 1
  fi
  [ -z "$name" ] && name="${remotes%%,*}"          # default = first listed
  local upper; upper=$(echo "$name" | tr '[:lower:]-' '[:upper:]_')
  local ssh_var="TRAWLER_REMOTE_${upper}_SSH"
  local jobs_var="TRAWLER_REMOTE_${upper}_JOBS"
  local url_var="TRAWLER_REMOTE_${upper}_URL"
  local models_var="TRAWLER_REMOTE_${upper}_MODELS"
  local workers_var="TRAWLER_REMOTE_${upper}_WORKERS"
  REMOTE_NAME="$name"
  REMOTE_SSH="${!ssh_var:-}"
  REMOTE_JOBS="${!jobs_var:-~/trawler-jobs}"
  REMOTE_URL="${!url_var:-http://localhost:8080/v1}"
  REMOTE_MODELS="${!models_var:-~/models}"
  REMOTE_WORKERS="${!workers_var:-4}"
  if [ -z "$REMOTE_SSH" ]; then
    echo "remote '$name' has no $ssh_var in .env (known: $remotes)" >&2
    return 1
  fi
}
