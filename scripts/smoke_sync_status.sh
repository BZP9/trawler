#!/usr/bin/env bash
# Smoke test for sync_job_row's status-column transitions (item 4 of
# output/psql-status-sync/STATE.md's status-model amendment). Runs FULLY
# LOCAL — no remote box, no real Postgres. Fakes `psql` as a capturing stub
# on PATH and asserts the UPDATE it would run for each observed <stage>.
#
#   bash scripts/smoke_sync_status.sh     # exits 0 on PASS, 1 on FAIL
#
# Contract under test (scripts/remote_env.sh sync_job_row):
#   stage=running     -> UPDATE ... SET ... status = 'running'  ...
#   stage=interrupted -> UPDATE ... SET ... status = 'interrupted' ...
#   any other stage    -> no status assignment (n_done/n_failed/config->>stage
#                          only; never downgrades a job already complete/cleaned)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
fails=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Fake psql: captures the -c SQL argument to a file instead of connecting.
cat > "$tmp/psql" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do :; done   # no-op, just to look like a real invocation
# the SQL text is the argument right after -c
prev=""
for a in "$@"; do
  if [ "$prev" = "-c" ]; then echo "$a" >> "$PSQL_CAPTURE"; fi
  prev="$a"
done
exit 0
EOF
chmod +x "$tmp/psql"
export PATH="$tmp:$PATH"
export PSQL_CAPTURE="$tmp/captured.sql"
export TRAWLER_DSN="postgresql://fake"

source "$HERE/remote_env.sh"

check_status() {  # <stage> <expect-status-clause-or-empty>
  local stage="$1" expect="$2"
  : > "$PSQL_CAPTURE"
  sync_job_row "job-x" 5 1 "$stage"
  local sql; sql="$(cat "$PSQL_CAPTURE" 2>/dev/null || true)"
  if [ -z "$expect" ]; then
    if echo "$sql" | grep -q "status = '"; then
      echo "FAIL: stage=$stage should NOT set status, got: $sql"
      fails=1
    else
      echo "PASS: stage=$stage leaves status untouched"
    fi
  else
    if echo "$sql" | grep -q "status = '$expect'"; then
      echo "PASS: stage=$stage -> status='$expect'"
    else
      echo "FAIL: stage=$stage expected status='$expect', got: $sql"
      fails=1
    fi
  fi
}

check_status "running"     "running"
check_status "interrupted" "interrupted"
check_status "stalled"     ""
check_status "pulled"      ""
check_status "queued"      ""

# WHERE clause must only ever match pending statuses (never touch complete/cleaned).
: > "$PSQL_CAPTURE"
sync_job_row "job-x" 5 1 "running"
sql="$(cat "$PSQL_CAPTURE")"
if echo "$sql" | grep -q "status IN ('exported','running','partial','interrupted')"; then
  echo "PASS: WHERE clause scoped to pending statuses only"
else
  echo "FAIL: WHERE clause missing pending-status scope, got: $sql"
  fails=1
fi

if [ "$fails" -eq 0 ]; then
  echo "SMOKE OK"
  exit 0
fi
echo "SMOKE FAILED"
exit 1
