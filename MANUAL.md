# Trawler Manual — for future sessions (human or model)

Read this when you need the *workflow*, not the API. Reference docs are in
`SKILL.md` (per-task skills); copy-paste skeletons are in `templates/`;
source is `src/trawler/`. This file is the narrative that ties them together
so nobody has to be re-taught.

## What trawler is (one paragraph)

Trawler is a **control plane for batch LLM jobs**. Config (prompts, models,
transports) lives in Postgres `cfg.*`; every run writes an output table
(`gen.<prompt>` / `enc.<encoder>`) plus a run-log row with resume-by-run_id
semantics. Where the tokens get generated is deliberately pluggable: a local
model server, a remote one over HTTP, a paid API, or — via the **offload
loop** — a completely different machine with no network/DB access to this
one. Control stays here; compute goes wherever is cheapest.

## Configuration — `.env` (single source of truth)

All machine-specific values live in `Trawler/.env` (gitignored; template
committed as `.env.example` — copy and fill on a new machine). Both shell
scripts under `scripts/` AND python entry points (the `trawler` CLI,
`trawler-init`, every pipeline run via `BaseRun`) auto-load it — no
`set -a; source .env` needed. Exported env vars always override it;
`TRAWLER_ENV_FILE` overrides the file location.
Remotes are **named**: `TRAWLER_REMOTES=studio,worker2` +
`TRAWLER_REMOTE_<NAME>_SSH/_JOBS/_URL/_MODELS` per box. First name = default.
`offload.sh models` lists weights + running servers on a box;
`offload.sh fetch-model <hf-repo> <file>` downloads (resumable) into its _MODELS dir.
Never hardcode hosts/users/paths in scripts or docs — add a var.

## Environment facts (verified 2026-07-08 — re-verify if stale)

| Thing | Value |
|---|---|
| DSN env var | `TRAWLER_DSN` or legacy `ROWINFER_DSN` (postgres on localhost) |
| Repo | `~/work/Trawler` (github BZP9/Trawler); CI runs pytest + audit on push |
| Remote "studio" | see `.env` (`TRAWLER_REMOTE_STUDIO_SSH`); user's shell alias `macstudio` opens the same box |
| Model servers there | **llama.cpp** `~/llama.cpp/llama-server` (loopback :8080, GGUF) and **LM Studio** (loopback :4160, MLX). Both often loopback-only — external curl failing while the server runs is normal; that's what the offload loop is for |
| Model formats | LM Studio models may be **MLX** — llama.cpp needs **GGUF** (`~/.lmstudio/bin/lms ls` to check). gemma-4-31b GGUF: `unsloth/gemma-4-31B-it-GGUF:Q4_K_M` via `llama-server -hf` |
| Remote trawler install | `$TRAWLER_REMOTE_<NAME>_JOBS/Trawler` (rsync'd copy, uv-synced; uv at `~/.local/bin/uv`) |
| cfg.model_type rows | `remote_lms`, `remote_llamacpp`, `remote_ollama`, `anthropic`, ... — `psql $ROWINFER_DSN -c "SELECT * FROM cfg.model_type"` |
| Multiple model hosts (direct mode) | one `cfg.model_type` row + one env var per host — see `.env.example` |

## Workflow 1 — normal pipeline (network to model server available)

1. Copy the matching skeleton from `templates/` (03 text / 04 json / 07 embed).
2. Fill placeholders; cfg rows must exist (`templates/01_cfg_setup.py`).
3. Smoke with `set_limit(5)`, then raise/remove and run.
4. Interrupted/failed? Same script + `set_resume("<run_id>")` —
   `templates/06_resume_topup.py`. `set_limit(N, total=True)` tops up to N ok rows.
5. Check results: `templates/09_inspect_query.py`.

## Workflow 2 — the offload loop (compute on another machine, no DB there)

Built for: model server unreachable from here (loopback-only LM Studio),
or the run should burn a GPU box without holding a network session open.

### Where do I type this? Which machine?

**Every command below is typed on YOUR machine**, in this repo, in your
normal terminal. The `trawler` CLI ssh's out to the remote for you — you
never need to open your own ssh session to run the offload loop. The one
step that genuinely executes ON the remote (`run-bundle`, in the diagram
below) is not something you type either — the queue's watchdog launches it
automatically once you `enqueue`.
The **only** reason to ssh into the box yourself: to *watch* live output —
`ssh <alias>` then `tmux attach -t trawler-queue` (or `trawler-<job-id>` for
a direct `trawler run`) — detach with `Ctrl-b d`, don't kill the pane. That's
optional; `trawler status` gives you the same progress from your own machine.

**Which remote?** `-r <name>` picks one of your named remotes
(`TRAWLER_REMOTES` in `.env`); omit it and the first name in that list is
used. Today there is exactly one configured remote (`studio`, i.e. the Mac
Studio / `macstudio` alias) — so in practice you will rarely type `-r` at
all. It starts mattering once a second box is added to `.env`.

```
[local]  trawler bundle ...          → output/jobs/<job-id>/  (job.toml, rows.jsonl, job.sqlite)
[carry]  rsync the dir over                                    (done FOR you by `enqueue`/`push`)
[remote] trawler run-bundle <dir>    → results into job.sqlite (resumable, no Postgres)
[carry]  rsync ONLY job.sqlite back                             (done FOR you by `pull`/`import`)
[local]  trawler import <dir>        → gen.<prompt> + gen._gen_log (fresh run_id, config->>'job_id')
```

### Quick recipe: re-run / continue an existing prompt (the common case)

Goal: *"I want to re-run `<PROMPT>` on the remote, but I don't remember (or
never knew) the bundle parameters."* This is almost always what you want —
skip straight to `rebundle`, which recovers the exact recipe from that
prompt's last job. Never retype `--source`/`--pk`/`--doc-col` from memory:
that's exactly how a hand-reconstructed recipe ends up pointing at the wrong
table or the wrong column (see Rules below) and silently poisons the run.

Fill in what you actually know; `rebundle` fills in everything else:

```
PROMPT   = <name in cfg.prompt, e.g. "dims2jd">        [REQUIRED — you must know this]
REMOTE   = <name from .env TRAWLER_REMOTES>            [default: first configured remote]
PRIORITY = <signed integer>                            [default: 0 — plain FIFO]
```

```bash
# 1. (optional) orient yourself — has this prompt run before, and how did it go?
trawler jobs --all | grep <PROMPT>

# 2. recover the recipe + a preview — ZERO side effects (no job dir, no DB writes)
trawler rebundle <PROMPT>
#    → prints decoder / model_type / source / pk / doc_cols exactly as last
#      shipped, plus pending / total / already-claimed row counts.
#    → "0 pending rows"? nothing NEW to ship — if you expect new rows, refresh
#      the staging table via that task's stage_*.py, don't hand-write SQL.
#    → exits 1 naming missing fields? the last job predates this feature
#      (legacy log row) — try `job-config <older-job-id>`, or ask a human.

# 3. bundle for real (add --limit N to cap it, e.g. for a first smoke test)
trawler rebundle <PROMPT> --go [--limit N]
#    → prints the new JOB_ID — you'll need it for the next step.

# 4. ship it to the box and start it on the priority queue
trawler enqueue <JOB_ID> [-p <PRIORITY>] [-r <REMOTE>]
#    → omit -r for the default remote; omit -p for normal priority (0, FIFO).

# 5. watch — one command, everything you need
trawler status

# 6. once done (trawler status/jobs shows n_done == n_rows), bring results home
trawler import <JOB_ID>
```

That covers re-running a prompt that has shipped before. For a **brand-new**
prompt (first bundle ever), see the parameter table + checklist in "Exact
commands" below — `rebundle` has nothing to recover yet, so `bundle`'s full
flag set applies.

### Exact commands (proven 2026-07-09, priority/rebundle verbs added 2026-07-13/14; all remote-agnostic via .env)

The `trawler` CLI is the single front door; remote verbs take `-r <name>`
(default = first in `TRAWLER_REMOTES`). `scripts/*.sh` are the implementation —
you never need to call them directly.

**1. bundle** — only needed by hand for a prompt's FIRST-EVER offload job
(re-runs: use `rebundle` above instead, it fills these in for you).

| flag | required? | default | where the value comes from |
|---|---|---|---|
| `--prompt` | **required** | — | a row in `cfg.prompt` — `psql "$TRAWLER_DSN" -c "SELECT name FROM cfg.prompt"` |
| `--decoder` | **required** | — | a row in `cfg.model_name` — the model + decode-params combo to run |
| `--model-type` | **required** | — | a row in `cfg.model_type`, and it MUST match what's actually serving on the remote (llama.cpp → `remote_llamacpp`, LM Studio → `remote_lms`, ollama → `remote_ollama`) — check with `trawler models -r <REMOTE>` |
| `--source` | **required** | — | `schema.table` — must be a PLAIN table, no `WHERE`/run_id filter (a filtered or computed source needs a staging table first) |
| `--pk` | **required** | — | one or more primary-key columns, space-separated |
| `--doc-col` | **required** | — | one or more columns the remote joins with `\n` to build the model input — must already BE the final text (no custom `doc_fn` ships) |
| `--limit` | optional | unlimited (all pending rows) | cap it for a first smoke bundle, e.g. `--limit 20` |
| `--out` | optional | `output/jobs` | rarely changed |
| `--dry-run` | optional flag | off | preview pending/claimed/total + resolved config, ZERO side effects — always run this before the real bundle |

Checklist before a prompt's first-ever bundle:
- [ ] `cfg.prompt` row exists for this prompt (trawler-cfg skill creates one)
- [ ] `cfg.model_name` (decoder) exists for the model you want to run
- [ ] `cfg.model_type` exists AND matches the remote's actual running server
- [ ] source table exists, is a plain table, `--doc-col` columns already hold final text
- [ ] ran `--dry-run` and the pending count looks right before spending GPU time

bundle ALWAYS excludes row_keys already ok/pending in `gen.<prompt>` —
manual dedup of the source table before bundling is never needed and
wastes work.

```bash
trawler bundle \
  --prompt <PROMPT> --decoder <MODEL> --model-type <MODEL_TYPE> \
  --source <schema.table> --pk <UID_COL> --doc-col <DOC_COL> \
  --limit <N> --out output/jobs --dry-run

trawler bundle \
  --prompt <PROMPT> --decoder <MODEL> --model-type <MODEL_TYPE> \
  --source <schema.table> --pk <UID_COL> --doc-col <DOC_COL> \
  --limit <N> --out output/jobs

# 2. enqueue (preferred): pushes the dir + queues it on the box's priority queue.
#    One job at a time at TRAWLER_REMOTE_<NAME>_WORKERS concurrency (don't pass
#    --concurrency); each job runs under a watchdog (per-minute status,
#    auto-relaunch on crash/early-stop, health-gated); a stalled job cools 30min
#    then auto-retries forever; the next task auto-starts on completion.
#    -p N (signed int, default 0, put it right after the job id): higher prio
#    runs first; a strictly-higher newcomer PREEMPTS the active job within ~1min
#    (watchdog tick) — preempted job requeues with prio + queue position intact
#    and resumes from committed rows when it's top again. Give a months-long
#    backfill -p -1 and urgent jobs -p 1/10; equal prio = FIFO.
trawler enqueue <JOB_ID> --retries 2 --temperature 0.7 --max-tokens 4000
trawler enqueue <JOB_ID> -p 10 [run-bundle args...]   # jump the queue

# 3. watch — one-stop overview: remote progress/%/rate/ETA/log tail, queue
#    health, AND local pending jobs, in one shot (three headed sections:
#    == remote (studio) == / == queue == / == local pending jobs ==).
#    This is THE status command — never stitch queue+jobs+status by hand.
trawler status                # overview: remote + queue + local pending jobs
trawler status <JOB_ID>      # focused: specific job only (--all: every remote)
trawler queue                 # queue only: active/waiting/cooling/done
trawler jobs                  # local pending jobs only: control-plane view
                                     # (bundle registers status='exported' in gen._gen_log;
                                     #  push/run/enqueue stamp remote+stage; import completes the row.
                                     #  Any status/pull check ALSO syncs observed n_done/n_failed/stage
                                     #  back to the row — the table is as fresh as your last look)
trawler job-config <JOB_ID>   # print the bundle recipe (prompt/decoder/model_type/source/
                                     # pk/doc_cols + status/rows) and a ready-to-copy re-bundle
                                     # command. Reads local job.toml first, falls back to
                                     # gen._gen_log (still works after `trawler clean`).
trawler rebundle <PROMPT>     # PREFERRED way to re-ship a prompt: finds its most recent
                                     # job, resolves the recipe the same way job-config does, and
                                     # prints it + a dry-run preview (pending/total/claimed) plus
                                     # both the --go line and the raw `trawler bundle ...` line.
trawler rebundle <PROMPT> --go [--limit N]  # actually bundle; prints job dir + enqueue hint

# 4. bring results home: pulls job.sqlite + imports (refuses double import; --force to override)
#    import never deletes job.sqlite — local output/jobs/<id>/ and the remote
#    copy both persist (resume + audit artifact); clean up by hand when done.
trawler import <JOB_ID>

# interrupt — interrupt a running/queued job; partial kept and resumable.
#    Graceful: signals the watchdog (INTERRUPT flag) to stop the current pass
#    and exit 2, so the queue parks it in queue/interrupted/ (logged
#    "interrupted", NOT "complete") without relaunch/requeue. Then pull/import
#    the partial, or re-enqueue to resume from the ok rows already written.
#    (-r goes BEFORE the job id: `trawler enqueue -r studio <JOB> ...`.)
trawler interrupt -r <NAME> <JOB_ID>

# clean — reclaim disk from imported job dirs (they persist after import).
#    Also releases 'pending' claim rows from gen.<prompt> for the cleaned job.
#    Dry-run by default (reports claim count); --yes to delete + release claims.
#    If Postgres is unreachable, dirs are still deleted (claim release is best-effort).
trawler clean --imported            # every status='complete' job
trawler clean <JOB_ID> --yes        # one job, delete + release claims
trawler clean <JOB_ID> --force --yes  # even if not-yet-imported (releases orphan claims)

# occasional verbs
trawler push <JOB_ID> --with-repo   # re-sync trawler code after code changes
trawler run  <JOB_ID> [args...]     # direct tmux-watchdog run, skips the queue
trawler pull <JOB_ID>               # fetch job.sqlite without importing
                                           # (warns to stderr, but still pulls, if the job
                                           #  is still RUNNING on the box — partial snapshot)
trawler models                      # weights + running servers on the box
trawler fetch-model <hf-repo> <f>   # resumable GGUF download onto the box

# multi-box: any verb with -r
trawler enqueue -r worker2 <JOB_ID> --retries 2

# on the box: tmux attach -t trawler-queue (queue runner) / trawler-<JOB_ID> (direct run)
# reboot kills tmux — re-issue `trawler enqueue` after a reboot.
```

Splitting one job across boxes: export two bundles with `--limit` halves
(bundle excludes already-ok rows, so export the second AFTER importing the
first, or slice the source into two staging tables). Each box runs its own
job dir; import both — pending recomputation keeps them disjoint afterwards.

### Rules that bite if forgotten

- **Env var name in job.toml is just a pointer.** The bundle stores
  `base_url_env` (e.g. `LLAMACPP_REMOTE_BASE_URL`); the remote sets that var
  to whatever server it actually has — LM Studio on loopback is fine, the
  protocol (`openai`) is what matters.
- **Custom doc_fn is NOT shipped.** Bundle only carries `--doc-col` columns
  (joined by `\n`). Cross-row or computed docs: materialize into a staging
  raw table first (pattern: `raw.dims2jd_docs(row_key pk, doc)`), bundle that.
- **Source must be a plain table** — no `where=`/run_id filter. Same fix:
  staging table.
- **Params don't travel either.** Pass `--temperature/--max-tokens/--timeout`
  to run-bundle to match the original run's `set_config`.
- **Claim rows written at bundle time.** `bundle` now inserts one `status='pending'` row per shipped `row_key` into `gen."<prompt>"`. This means: (1) **raw `SELECT *` on a gen table will show `pending` rows** — that's expected, they're invisible to `from_gen(latest_ok=True)` readers; (2) a **second concurrent bundle** for the same prompt will skip claimed rows (exclusion is now `status IN ('ok','pending')`); (3) `clean --yes` **releases** `'pending'` claims for the job before deleting the dir. If the bundle is abandoned without importing, `trawler clean <JOB_ID> --force --yes` releases the claims and frees the slots.
- **Pending = source minus ALL ok/pending rows in `gen.<prompt>`** — so
  bundle → run → import → re-bundle converges to `row_count = 0`. Re-bundling
  after a partial run exports only what's still missing. **Manual dedup of the
  source table before bundling is never needed and wastes work** — bundle's
  exclusion query (`status IN ('ok','pending')`) always does it for you.
- **`pull` checks if the job is still running before rsyncing** — a
  `queue/active/<job-id>.task` or a live `run-bundle` process on the box means
  the pull is a partial snapshot. It prints a `WARNING: ... still RUNNING ...`
  to stderr and proceeds anyway (never blocks); `import` reuses this same
  check — a partial pull of a still-live job is marked `running`, a partial
  pull of a confirmed-stopped job is marked `partial` (never `interrupted`,
  which is reserved for a job confirmed parked in `queue/interrupted/`). A
  later re-pull/re-import picks up more rows either way.
- **`enqueue`/`push` refuse a RUNNING job without `--force`** (2026-07-14,
  added after a duplicate-queue-entry incident) — same liveness check as
  `pull`, but blocking: re-enqueueing (or re-pushing job.sqlite over) a job
  that never actually stopped risks the job ending up both `active` and
  `waiting` in the queue. `trawler enqueue <JOB_ID> --force` bypasses it.
- **`trawler job-config <JOB_ID>`** recovers the exact bundle recipe (prompt,
  decoder, model_type, source, pk, doc_cols) an operator/agent needs to
  re-bundle identically — no more hand-reading `job.toml` or raw SQL. It
  prints a ready-to-copy `trawler bundle ...` line and falls back to
  `gen._gen_log` (`config->>'job_id'`) once the local job dir is gone
  (e.g. after `trawler clean`).
- **`trawler rebundle <PROMPT>`** is the preferred way to re-ship a prompt —
  it finds the prompt's latest job for you (no need to already know a job
  id) and resolves the recipe the same way `job-config` does, so an agent
  never hand-reconstructs `--source`/`--doc-col` (which is how staging-table
  pipelines get poisoned with wrongly-formatted inputs). `--go` bundles for
  real; without it, it's a zero-side-effect preview. Incomplete recipes
  (legacy log rows) exit 1 naming the missing fields rather than guessing.
  "Latest" always excludes `stage='cleaned'` rows, so a cleaned/abandoned job
  is never re-resolved even if it's the newest by timestamp (bug found live
  2026-07-14 — the exclusion was missing on first ship; regression-tested).
- **`trawler clean --yes` now stamps `stage='cleaned'`** on the job's
  `gen._gen_log` row (best-effort, same DB-unreachable-then-warn pattern as
  claim release). This keeps deleted job dirs from showing up forever as
  ghost "pending" rows in `trawler jobs`/`status`; `trawler jobs --all`
  still shows them for full-history auditing. Rows cleaned **before** this
  feature shipped were never stamped — if `rebundle`/`jobs` surfaces an old
  ghost, stamp it by hand once:
  `UPDATE gen._gen_log SET config = config || '{"stage":"cleaned"}'::jsonb WHERE name = '<job-id>'`.
- **run-bundle is resumable and early-stops** (default 10 consecutive
  failures — dead server fails fast). Just re-run the same command.
- **Preemption costs only the in-flight rows.** run-bundle commits per row;
  a preempted (or interrupted, or killed) pass loses just the rows still in
  flight, which rerun on resume. Preemption latency ≈ one watchdog tick (60s).
  The watchdog kills the actual `run-bundle` process by cmdline match, not
  just `$!` of its logging pipeline — killing only the pipeline's last stage
  leaves run-bundle running and freezes the queue on `wait` (hit live
  2026-07-13; fixed, smoke-guarded in `smoke_priority.sh`).
- **The running queue is old code until its tmux session restarts.** enqueue
  scp's the current remote_queue.sh/remote_watchdog.sh, but a queue runner
  (and watchdog) already running keeps executing the version it started with.
  Deploy sequence (proven 2026-07-13; safe — per-row commits):
  ```sh
  # NOTE: tmux is NOT on PATH over non-interactive ssh (macOS boxes:
  # /opt/homebrew/bin/tmux). A bare `ssh box tmux ...` "succeeds" with
  # "no session" while the queue keeps running — always use the full path.
  ssh <box> '/opt/homebrew/bin/tmux kill-session -t trawler-queue-<remote-name>;
             pkill -f run-bundle; rm -f <jobs-dir>/queue/active/*.task'
  ssh <box> 'pgrep -fl "remote_queue|remote_watchdog|run-bundle" || echo all-clear'
  trawler enqueue <URGENT_JOB> -p 1      # restarts the queue runner
  trawler enqueue <BACKFILL_JOB> -p -1   # waits behind it
  trawler queue                          # verify active/waiting + prios
  ```
  (Old-format waiting/cooling task files parse via a legacy fallback, but
  re-enqueueing refreshes them properly.)
- **import** creates the gen table if missing, parses `'j'` prompts into
  `json_output`, maps `fail → failed`. `carry` is NULL on imported rows.
  The `run_id` is assigned at **bundle** time and **reused** through import
  (import UPDATEs the same `gen._gen_log` row) — it is NOT new per import.
- **import status = complete only if all rows ran.** If fewer than
  `job_meta.row_count` rows were attempted (partial pull), the job is marked
  **`running`** (still live on the remote — checked the same way `pull`
  checks) or **`partial`** (confirmed not live); either way it stays in
  `trawler jobs` and is skipped by `clean --imported`. **`interrupted`** is
  reserved for a job confirmed parked in `queue/interrupted/` (actually
  stopped — watchdog exit 2, or a local `^C`), set by the write-back sync on
  `status`/`pull`, not by import. Re-import after more rows ran updates the
  same row (any of `exported`/`running`/`partial`/`interrupted`); only a
  `complete` job needs `--force` to re-import.
- **Downstream readers must union runs.** A prompt can hold several run_ids
  (a live run + an offload run, or multiple import cycles); a consumer
  filtering on one run_id goes stale. Pattern (used in
  `finetune/dims2jd/gen_dataset.py`):
  `SELECT DISTINCT ON (row_key) ... WHERE status='ok' ORDER BY row_key, created_at DESC`.
- **Concurrency: match the server.** llama.cpp with `-np 8` → `--concurrency 8`;
  LM Studio / MLX largely serializes → `--concurrency 1-2` (more just queues).
- **MLX vs GGUF**: LM Studio models on the Mac Studio may be MLX format;
  llama.cpp needs GGUF. Check `~/.lmstudio/bin/lms ls` before planning to
  swap servers. Changing the model/quant mid-dataset changes the data
  distribution — ask the user first.

### Two people, one GPU box

The box itself is set up ONCE (SSH, uv, model server — see `INIT.md`).
Nothing there needs to happen twice. What differs per person is purely
CLIENT-SIDE config: each person runs their own queue against the same box,
from their own `.env` (gitignored, nothing to coordinate in the repo).

- Pick a `REMOTE_NAME` per person — any two distinct labels work, it does
  NOT need to be your username (e.g. `studio-1` / `studio-2`, see
  `.env.example`). It's just a local string that gets namespaced into the
  queue's tmux session (`trawler-queue-<name>`) so two queue runners on the
  same box never collide.
- Each person's `_JOBS` dir must differ (`~/trawler-jobs-1` vs
  `~/trawler-jobs-2`) — job dirs, `queue/`, and `queue.log` all live under
  it, so this alone keeps task files, active/done/cooling state, and per-job
  tmux sessions (`trawler-<job-id>`) fully separate.
- Only the box's `_SSH` host is actually shared. Split `_WORKERS` between
  you so both totals stay under the server's real capacity — nothing
  enforces this automatically, it's a manual sizing agreement.
- **If two people accidentally pick the same `REMOTE_NAME`**, `enqueue` /
  `status` / `queue` detect it: the tmux session already exists, but a
  `pgrep` check confirms whether it's actually watching *your* `_JOBS` dir.
  If not, `enqueue` prints `WARNING: ... NAME collision ...` and your job
  will not run; `status`/`queue` show `runner: UP (NAME COLLISION...)`. Fix
  by picking a different `REMOTE_NAME` and re-enqueueing — no data is lost,
  the job just sits unqueued until then.
- **One-time step when upgrading an already-running box to this scheme**:
  rename the existing live session before deploying the new code, or the
  next `enqueue`/`status` will look for `trawler-queue-<name>` under the new
  naming, not find it, and try to start a second runner over the same queue
  dir:
  `ssh <box> '/opt/homebrew/bin/tmux rename-session -t trawler-queue trawler-queue-<remote-name>'`
  (zero interruption to the job already running inside it).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Package metadata name 'trawler' does not match` in a consumer repo | pyproject still names `rowinfer` — dep + `[tool.uv.sources]` must both say `trawler` |
| `bad interpreter ... .venv/bin/python3` | repo folder was moved/renamed — `rm -rf .venv && uv sync` |
| bundle dies `InFailedSqlTransaction` | fixed (rollback after missing-gen-table probe); if seen again, the regression test is `tests/test_offload_loop.py::test_bundle_rolls_back_after_missing_gen_table` |
| run-bundle: every row ParseError "empty output" | thinking model ate the token budget — raise `--max-tokens` (4000+) |
| run-bundle: `env var X not set` | export the `base_url_env` named in job.toml on THAT machine |
| import: `already imported` | intentional guard — `--force` only if you mean it |
| external curl to 10.1.2.47:4160 fails but server "running" | LM Studio bound to loopback — that's what the offload loop is for |

## Where knowledge goes (so this file stays true)

Feature loop (CLAUDE.md) applies: code + SKILL.md + sync in one commit.
Painful surprises → this manual's Troubleshooting table or EVOLVE.md log.
New use case → a `templates/` skeleton, not a chat explanation.
