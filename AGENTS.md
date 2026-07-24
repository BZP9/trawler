# Agent Memo — Adding Features to Trawler

Checklist for future me when adding any non-trivial feature.

For *finding* work (improvement/audit/evolution passes) rather than executing
a known feature, read `EVOLVE.md` — it owns the audit procedure
(`uv run python scripts/audit.py`), smell heuristics, and the evolution log.

---

## Adding a new pipeline type (new BaseRun subclass)

- [ ] Set `LOG_TABLE` and `OUT_SCHEMA` class vars
- [ ] Implement all abstract methods: `_out_table_name`, `_out_table_cols`, `pre_run_check`, `post_run_check`, `pre_step`, `step`, `post_step`
- [ ] Override `_extras_floor` — anything that must survive a `post_step` raise (raw output, carry cols, doc, etc.)
- [ ] Override `_extra_log_cols` if the log table needs extra cols at register time
- [ ] Override `_build_snapshot` if the config snapshot needs subclass-specific fields
- [ ] Raise `ConfigError` (not `RuntimeError`) in setters and `pre_step` for setup/env/cfg problems
- [ ] Add a new `__init__.py` for the subpackage folder
- [ ] Export the new class from `trawler/__init__.py`
- [ ] Add a skill entry in `SKILL.md` with a tight one-line trigger

## Adding a new error category

- [ ] Subclass `RowInferError` in `errors.py`
- [ ] Export it from `trawler/__init__.py`
- [ ] Add it to the error table in `SKILL.md` (trawler-backbone skill)
- [ ] Add it to the error table in `run/README.md`
- [ ] Wire the raise site in `clients.py` or the relevant pipeline step

## Adding a new protocol (new LLM or embed provider)

- [ ] Add client function in `model/clients.py` following the existing signature
- [ ] Register in `CHAT_CLIENTS` or `EMBED_CLIENTS` dict
- [ ] Add seed row to `_SEED_MODEL_TYPES` in `init.py` if it's a common transport
- [ ] Document in `model/README.md` protocol table
- [ ] Update `table/README.md` cfg.model_type seed rows table
- [ ] Update `SKILL.md` trawler-enc or trawler-dec protocol table

## Adding a new cfg table column

- [ ] Add column to `table/sql/init.sql` with `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (keep idempotent)
- [ ] Add to the relevant `cfg.py` upsert function signature + SQL
- [ ] Update cfg table docs in `table/README.md`
- [ ] Update `SKILL.md` trawler-cfg API block if the param is user-facing

## Adding a new tool function (cfg / inspect / query)

- [ ] Match DSN pattern: `dsn: str | None = None` + call `trawler.dsn.resolve_dsn(dsn)` for env fallback
- [ ] Use `psycopg.connect(dsn, row_factory=dict_row)` as context manager
- [ ] Validate string params against a frozenset before interpolating into SQL (injection guard)
- [ ] Export from the module's natural namespace (no need to touch `__init__.py` — modules already exported)
- [ ] Add to the relevant skill block in `SKILL.md`

## Changing the log table schema

- [ ] Add `ALTER TABLE … ADD COLUMN IF NOT EXISTS` to `init.sql` (never DROP or rename — breaks older DBs)
- [ ] Update `_register_run` or `_extra_log_cols` in the relevant class
- [ ] Update `_build_snapshot` if the new col should be in the config jsonb
- [ ] Update log table docs in `generate/README.md` or `encode/README.md`
- [ ] Update `table/README.md` log table section

## Skills — what they are and why they exist

Trawler is a library. Skills in `SKILL.md` exist so **users** can load them into Claude Code globally and call agents to help set up pipelines, manage cfg, inspect runs, etc. — without reading source code. Each skill covers one task type with no cross-references so it loads lean.

Skills live in `~/.claude/skills/` globally (not in the repo). `scripts/sync_skills.py` is the user-facing install command — it splits `SKILL.md` into individual `~/.claude/skills/trawler-<name>/SKILL.md` folders. Users re-run it after pulling updates.

## Feature completion loop (every feature, in order)

1. Finish code changes
2. Check `SKILL.md` — does the feature change any skill's API, trigger, or scope?
3. If yes → update the relevant skill block(s) in `SKILL.md` **before** committing
4. Commit (skill update in same commit as feature)
5. Push → post-push hook auto-syncs your local `~/.claude/skills/` as a convenience

The hook is for the repo maintainer only. Users get updated skills by pulling and re-running `python3 scripts/sync_skills.py`.

**One-time hook setup** (per clone):
```sh
bash scripts/setup_hooks.sh
```

If the hook isn't installed, sync manually:
```sh
python3 scripts/sync_skills.py
```

---

## Touching the offload loop (bundle / runner / importer / scripts)

- [ ] One `gen._gen_log` row per job: bundle INSERTs `status='exported'`; import UPDATEs it to complete — don't add a second insert path
- [ ] New job.toml field → write in `_write_bundle`, read in `_load_job` (runner) AND importer if relevant, plus a roundtrip test
- [ ] run-bundle must stay runnable WITHOUT Postgres — no DB imports at runner module top level beyond what exists
- [ ] New status value → widen the CHECK in `table/sql/init.sql` AND note the live-DB ALTER in the commit message
- [ ] Scripts: no hardcoded hosts/paths — resolve via `scripts/remote_env.sh` (.env named remotes); update `.env.example` for new vars
- [ ] `.env` is auto-loaded on BOTH sides: shell via `remote_env.sh`, python via `trawler.env.load_env()` (called in `cli.main`, `init.main`, `BaseRun.__init__`). Exported vars win. A new config var just needs a `.env.example` entry — no `set -a; source` and no new load call
- [ ] Remote-shell string interpolation: `$REMOTE_JOBS` is a literal `~/...`; the remote shell won't tilde-expand it **inside double quotes** (e.g. a `pkill -f "...pattern"`). Match on the job id, or use an unquoted assignment where expansion is needed — verify against the real `ps` cmdline (absolute path)
- [ ] Watchdog↔queue exit-code contract: watchdog `0`=complete→`done/`, `1`=stalled→`cooling/`, `2`=interrupted→`interrupted/`, `3`=preempted→requeued to waiting. Changing an exit code means updating `remote_queue.sh` routing AND the smoke scripts (`smoke_interrupt.sh`, `smoke_priority.sh`)
- [ ] Task-file metadata (lines 4+) is labeled `key=value` (`prio=`, `retry=`, `retry_at=`) — never positional; keep the legacy line-4/5 fallback in `_meta` until no pre-2026-07 task files remain on any box
- [ ] After editing `remote_queue.sh`/`remote_watchdog.sh`, run `bash scripts/smoke_interrupt.sh` AND `bash scripts/smoke_priority.sh` (local, no remote); remember the box runs the code **shipped at last enqueue** — a running watchdog/queue is old code until its tmux session is restarted
- [ ] Update MANUAL.md runbook + trawler-offload skill block in the same commit

## General hygiene every time

- [ ] Run the feature completion loop above — code → check skill → update if needed → push
- [ ] `table/__init__.py` must exist for `importlib.resources.files("trawler.table.sql")` — don't delete it
- [ ] All setter errors → `ConfigError`, not `RuntimeError` or bare `Exception`
- [ ] New subpackage folder → needs its own `__init__.py`
- [ ] New public class or exception → add to `__all__` in `trawler/__init__.py`
- [ ] Upserts always `ON CONFLICT (name) DO UPDATE SET … updated_at=now()`
- [ ] No raw SQL string interpolation of user-supplied table/col names without frozenset validation
- [ ] Run `git add` by file, not `git add -A` (avoid committing .env, __pycache__ stragglers)
