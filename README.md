# Trawler (package: trawler)

Row-by-row inference control plane. Batch LLM generation and embedding pipelines with structured hooks, categorized errors, and Postgres-backed run logs. Compute is pluggable: local/remote model servers, paid APIs, or the **offload loop** — bundle a job to a portable dir, run it on any machine (no Postgres there), import results back. Pip-installable; designed to be packaged as tools and skills for a code agent.

Docs by tier: `SKILL.md` (per-task reference) → `templates/` (copy-fill skeletons) → `MANUAL.md` (end-to-end workflows + environment runbook) → source. First time setting up a machine or a remote GPU box? Start at `INIT.md`.

## Skills loaded after `sync_skills.py`

| skill | trigger |
|-------|---------|
| `trawler-dec` | writing or running a LLM generation pipeline (text or JSON output) |
| `trawler-enc` | writing or running an embedding pipeline |
| `trawler-cfg` | adding/editing/querying prompts, decoders, encoders, or model_types |
| `trawler-inspect` | checking run status, errors, progress, failed rows after a run |
| `trawler-query` | reading output table rows, listing tables, checking table sizes |
| `trawler-raw` | creating raw schema tables, loading data from CSV / JSONL / another DB table |
| `trawler-db-init` | first-time DB setup or checking schema DDL |
| `trawler-offload` | exporting a job to another machine (no Postgres there), running it, importing results back |
| `trawler-backbone` | subclassing BaseRun to build a new pipeline type from scratch |
| `trawler-evolve` | auditing the repo for improvements; self-improvement pass (`EVOLVE.md` + `scripts/audit.py`) |

## Install

```sh
uv sync
trawler-init --dsn jdbc:postgresql://localhost:5432   # one-shot DB setup
export TRAWLER_DSN=postgresql://localhost:5432/trawler
python3 scripts/sync_skills.py                         # load Claude Code skills globally
```

`sync_skills.py` splits `SKILL.md` into individual files under `~/.claude/skills/trawler-*/`. Re-run whenever you pull new changes to pick up updated or new skills.

This assumes Postgres and `.env` already exist. Starting from a bare machine, or want to add a remote GPU box to run offload jobs on? See `INIT.md`.

## Layout

```
src/trawler/
├── __init__.py           # top-level exports: run classes, source helpers, cfg/inspect/query modules
├── errors.py             # categorized exceptions (ConfigError / EndpointError / BudgetError / ...)
├── dsn.py                # resolve_dsn() helper: TRAWLER_DSN > ROWINFER_DSN (back-compat) > ConfigError
├── init.py               # trawler-init CLI: CREATE DB + schemas + seed cfg.model_type
├── source.py             # from_db / from_csv / from_jsonl
├── cfg.py                # cfg table CRUD (upsert / list / get / delete for all 4 cfg tables)
├── inspect.py            # run log inspection (list_runs / get_run / run_stats / failed_rows)
├── query.py              # output table reads + schema discovery (get_output / list_tables / table_row_counts)
├── cli.py                # trawler CLI: bundle/enqueue/status/queue/import/... (offload front door)
├── offload/              # portable job dirs: bundle export, remote runner, importer
├── run/                  # BaseRun lifecycle backbone
├── generate/             # TextGenRun + JsonGenRun — LLM call → gen schema
├── encode/               # MinimalEncodeRun — embedding → enc schema
├── model/                # protocol clients (ollama / openai / anthropic / sentence_transformers)
└── table/                # init.sql + schema/naming documentation

templates/                # runnable skeleton per use case (see templates/README.md)
scripts/                  # implementation behind the CLI's remote verbs (offload.sh,
                          # remote_status/queue/watchdog.sh) + sync_skills.py, audit.py
```

| module | role |
|--------|------|
| `cfg.py` | upsert / read / delete rows in cfg.system_prompt, cfg.decoder, cfg.encoder, cfg.model_type |
| `inspect.py` | query run logs: list recent runs, check status, stats by error_category, fetch failed rows |
| `query.py` | read output tables (gen/enc), list tables in a schema, row counts |
| `run/` | `BaseRun` — shared lifecycle: pre-flight, log register, per-row loop, error categorization, finalize |
| `generate/` | `MinimalGenRun` scaffold + `TextGenRun` + `JsonGenRun` |
| `encode/` | `MinimalEncodeRun` — text → vector(dim) via ollama / openai / sentence-transformers |
| `model/` | clients + `DecoderConfig` / `EncoderConfig` / `ResolvedEndpoint` dataclasses |
| `table/` | `init.sql` — idempotent DDL for all schemas and log tables |
| `offload/` | `bundle()` (export + register job), `run_bundle()` (execute anywhere, resumable, early-stop), `import_bundle()` (merge back, completes the job's log row) |

## Quick start

### Manage cfg (no SQL needed)

```python
import trawler

# add a prompt
trawler.cfg.upsert_system_prompt(
    "extract_skills",
    content="Extract skills as JSON array...",
    expected_output="j",
)

# add a model
trawler.cfg.upsert_decoder("gemma4_31b", repo_name="google/gemma-4-31b-it")

# add an encoder
trawler.cfg.upsert_encoder("jd2vector", repo_name="all-MiniLM-L6-v2", dim=384)

# view all prompts
trawler.cfg.list_cfg("system_prompt")
```

### Run a pipeline

```python
import trawler

gen = trawler.JsonGenRun()
gen.set_model("gemma4_31b")
gen.set_model_type("remote_lms")
gen.set_system_prompt("extract_skills")
gen.set_data_source(trawler.from_db("jobs"), source_uid="id")
gen.set_config(temperature=0.0, max_tokens=2000)  # timeout=... sets HTTP timeout (s)
gen.set_limit(5)
gen.set_retries(2)          # retry transient endpoint errors, backoff doubling
run_id = gen.run()
```

### Inspect results

```python
import trawler

# recent runs
trawler.inspect.list_runs("gen", limit=10)

# stats for a run
trawler.inspect.run_stats(run_id)
# → {status, n_done, n_failed, pct_done, out_table, by_category: {...}}

# failed rows with error details
trawler.inspect.failed_rows(run_id)

# read output
trawler.query.get_output("gen.extract_skills", run_id=run_id, status="ok")

# what tables exist and how big
trawler.query.list_tables("gen")
trawler.query.table_row_counts("gen")
```

## Cfg-backed config

All cross-cutting config lives in Postgres `cfg` schema. Add a model = INSERT row via `cfg.upsert_decoder()`. No code change.

| table | role |
|-------|------|
| `cfg.system_prompt` | prompt content + `expected_output` ('t' text / 'j' JSON) |
| `cfg.decoder` | LLM model rows (name + repo_name + optional format) |
| `cfg.encoder` | embedding model rows (+ dim) |
| `cfg.model_type` | transport profiles: protocol + base_url_env + api_key_env |

Model and transport are decoupled: same `gemma4_31b` can run via `local_ollama` one run, `remote_lms` another.

## Pipeline flow

```
set_*() validates early → run()
  ├─ pre_run_check
  ├─ pre-flight (dry first row before register)
  ├─ _register_run  →  _gen_log / _enc_log row
  ├─ iter data_source:
  │     pre_step → step → post_step
  │     per-row error_category on failure, batch continues
  │     _bump_progress(n_done, n_failed) per row
  └─ finalize (complete / failed / interrupted / early_stopped)
```

## Offload loop (compute anywhere) — the `trawler` CLI

One front door for the whole loop. Remote verbs take `-r <name>` (default:
first remote in `TRAWLER_REMOTES`); remotes are configured in `.env`
(copy `.env.example`): `TRAWLER_REMOTES=a,b` + `TRAWLER_REMOTE_<NAME>_SSH/_JOBS/_URL/_MODELS/_WORKERS`.

```sh
# 1. export a job from Postgres → output/jobs/<job-id>/
trawler bundle --prompt P --decoder M --model-type MT \
  --source raw.t --pk id --doc-col doc

# 2. hand it to the remote's queue and walk away
#    (pushes the dir, queues it; the box runs jobs FIFO at _WORKERS
#     concurrency under a watchdog, auto-retries stalls every 30 min)
trawler enqueue <job-id> --retries 2 --max-tokens 4000

# 3. watch
trawler status                 # newest job: progress, rate, ETA, queue health
trawler status <job-id>        # specific job        (--all: every remote)
trawler queue                  # queue only: active / waiting / cooling / done
trawler jobs                   # control-plane view from gen._gen_log

# 4. bring results home (pulls job.sqlite, merges, completes the log row)
trawler import <job-id>
```

Occasional verbs:

```sh
trawler push <job-id> --with-repo    # re-sync trawler code to the box
trawler run  <job-id> [args...]      # start directly, skip the queue
trawler pull <job-id>                # fetch job.sqlite without importing
trawler models                       # weights + running servers on the box
trawler fetch-model <hf-repo> <file> # resumable GGUF download onto the box
trawler run-bundle <dir> [args...]   # what the remote itself executes
```

Everything is resumable: `import` a partial run anytime; re-`bundle` exports
only rows not yet ok. Full runbook: `MANUAL.md`.

## Error categories

Caught per row; `error_category` col written with exception class name:

| category | meaning |
|----------|---------|
| `ConfigError` | setup / env / cfg problem |
| `EndpointError` | 5xx, 429, timeout, DNS — transient; auto-retried via `set_retries(n)` |
| `ProtocolError` | other 4xx, malformed response, empty content |
| `BudgetError` | `finish_reason='length'` or reasoning model hit token limit |
| `ParseError` | post_step couldn't parse LLM output |

Query failed rows by type: `WHERE error_category='BudgetError'`.

## Env vars

Machine-specific values live in `.env` (gitignored; documented template: `.env.example`). Model-endpoint names are declared per `cfg.model_type` row (`base_url_env` / `api_key_env`); the values are plain env vars:

```sh
export TRAWLER_DSN=postgresql://localhost:5432/trawler   # ROWINFER_DSN also accepted (back-compat)
export OLLAMA_LOCAL_BASE_URL=http://localhost:11434
export OLLAMA_REMOTE_BASE_URL=http://10.x.x.x:11434
export LMS_LOCAL_BASE_URL=http://localhost:1234/v1
export LMS_REMOTE_BASE_URL=http://10.x.x.x:1234/v1
export LLAMACPP_REMOTE_BASE_URL=http://10.x.x.x:8080/v1
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=sk-...
export ANTHROPIC_BASE_URL=https://api.anthropic.com      # bare host, no /v1
export ANTHROPIC_API_KEY=sk-ant-...
```
