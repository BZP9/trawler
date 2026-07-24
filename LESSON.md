# LESSON

Things learned from building the gen pipeline + first end-to-end LMS run.

## Importing a partial job marked it 'complete'

`import_bundle` hard-coded `status='complete'` on the `gen._gen_log` row. Importing an interrupted job (2019 of 487947 rows, pulled to use elsewhere) marked the whole job complete — it vanished from `trawler jobs`, became a `clean --imported` target, and lied that the main task was done.

- Import status must reflect real completion: `complete` only when `len(results) >= job_meta.row_count`, else `interrupted` (`_completion_status`). Keep partial jobs in the pending view (`status IN ('exported','interrupted')`) and out of `clean --imported`.
- run_id is assigned at `bundle` and REUSED through import (same `_gen_log` row) — not created per import. Multiple run_ids for one prompt = separate runs (live + offload, or cycles); readers union+dedup by row_key.

## `~` doesn't expand inside double-quoted remote-shell strings

`trawler interrupt`'s `pkill -f "run-bundle $REMOTE_JOBS/$JOB"` silently matched nothing ("no live run-bundle") while the job kept running. `$REMOTE_JOBS` is the literal string `~/trawler-jobs`; inside the double-quoted pkill pattern the remote shell does NOT tilde-expand it, but the real process cmdline has the absolute `/Users/alvin.huang/trawler-jobs/...`, so the pattern never matched. (An unquoted `Q=$REMOTE_JOBS/queue` assignment DID expand, which masked the bug.)

- In remote-shell strings, never rely on `~` expanding inside double quotes. Match on the job id (`pkill -f "run-bundle.*$JOB"`) or verify the pattern against the real `ps -o command` cmdline.

## Interrupt via `exit 0` mislabels a job "complete"

The first cut made the watchdog `exit 0` on the interrupt flag (to dodge the queue's retry path). But the queue reads `exit 0` as **complete** → it logged a 2019/487947 job as `complete` and filed it in `done/`, indistinguishable from a finished run.

- Give distinct outcomes distinct exit codes: watchdog `0`=complete, `1`=stalled, `2`=interrupted; the queue routes on them (`done/`/`cooling/`/`interrupted/`). Guarded by `scripts/smoke_interrupt.sh`.

## Shipped remote scripts are dormant until the queue session restarts

`enqueue`/`run` scp the latest `remote_queue.sh`/`remote_watchdog.sh` to the box, but a **running** `trawler-queue` tmux session keeps executing the OLD code in memory. New STOP/exit-code behavior did nothing until the session was killed and re-enqueued.

- After changing the queue/watchdog scripts, restart the box's queue session (kill `trawler-queue` + re-enqueue) — don't expect a live watchdog to pick up new code. Say "takes effect next run" in summaries.

## `-r NAME` must precede the job id in offload passthrough verbs

`trawler enqueue <JOB> -r studio --retries 2` leaked `-r studio` into run-bundle's args (`unrecognized arguments: -r studio`, 0 progress) because argparse's `REMAINDER` starts at the first positional and swallows everything after. `trawler enqueue -r studio <JOB> ...` parses correctly.

- Put `-r NAME` BEFORE the job id for every passthrough verb (enqueue/run/interrupt/pull/…). Documented in MANUAL.

## Reasoning models eat `max_tokens`

Gemma-4-31b puts chain-of-thought in `reasoning_content`; `content` stays empty until reasoning finishes. With `max_tokens=200` the model burned all budget reasoning and emitted nothing. With 2000 still truncated mid-output.

- Default `max_tokens >= 2000` for reasoning models. Probe with `set_limit(1)` first.
- `finish_reason == 'length'` is the canonical "budget too small" signal — surface it. Don't only check `content == ''`.
- The `usage.completion_tokens_details.reasoning_tokens` field tells you how much reasoning ate.

## OpenAI-compatible base URLs MUST end in `/v1`

LM Studio: `http://host:port/v1` — not `http://host:port`. Code appends `/chat/completions`; without `/v1` the request 404/400s. Bake into docs + seed env example.

## LLM JSON output is messy

Real models emit any of:
- ```json {…} ```` (fenced)
- preamble "Here you go:\n```json…```\n" (fence in middle)
- "Sure! `{…}` — done." (raw object embedded in prose)
- truncated mid-key (no closing brace)
- pretty-printed nested with arrays

Stripping `^```…```$` from edges only is not enough. Need: (a) regex search for fence anywhere, (b) fall back to first balanced `{…}` / `[…]` block, (c) raise `ParseError` with the first 200 chars so the user can read what the model actually said.

## Per-row failure isolation needs a "floor"

If `post_step` raises (parse fail), naive try/except wipes the extras dict — `raw_output` and carry cols vanish. Caller can't tell whether the LLM never responded vs. responded with garbage.

Fix: `_extras_floor(row, raw)` runs unconditionally and merges UNDER post_step extras. raw_output + carry cols always written, even on parse fail. `error_category` (class name of the exception) makes per-row failures sortable in SQL.

## Categorized exceptions > generic RuntimeError

`BudgetError`, `EndpointError`, `ProtocolError`, `ParseError`, `ConfigError`. Each row's `error_category` col holds the class name. `WHERE error_category='BudgetError'` instantly tells you "raise max_tokens" without grepping tracebacks.

5xx → EndpointError (transient, retry-worthy).
4xx → ProtocolError (your fault — bad model name, bad payload, no /v1, etc.).
finish=length + empty content → BudgetError.

## Pre-flight saves money

Run one dry row through `pre_step + step + post_step` **before** `_register_run`. Catches config/budget/endpoint/parse without:
- burning a log row
- iterating N-1 doomed rows
- needing the user to read tracebacks for the second time

Trade-off: one extra LLM call per run. Worth it. Toggle with `set_preflight(False)` for trusted setups.

## Schema migrations need `ADD COLUMN IF NOT EXISTS`

Iterating the design adds cols. `CREATE TABLE IF NOT EXISTS` is not enough — pre-existing tables stay frozen at old shape. Pair each `CREATE TABLE` with explicit `ALTER … ADD COLUMN IF NOT EXISTS` for new cols. Idempotent migration story stays simple.

For per-output tables, the backbone calls `_ensure_column(col, sql_type)` (also `ADD COLUMN IF NOT EXISTS`) so subclasses can grow tables at runtime without a separate migration step.

## Setter validation > plain attrs

Early experiment: plain `gen.model = "x"` direct assign. Late failure when the cfg row doesn't exist meant a useless `_gen_log` row + interrupt mid-iter.

Switching to `set_model("x")` that immediately SELECTs the cfg.decoder row turns typos into instant errors. Same pattern for `set_model_type` (env var must resolve), `set_system_prompt` (must exist + EXPECTED_OUTPUT must match), `set_data_source` (peek first row, validate `source_uid` present).

Trade-off: setters can't be reordered freely without thinking — but the early failure mode is worth it.

## Track `n_rows` / `n_done` / `n_failed` per run

Just `n_done` doesn't tell you health. Adding `n_failed` (per-row failure counter) makes `_gen_log` self-diagnosing:

```sql
SELECT name, status, n_rows, n_done, n_failed FROM gen._gen_log ORDER BY started_at DESC;
```

`n_rows` set at register from limit or `len(data_source)` (null if neither available).

## Output column types

Default everything to **jsonb** (preserves type roundtrip — int, str, bool, list, dict). Use **text** only for genuinely-text things (raw_output: raw LLM string, error: traceback). Use **vector(dim)** for embeddings later. Never default to text for arbitrary user values — string-coerced ints lose type.

## TextGen doesn't need a parsed output col

Earlier draft: TextGen stored `json_output = Jsonb(raw)`. That's just the raw text json-encoded — duplicate data, confusing for callers. Drop. TextGen only writes `raw_output`. JsonGen adds `json_output` on top.

Lesson: don't add cols just for symmetry across variants. Each variant owns its own cols via `_out_table_cols()`.

## Pyright vs venv

Pyright in editor reads system Python; runtime uses `.venv`. Diagnostics like "psycopg cannot be resolved" are venv-resolution noise, not real errors. Add a `pyrightconfig.json` with `venvPath`/`venv` to silence them next pass.

## Postgres URL conventions

- JDBC URLs prefix with `jdbc:` — strip before handing to psycopg.
- `template1` is the safe admin DB to connect to for `CREATE DATABASE` (always exists, can't be dropped).
- `CREATE DATABASE` needs `autocommit=True` — can't run inside a transaction.

## Renaming the repo folder breaks every venv pointing at it

Folder rename (RowInfer → Trawler) left `.venv/bin/*` shebangs pointing at the old absolute path — `bad interpreter` on any uv run, in this repo AND every consumer repo with an editable install. `rm -rf .venv && uv sync` in each.

- After moving/renaming a repo dir: recreate venvs, don't debug them.

## CHECK constraints drift from code silently when tests are DB-free

`_finalize` writes `early_stopped` but `_gen_log`'s status CHECK only allowed 4 values — a real early-stopped run would crash at finalize. DB-free tests (FakeRun stubs `_finalize`) can never catch constraint drift.

- When adding a status/enum value in code, grep `init.sql` for the CHECK in the same commit.
- Widening a CHECK needs DROP+ADD CONSTRAINT on live DBs too — init.sql only fixes fresh installs.

## LM Studio "server running" ≠ reachable from the network

`lms server status` said running on 4160 while external curl got connection refused — bound to loopback. Not an outage; it's the normal reason to use the offload loop (run-bundle on the box itself against localhost).

- Before debugging a "down" model server, check on-box loopback first: `ssh <box> curl localhost:<port>/v1/models`.

## job.sqlite is remote-owned — a re-push wipes progress

`offload.sh enqueue` ran `push` first; rsync overwrote the remote job.sqlite
(150 rows of accumulated results) with the local empty copy from bundle
export. Resumable design meant only recompute, not data loss — but hours of
GPU time burned.

- Push everything EXCEPT job.sqlite; ship job.sqlite only with
  `--ignore-existing`. The results DB belongs to whoever is running the job.
- Direction discipline: job dir flows local→remote once; job.sqlite flows
  remote→local only (pull).

## Skill trigger keyword overlap misrouted "remote status" to trawler-inspect

A Haiku session asked to "check trawler remote status" keyword-matched
trawler-inspect ("checking run status") instead of trawler-offload, which
owned the answer (`trawler status`) but whose trigger never said
"status". inspect then crashed with `KeyError: 'system_prompt'` on the
offload log rows (bundle writes a different config shape), and the session
degenerated into guessing sqlite paths and psql table names.

- Skill triggers must claim their status/check verbs explicitly AND
  cross-reference the neighbor skill they're confusable with.
- Skill blocks consumed by small models need a copy-paste runnable example —
  API signatures alone invite invented dict keys and table names.
- Any code path reading `_gen_log.config` must handle both shapes: live-run
  (`config['system_prompt']`) and offload (`config['offload']/['prompt']`).

## Bare `ssh box tmux ...` silently no-ops — tmux not on PATH non-interactively

Deploying the priority queue: `ssh studio 'tmux kill-session -t trawler-queue'`
reported "no queue session" while the session was alive — non-interactive ssh
on the Mac box doesn't load /opt/homebrew/bin into PATH, so `tmux` was
command-not-found and the || branch misread that as "absent". The old-code
queue kept running. Full path worked. Also: killing the session left an
orphan `uv ... run-bundle` process (pkill needed) and a stale
`queue/active/*.task` file (enqueue purges terminal dirs, not active/).

- Over non-interactive ssh, always call tmux by full path (the scripts'
  `TMUX_BIN=$(command -v tmux || ls /opt/homebrew/bin/tmux)` pattern) —
  never bare `tmux`.
- After killing a queue session: `pkill -f run-bundle` and
  `rm -f queue/active/*.task`, then verify with
  `pgrep -fl "remote_queue|remote_watchdog|run-bundle"`.

## Preemption froze the queue: killing $! of a pipeline kills only the logger

First live preemption (prio 1 over -1) fired on time, then hung the box for
5+ min: in remote_watchdog.sh, `RUN_PID=$!` of `trawler run-bundle |
while read ...` is the PID of the LAST pipeline stage. `kill $RUN_PID` killed
the log reader; `wait $RUN_PID` then waited on the WHOLE pipeline job, whose
uv/python run-bundle processes were still running — watchdog blocked, queue
frozen, run-bundle kept going with no log lines. The interrupt path had the
same latent bug, masked because offload.sh interrupt pkills run-bundle
externally.

- To stop a backgrounded pipeline, kill by cmdline match (pkill -f
  "run-bundle.*$JOB") or the process group — never just $!.
- Smoke tests that stub the watchdog validate queue routing only; the first
  live preemption/interrupt on a real box is part of the test plan, watched
  end-to-end (queue.log must show the NEXT job starting, not just the
  PREEMPTED line).

## Same-named staging table ≠ same content — reinventing stage_*.py corrupts silently

Round 4 of a Haiku offload field test correctly used `trawler rebundle` (a
fix from round 3's failure) but, on seeing "0 pending", wrote its own
Postgres staging script instead of finding and running the task's real
`stage_dims2jd_docs.py`. Its script destructively renamed the correct
3838-row `raw.dims2jd_docs` (proper multi-section doc template) out of the
way and replaced it with 11,360 rows of raw JSON dump — a bundle was
actively shipping that wrong-format data to the remote before it was caught,
interrupted, and restored from the rename-preserved backup. `job-config`/
`rebundle` only verify schema (table+column names match); they cannot detect
that the *content-building logic* differs. A right-shaped, wrong-content
table is strictly more dangerous than a wrong-shaped one because it passes
every automated check.

- "0 pending" or "staging looks stale" is never a green light to write
  staging SQL — it's a signal to search for and run that task's own
  `stage_*.py` (`find <task-dir> -name 'stage_*.py'`).
- When restoring/rebuilding any table an agent may have touched, verify
  actual row CONTENT against a known-good sample, not just row count or
  column names — schema match is not content match.
- Destructive rename-based "migrations" written ad hoc by an agent are a
  smell even when well-intentioned (this one's rename-not-drop pattern is
  literally why recovery was possible) — prefer scripts that upsert.

## Local `interrupted` status ≠ job stopped on the box — check remote before enqueue
A session re-enqueued dims2jd-20260714T020028Z because psql showed
`status='interrupted'` after a partial import — but that status only means
"partial pull"; the job was still RUNNING on studio. Result: duplicate queue
entry (same id active AND waiting) and a `push` that rsynced a stale
job.sqlite over the live one mid-run (no damage observed, by luck).
- Before `trawler enqueue`/`push`, run `trawler status` and confirm the job
  is NOT in `active:` — local `_gen_log` status is import-time state, not
  remote process state.

## Bundle written to cwd, enqueue shipped nothing, job parked as stuck
`trawler bundle` run from ~/work/finetune (via the shell alias) wrote the job
dir to finetune/output/jobs/ because --out defaulted to cwd-relative
"output/jobs", while push/enqueue/import resolve _repo_root()/output/jobs.
enqueue then silently skipped the push (`[ -d ] &&`) and queued anyway; the
remote parked the task as stuck (malformed/missing job dir).
- Fixed: bundle --out now defaults to <repo>/output/jobs regardless of cwd;
  enqueue aborts if the job dir exists neither locally nor on the remote,
  and purges stale queue/stuck/<id>.task on re-enqueue.
- If a job lands in `stuck (malformed)`: the dir never reached the box —
  fix/move the local dir, re-enqueue, delete the stale stuck task file.

## Bare `trawler status` only synced the newest remote job, not all
User's jd2jd job had been interrupted days earlier but bare `trawler status`
kept showing stale status=running/stage=running from the last poll — because
the overview's remote section calls remote_status.sh with no job-id, which
picks only the newest job dir on the box (`ls -td | head -1`) to poll+sync.
Older non-terminal jobs never got refreshed unless polled by name.
- Fixed: `_sync_all_remote_jobs` sweeps every gen._gen_log row with
  status IN (exported, running, partial, interrupted) and polls each via
  remote_status.sh before the overview prints, same sync `status <job-id>`
  already did per-job.
- If a job's local status still looks wrong after `trawler status`: run
  `trawler status <job-id>` directly and compare — that path was always
  correct, only the no-arg overview had the gap.
