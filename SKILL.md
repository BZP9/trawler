# Trawler Skills

Minimal skill index. Each skill covers one task type. Load the matching one — skip the rest.

Guideline tiers: **skill** (concepts, here) → **template** (`templates/` — runnable skeleton per use case, copy + fill placeholders) → **raw code** (`src/trawler/`). Prefer a template over writing a pipeline from scratch; see `templates/README.md` for the index.

End-to-end workflows + environment facts (Mac Studio ssh, LM Studio loopback, offload runbook, troubleshooting): `MANUAL.md` at repo root.

---

## skill: trawler-backbone

**Trigger**: subclassing BaseRun to build a new pipeline type from scratch

**Scope**: `src/trawler/run/base.py`, `src/trawler/errors.py`

### Design pattern

Trawler is a **cfg-in-DB, code-in-subclass** pattern. Config (models, prompts, transports) lives in Postgres `cfg` schema. Python subclasses only own per-row logic (`pre_step` / `step` / `post_step`). The backbone owns everything else.

### Lifecycle (one impl, shared by gen + enc)

```
set_*()             # each setter validates immediately (DB lookup + env resolve)
run()
  ├─ pre_run_check()            # assert all required attrs set; resolve carry_cols
  ├─ _build_snapshot()          # freeze config into dict for log
  ├─ _pre_flight()              # dry first row BEFORE register (if preflight=True)
  │                             # catches Budget/Endpoint/Protocol/Parse upfront
  ├─ _ensure_out_table()        # CREATE TABLE IF NOT EXISTS out_table
  ├─ _ensure_column("error_category", "text")
  ├─ _register_run()            # INSERT into LOG_TABLE → assigns run_id
  │
  ├─ for row in data_source (up to limit):
  │     _do_row(row)
  │       ├─ pre_step(row)  → payload
  │       ├─ step(payload)  → raw output
  │       ├─ post_step(row, raw) → extras dict
  │       │   [any exception → status=failed, error=traceback, category=type(e).__name__]
  │       ├─ _extras_floor(row, raw)  merged BEFORE extras (floor survives post_step raise)
  │       └─ _write_row(row_key, status, error, merged_extras)
  │     _bump_progress(n_done, n_failed)   # UPDATE log row, throttled ~1 per 2s; exact at finalize
  │
  ├─ post_run_check()
  └─ finally: _finalize(status)    # always — Ctrl-C / exception / normal exit
```

### Subclass contract

| classvar | what to set |
|----------|-------------|
| `LOG_TABLE` | `"gen._gen_log"` or `"enc._enc_log"` |
| `OUT_SCHEMA` | `"gen"` or `"enc"` |

| abstract method | purpose |
|-----------------|---------|
| `_out_table_name()` | return output table name (no schema prefix) |
| `_out_table_cols()` | return `{col: sql_type}` for extra cols beyond backbone cols |
| `pre_run_check()` | assert required attrs; set defaults |
| `post_run_check()` | any post-loop validation |
| `pre_step(row)` | build call payload from row |
| `step(payload)` | make the LLM/embed call; return raw output |
| `post_step(row, raw)` | parse raw → return extras dict |

### Override hooks (optional)

| hook | default | when to override |
|------|---------|------------------|
| `_extras_floor(row, raw)` | `{}` (BaseRun) — subclasses save raw_output + doc + carry | guarantee fields survive post_step raise; merged before extras |
| `_extra_log_cols()` | `{source_table, source_run_id}` | extend with `{**super()._extra_log_cols(), ...}` — base already writes source provenance |
| `_build_snapshot()` | model + endpoint + params + source_uid + source_table + source_run_id + limit | extend with subclass-specific fields |
| `_fmt_payload(payload)` | `""` | return short string shown as `pre:` hint before step(); enc/dec override to show doc snippet |
| `_fmt_raw(raw)` | first 150 chars of str(raw) | return short string shown after step(); enc overrides to show `vec[dim]` |
| `_fmt_post(extras)` | `""` | return short string shown after post_step(); JsonGenRun overrides to show parsed keys |

### Runtime output

Every run logs per-row `pre:`/`raw:`/`post:` lines + periodic `[eta]` to stdout via the `trawler` logger (`set_verbose(False)` silences). Reading the output:

- `pre:` prints **before** the model call — a hang after it is model/network, not code.
- `[eta]` every 10 rows: `recent` = mean latency of last 20 rows; `wall/row` = elapsed ÷ done and drives the ETA. `recent` ≫ `wall/row` → endpoint slowing; with concurrency, `wall/row` ≈ `recent`/workers is normal.
- Row counters (`#430/1000`, `done=`) are **cumulative across passes**: on resume, prior-ok rows count toward both sides; already-ok rows are skipped silently and never advance the counter.

### Error categories

Per-row exceptions caught in `_do_row`; `type(e).__name__` → `error_category` col. Batch continues on any of these. Only un-caught exceptions (DB down, OOM) break the whole run.

| class | meaning | who raises |
|-------|---------|------------|
| `ConfigError` | setup / env / cfg problem | setters, pre_step |
| `EndpointError` | 5xx / 429 / timeout / DNS — transient; auto-retried when `set_retries(n)` set | clients.py |
| `ProtocolError` | other 4xx / empty / malformed response | clients.py |
| `BudgetError` | finish_reason=length or reasoning model hit limit | clients.py |
| `ParseError` | post_step couldn't parse output | JsonGenRun.post_step |

### Key invariants

- Setters validate at call time → typos fail before `run()` registers anything.
- `_pre_flight` runs first row dry → config/budget/endpoint errors surface before any log row is written. On success the result is reused as row 1's output — no double model call.
- `step`/`step_batch` retried on `EndpointError` only when `set_retries(n, backoff)` is set; delay doubles per attempt.
- DB writes go through a connection pool for the duration of `run()`; progress UPDATEs throttled to ~1 per 2s, exact counts written at finalize. Missing `psycopg_pool` (stale env) → warning + per-call connection fallback, never a crash.
- `_register_run` happens before the loop → every run has a log row.
- `_finalize` in `finally:` → status always written (complete / failed / interrupted / early_stopped).
- `_extras_floor` merged before `extras` → floor fields persisted even if `post_step` raises.
- `config` snapshot frozen at register → reproducible even if cfg rows change later.

---

## skill: trawler-dec

**Trigger**: writing or running a LLM generation pipeline (text or JSON output)

**Scope**: `src/trawler/generate/gen.py`, `text_gen.py`, `json_gen.py`, `src/trawler/source.py`

### Ask upfront (before any exploration)

If any of these three are missing from the request, ask immediately — do not explore:

1. **Source table + uid col** — e.g. `raw.jd`, uid=`caid`
2. **doc_fn col(s)** — single col name, list of cols, or callable description
3. **Model + model_type** — e.g. `gemma4-31b` / `remote_lms`

Template the user should provide:
> "Build a `<TextGen|JsonGen>` pipeline. Source: `<schema.table>`, uid=`<col>`, doc col=`<col>`. Model: `<model>`/`<model_type>`. Prompt: `<prompt_name>`."

Once you have all three, run exactly these cfg confirms (fast, necessary) then write:

```bash
psql $TRAWLER_DSN -c "SELECT name, expected_output FROM cfg.system_prompt WHERE name = '<prompt_name>';"
psql $TRAWLER_DSN -c "SELECT name FROM cfg.decoder WHERE name = '<model>';"
psql $TRAWLER_DSN -c "SELECT name FROM cfg.model_type WHERE name = '<model_type>';"
```

Load `trawler-cfg` to register models/prompts if missing. No other exploration needed.

**Missing env var?** `echo $TRAWLER_DSN` returns empty → run `grep -E 'TRAWLER_DSN|ROWINFER_DSN' ~/.zshrc` first. Also accepted: `ROWINFER_DSN` (back-compat). Do not hunt `.env` files.

### Classes

| class | expected_output | post_step |
|-------|-----------------|-----------|
| `TextGenRun` | `'t'` | returns `{}` — raw_output + doc saved via floor |
| `JsonGenRun` | `'j'` | parses output → `{"json_output": Jsonb(parsed)}` — raw_output + doc saved via floor |

Both inherit `MinimalGenRun` which owns setters + default pre/step.

### Setters

| setter | validates |
|--------|-----------|
| `set_model(name)` | `cfg.decoder` row exists → `DecoderConfig` |
| `set_model_type(name)` | `cfg.model_type` exists + env var set → `ResolvedEndpoint` |
| `set_system_prompt(name)` | row exists + `expected_output` matches class's `EXPECTED_OUTPUT` |
| `set_data_source(src, source_uid)` | accepts `RowSource` or any iter; if `RowSource`, records `source_table`/`source_run_id` in log |
| `set_doc_fn(fn)` | **required** — produces the user message sent to LLM |
| `set_config(**params)` | stored as-is; `timeout` (s) sets the per-request HTTP timeout, default 600 |
| `set_carry_cols(list)` | default `[]` = carry nothing; explicit opt-in only |
| `set_limit(n, total=False)` | positive int or `None`. Default caps rows processed THIS pass (resume does `n` more). `total=True` caps cumulative ok rows — resume tops up until the table has `n` ok rows |
| `set_gen_name(name)` | optional; defaults to `f"{system_prompt}-{utc_iso}"` |
| `set_preflight(bool)` | default `True` |
| `set_resume(run_id)` | reuse existing run_id; skip already-ok rows; retry failed/missing; disables preflight |
| `set_early_stop(n)` | stop after `n` consecutive failures; default `10`; `None` to disable |
| `set_concurrency(n)` | run `n` LLM calls in parallel via a thread pool; default `1` (serial). Speeds up HTTP-bound gen runs. `limit` is enforced exactly (in-flight rows counted) |
| `set_retries(n, backoff=2.0)` | retry `step` on `EndpointError` (5xx / 429 / timeout) up to `n` extra times; delay `backoff * 2**(attempt-1)` s; default `0` |
| `set_verbose(bool)` | default `True`; `False` silences per-row console output (log rows still written) |

### Resuming a run

Use when a run failed mid-way (LLM down, timeout, Ctrl-C) or needs a retry after a tweak:

```python
gen = JsonGenRun()
gen.set_model("gemma4-31b")
gen.set_model_type("remote_lms")
gen.set_system_prompt("text2seg")
gen.set_data_source(from_db("raw.jd"), source_uid="caid")
gen.set_doc_fn("text")
gen.set_resume("<previous_run_id>")   # ← same source, same setup, add this
run_id = gen.run()                    # run_id == previous_run_id
```

What happens: prior-ok row_keys are loaded and skipped silently; failed/missing rows are processed; the log row goes back to `running` with `n_done` seeded from prior-ok; finalises as `complete` under the same run_id and output table. `set_limit(n)` = `n` MORE rows this pass; `set_limit(n, total=True)` = top up until `n` ok rows total.

### `set_doc_fn` forms

```python
gen.set_doc_fn("job_description")                       # single col → user message
gen.set_doc_fn(["job_name", "job_note"])                # cols joined by "\n"
gen.set_doc_fn(lambda r: json.dumps({                   # callable, full control
    "title": r["job_name"],
    "body": clean_html(r["job_note"]),
}))
```

`doc_fn` produces the **user message** sent to the LLM. System message comes from `set_system_prompt`.
`_doc_cols` (str/list source cols) are automatically excluded from `carry` resolution.

### Sources

```python
from trawler import JsonGenRun, TextGenRun, from_db, from_csv, from_jsonl, from_enc, from_gen

gen.set_data_source(from_db("jobs"), source_uid="id")                # raw DB table
gen.set_data_source(from_enc("bge-m3", run_id=rid, where="status='ok'"), source_uid="row_key")  # enc output
gen.set_data_source(from_gen("extract_skills"), source_uid="row_key")  # gen output (re-process)
gen.set_data_source(from_csv("data/jobs.csv"), source_uid="id")      # file: also from_jsonl
```

`from_enc` / `from_gen`: `run_id` filter is parameterized (safe). `where` is a raw SQL fragment — developer-owned.

### Usage

```python
gen = JsonGenRun()
gen.set_model("gemma4_31b")
gen.set_model_type("remote_lms")
gen.set_system_prompt("extract_skills")
gen.set_data_source(from_db("jobs"), source_uid="id")
gen.set_doc_fn(["title", "description"])
gen.set_carry_cols(["company", "salary"])
gen.set_config(temperature=0.0, max_tokens=2000)
gen.set_limit(5)
run_id = gen.run()
```

### Output table — `gen.<system_prompt.name>`

| col | notes |
|-----|-------|
| run_id, row_key, status, error, error_category, created_at | backbone |
| raw_output (text) | always saved via floor |
| doc (text) | user message sent to LLM — always saved via floor |
| json_output (jsonb) | JsonGenRun only; null on parse fail |
| carry (jsonb) | cols listed in `set_carry_cols` — single jsonb col, always present |

Query carry: `carry->>'company'`, `(carry->>'salary')::int`.

### Log table — `gen._gen_log`

| col | notes |
|-----|-------|
| run_id, name, model, status, started/ended_at, n_rows, n_done, n_failed, error, config | backbone |
| system_prompt_content | full prompt at register — SQL-side audit without jsonb dig |
| source_table (text) | log id of source: `'enc.bge-m3'`, `'csv:/path'`, etc. — null for bare iters |
| source_run_id (uuid) | upstream run_id — set when using `from_enc`/`from_gen` with `run_id=` |

### JSON coercion (`_coerce_json`)

Tries in order: dict/list passthrough → bytes decode → ` ```json ` fence → `json.loads` → first balanced `{…}`/`[…]` block → `ParseError`.

---

## skill: trawler-enc

**Trigger**: writing or running an embedding pipeline

**Scope**: `src/trawler/encode/encode_run.py`, `src/trawler/source.py`

### Check first

```bash
psql $TRAWLER_DSN -c "SELECT name, dim, repo_name FROM cfg.encoder;"
psql $TRAWLER_DSN -c "SELECT name, protocol, base_url_env FROM cfg.model_type;"
```

Load `trawler-cfg` to register models if missing.

**Missing env var?** `echo $TRAWLER_DSN` returns empty → run `grep -E 'TRAWLER_DSN|ROWINFER_DSN' ~/.zshrc` first. Also accepted: `ROWINFER_DSN` (back-compat). Do not hunt `.env` files.

### Class

`MinimalEncodeRun` — one concrete class, no variants.

```python
LOG_TABLE  = "enc._enc_log"
OUT_SCHEMA = "enc"
```

Output table name = encoder `name` → `enc.<encoder.name>`.

### Setters

| setter | validates |
|--------|-----------|
| `set_model(name)` | `cfg.encoder` row exists → `EncoderConfig` (incl. dim) |
| `set_model_type(name)` | `cfg.model_type` exists; null `base_url_env` OK for local protocols |
| `set_data_source(src, source_uid)` | accepts `RowSource` or any iter; if `RowSource`, records `source_table`/`source_run_id` in log |
| `set_doc_fn(fn)` | **required** — produces text to embed into `doc` |
| `set_config(**params)` | e.g. `normalize=True`; `timeout` (s) sets the per-request HTTP timeout, default 600 |
| `set_carry_cols(list)` | default `[]` = carry nothing; explicit opt-in only |
| `set_limit(n, total=False)` | positive int or `None`. Default caps rows processed THIS pass (resume does `n` more). `total=True` caps cumulative ok rows — resume tops up until the table has `n` ok rows |
| `set_encode_name(name)` | optional; defaults to `f"{model.name}-{utc_iso}"` |
| `set_preflight(bool)` | default `True` |
| `set_resume(run_id)` | reuse existing run_id; skip already-ok rows; retry failed/missing; disables preflight |
| `set_early_stop(n)` | stop after `n` consecutive failures; default `10`; `None` to disable |
| `set_batch_size(n)` | embed `n` docs per call via `step_batch`; default `1`. One HTTP/in-proc call per batch (`sentence_transformers` gets a full GPU batch; ollama/openai send array input). A row whose `pre_step` fails is isolated; a batch-call failure fails all rows in that batch. `limit` is enforced exactly (batches flush early at the limit) |
| `set_retries(n, backoff=2.0)` | retry `step`/`step_batch` on `EndpointError` (5xx / 429 / timeout) up to `n` extra times; delay `backoff * 2**(attempt-1)` s; default `0` |
| `set_verbose(bool)` | default `True`; `False` silences per-row console output (log rows still written) |

### `set_doc_fn` forms

```python
enc.set_doc_fn("description")                            # single col
enc.set_doc_fn(["job_name", "job_note"])                 # cols joined by "\n"
enc.set_doc_fn(lambda r: f"{r['title']}: {r['body']}")  # callable, full control
```

`_doc_cols` (str/list source cols) are automatically excluded from `carry` resolution.

### Protocols

| model_type protocol | function | transport |
|--------------------|----------|-----------|
| `ollama` | `ollama_embed` | POST `{base_url}/api/embed` |
| `openai` | `openai_embed` | POST `{base_url}/embeddings` (base_url must end `/v1`) |
| `sentence_transformers` | `sentence_transformer_embed` | in-process; lazy import; no HTTP |

### Sources

```python
from trawler import MinimalEncodeRun, from_db, from_csv, from_jsonl, from_enc, from_gen

enc.set_data_source(from_db("jobs"), source_uid="id")                # raw DB table
enc.set_data_source(from_gen("extract_skills", run_id=rid, where="status='ok'"), source_uid="row_key")  # embed gen output
enc.set_data_source(from_enc("bge-m3"), source_uid="row_key")        # re-embed; files: from_csv/from_jsonl
```

`from_enc` / `from_gen`: `run_id` filter is parameterized (safe). `where` is a raw SQL fragment — developer-owned.

### Usage

```python
enc = MinimalEncodeRun()
enc.set_model("jd2vector")
enc.set_model_type("local_sentence_transformer")
enc.set_data_source(from_db("jobs"), source_uid="id")
enc.set_doc_fn(["title", "description"])
enc.set_carry_cols(["title", "company"])
enc.set_config(normalize=True)
enc.set_limit(5)
run_id = enc.run()
```

Via HTTP (LMS / OpenAI-compatible):

```python
enc.set_model("nomic_embed")
enc.set_model_type("remote_lms")   # LMS_REMOTE_BASE_URL env must be set
```

### Output table — `enc.<encoder.name>`

| col | notes |
|-----|-------|
| run_id, row_key, status, error, error_category, created_at | backbone |
| vec (vector(dim)) | pgvector embedding |
| doc (text) | embedded text — floor, survives step/post failure |
| carry (jsonb) | cols listed in `set_carry_cols` — single jsonb col, always present |

Query carry: `carry->>'firm_name'`, `(carry->>'salary')::int`.

### Log table — `enc._enc_log`

| col | notes |
|-----|-------|
| run_id, name, model, status, started/ended_at, n_rows, n_done, n_failed, error, config | backbone |
| dim | encoder dimension at register |
| source_table (text) | log id of source: `'raw.jobs'`, `'gen.extract_skills'`, etc. — null for bare iters |
| source_run_id (uuid) | upstream run_id — set when using `from_enc`/`from_gen` with `run_id=` |

### Validation chain

`set_model` → dim known → `post_step` validates `len(vec) == dim` → `ConfigError` on mismatch. Pre-flight runs this full chain before register.

---

## skill: trawler-cfg

**Trigger**: adding/editing/querying prompts, decoders, encoders, or model_types

**Scope**: `src/trawler/cfg.py`

**API**:
```python
trawler.cfg.upsert_system_prompt(name, content, expected_output, description=None)
trawler.cfg.upsert_decoder(name, repo_name, format=None, description=None)
trawler.cfg.upsert_encoder(name, repo_name, dim, format=None, description=None)
trawler.cfg.upsert_model_type(name, protocol, base_url_env=None, api_key_env=None, description=None)
trawler.cfg.list_cfg(table)         # table: 'system_prompt'|'decoder'|'encoder'|'model_type'
trawler.cfg.get_cfg(table, name)
trawler.cfg.delete_cfg(table, name)
```

**Notes**: all upserts are ON CONFLICT DO UPDATE + updated_at=now(). expected_output must be `'t'` or `'j'`. `name` must match `[A-Za-z0-9_-]{1,63}` (it becomes an output table name) — invalid names raise `ValueError` before touching the DB.

---

## skill: trawler-inspect

**Trigger**: checking LOCAL run status, errors, progress, failed rows after a run (reads gen._gen_log / enc._enc_log in Postgres). NOT for remote/offload job status — "remote status", "job status on the box", queue/ETA questions → use trawler-offload's CLI (`trawler status`, `trawler jobs`, `trawler queue`) instead.

**Scope**: `src/trawler/inspect.py`

**`gen._gen_log` columns** (so agents stop guessing): `run_id`, `name`, `model`,
`status`, `started_at`, `n_rows`, `n_done`, `n_failed`, `source_table`,
`system_prompt_content`, `config` (jsonb — offload rows carry `job_id`,
`offload`, `prompt`, `stage`, `remote`, `model_type`, `pk`, `doc_cols`).

**`status` values** — local runs: `running`/`complete`/`failed`/`interrupted`/
`early_stopped`. Offload jobs: `exported` → `running` → `partial` |
`interrupted` → `complete`, plus `cleaned` (ghost row, dir deleted). Liveness
wins over coverage: a partially imported job still running remotely is
`running`, never `partial`. Full model + who-sets-what: trawler-offload skill.
psql is the live status view: `status`/`jobs`/`pull` write fresh
`n_done`/`n_failed`/`status` back after every remote poll (best-effort;
DB-unreachable warns, never fails the command).

**API**:
```python
trawler.inspect.list_runs(schema='gen', status=None, limit=20)
trawler.inspect.get_run(run_id, schema='gen')
trawler.inspect.run_stats(run_id, schema='gen')
# → {status, n_done, n_failed, pct_done, out_table, by_category: {ErrorClass: n}}
trawler.inspect.failed_rows(run_id, out_table=None, schema='gen', limit=100)
# out_table optional — derived from log snapshot if omitted
```

**Copy-paste example** (run verbatim; do NOT invent dict keys, table names, or a sqlite path — the log lives in Postgres):
```sh
uv run python3 - << 'EOF'
from trawler.inspect import list_runs, run_stats, failed_rows

for r in list_runs(limit=10):
    print(f"{r['run_id']}  {r['status']:<12} {r['name']}")

rid = "PASTE-RUN-ID-HERE"
s = run_stats(rid)
print(f"\n{s['name']}: {s['status']}  {s['n_done']}/{s['n_rows']} done "
      f"({s['pct_done']}%), {s['n_failed']} failed → {s['out_table']}")
print("errors by category:", s["by_category"])
for row in failed_rows(rid, limit=5):
    print(f"--- {row['row_key']}\n{row['error']}")
EOF
```
Offload runs (`status='exported'`, name like `<prompt>-20260713T081500Z`) appear here too, but their live progress is on the remote — check with `trawler status`.

---

## skill: trawler-query

**Trigger**: reading output table rows, listing tables, checking table sizes

**Scope**: `src/trawler/query.py`

**API**:
```python
trawler.query.get_output(out_table, run_id=None, status=None, limit=100)
# out_table: 'gen.extract_skills' or bare 'extract_skills' (defaults to gen)
trawler.query.list_tables(schema)          # 'gen'|'enc'|'cfg'|'raw'
trawler.query.table_row_counts(schema)     # {table_name: int}
```

---

## skill: trawler-db-init

**Trigger**: first-time DB setup or checking schema DDL

**Scope**: `src/trawler/init.py`, `src/trawler/table/sql/init.sql`

**Full init sequence** (run once per clone):
```sh
uv sync
trawler-init --dsn postgresql://localhost:5432   # creates DB + schemas + seeds cfg.model_type
export TRAWLER_DSN=postgresql://localhost:5432/trawler   # ROWINFER_DSN also accepted (back-compat)
bash scripts/setup_hooks.sh                       # installs post-push hook → auto-syncs skills on push
python3 scripts/sync_skills.py                    # first sync (hook fires on next push, not this one)
```

**What trawler-init creates**: schemas gen/enc/cfg/raw, log tables `_gen_log` / `_enc_log`, all cfg tables, seeds 7 model_type rows.

**What setup_hooks.sh does**: installs `.git/hooks/post-push` — after every `git push`, SKILL.md is split into individual files under `~/.claude/skills/trawler-*.md` automatically.

**Other trawler-init flags**:
```sh
trawler-init --dsn <dsn> --dbname trawler --no-seed
```

---

## skill: trawler-raw

**Trigger**: creating raw schema tables, loading data from CSV / JSONL / another DB table, setting up PK, multi-source ingestion before a pipeline run

**Scope**: `src/trawler/raw.py`

### When to set pk

**Always confirm the pk column with the user before loading if the table will feed a gen or enc pipeline.**

The raw table pk becomes `source_uid` in `set_data_source()` — it is how the pipeline indexes each row in the output table. Without it, rows have no stable identity across runs.

Decision rule:
- User says "load X to raw" → ask "which column is the unique ID / pk?" if not already stated
- User says "load X to raw, pk is `job_id`" → use `pk="job_id"` in the loader call
- Different sources have different pk column names (`"id"`, `"uid"`, `"sid"`, `"job_id"`, etc.) — set per call, never assume
- **No default pk** — omitting `pk=` creates a keyless table with no error or warning

**Composite PK**: pass a list when the natural key spans multiple columns (e.g. experience rows keyed by `resume_id + sort`):
```python
pk = ["resume_id", "sort"]
trawler.raw.load_from_jsonl("experience", "exp.jsonl", pk=pk)

# pipeline — pass same list to source_uid
gen.set_data_source(trawler.source.from_db("experience"), source_uid=["resume_id", "sort"])
# row_key stored as JSON array: '["r1", "2"]' — collision-safe
```

After loading, the same column name(s) are passed as `source_uid` when setting up the pipeline:
```python
# single pk
trawler.raw.load_from_csv("jobs", "jobs.csv", pk="job_id")
gen.set_data_source(trawler.source.from_db("jobs"), source_uid="job_id")
```

### API

```python
trawler.raw.create_table(table, columns, *, pk=None, if_not_exists=True, dsn=None)
trawler.raw.drop_table(table, *, dsn=None)
trawler.raw.infer_columns(rows)                    # {col: pg_type} from list[dict]

trawler.raw.load_from_csv(table, path, *, columns=None, pk=None, batch_size=500, truncate=False, on_conflict="error", encoding="utf-8", dsn=None)   -> int
trawler.raw.load_from_jsonl(table, path, *, columns=None, pk=None, batch_size=500, truncate=False, on_conflict="error", encoding="utf-8", dsn=None) -> int
trawler.raw.load_from_db(dest_table, src_table, *, columns=None, pk=None, batch_size=1000, truncate=False, on_conflict="error", dsn=None, src_dsn=None) -> int
```

All loaders return rows actually written (skipped rows not counted). All create `raw.<table>` if it doesn't exist.

### on_conflict

| value | behaviour |
|-------|-----------|
| `"error"` (default) | raises on duplicate PK |
| `"skip"` | `ON CONFLICT DO NOTHING` — silently ignores duplicates |
| `"replace"` | `ON CONFLICT (pk) DO UPDATE SET ...` — upserts all non-PK cols (requires `pk` to be set, raises `ValueError` otherwise) |

Use `"skip"` or `"replace"` for incremental/idempotent loads without `truncate=True`.

### CSV caveat

CSV has no type info — all values land as `text`. If the pk column should be `bigint`, pass explicit `columns={"job_id": "bigint", ...}`, otherwise the PK will be `text`. For JSONL and DB sources, types are inferred automatically.

### Type inference (`infer_columns`)

| Python type | Postgres type |
|-------------|---------------|
| `bool` | `boolean` |
| `int` | `bigint` |
| `float` | `double precision` |
| `dict` / `list` | `jsonb` |
| `str` | `text` |
| `None` (all-null col) | `text` |

### Usage

```python
import trawler

# CSV with explicit types (needed when pk should be bigint)
trawler.raw.create_table("jobs", {"job_id": "bigint", "title": "text", "body": "text"}, pk="job_id")
n = trawler.raw.load_from_csv("jobs", "data/jobs.csv", pk="job_id", on_conflict="replace")

# JSONL — types inferred, incremental load
n = trawler.raw.load_from_jsonl("candidates", "data/cands.jsonl", pk="uid", on_conflict="skip")

# from another schema / another DB (src_dsn=...); truncate=True = full refresh
n = trawler.raw.load_from_db("jobs_raw", "gen.extract_skills")
```

### Notes

- `truncate=True` = TRUNCATE before insert (default append). `pk=` str or list (composite).
- `src_table` bare name defaults to `raw` schema; use `"gen.table"`/`"enc.table"` otherwise.
- `load_from_db` uses a server-side cursor — safe on millions of rows.
- Inferred column order = first-seen order across rows.

---

## skill: trawler-offload

**Trigger**: exporting a job to run on another machine without Postgres, running a bundled job, importing remote results back — AND any check of remote/offload job state: "remote status", progress/rate/ETA of a job on the box, queue health (`trawler status`, `trawler queue`, `trawler jobs`). NEVER raw ssh to the box for any of this — the `trawler` CLI is the only interface (3 narrow ssh exceptions listed inside)

**Scope**: `src/trawler/offload/bundle.py`, `runner.py`, `importer.py`, `src/trawler/cli.py`

### ⛔ SSH IS NOT THE INTERFACE — the `trawler` CLI is

Do NOT ssh to the box to check, fix, or move anything. Every remote state
question and action already has a CLI verb (`status`, `queue`, `jobs`,
`enqueue`, `interrupt`, `import`, `clean --remote`), and the CLI also keeps
`gen._gen_log` in sync — raw ssh reads stale/partial state and its writes
desync the control plane. If you are typing `ssh` and the box's hostname,
stop and find the verb in the command list below; if no verb covers it,
report that to the user instead of improvising over ssh.
ONLY three sanctioned ssh uses, nothing else:
1. read a queued task's saved args before re-enqueue (`sed -n 2p .../<ID>.task`)
2. the queue-code deploy sequence — follow MANUAL.md VERBATIM
3. delete a stale `queue/stuck/<ID>.task` after recovering a stuck job

### Route the request FIRST — user's words → one command

Match the request to a row before doing anything else. Every wrong offload
action on record started by skipping this table.

| user says (any phrasing) | you run | never |
|---|---|---|
| "check remote / job status", "progress", "ETA", "how's X going" | `trawler status` (or `trawler status <job-id>` for one job) | ssh to the box; stitching queue+jobs+psql by hand |
| "import", "pull back", "bring results home", "partial rows" | `trawler import <job-id>` (pulls first; a partial import is fine — status becomes `running` if still live, `partial` if confirmed stopped, re-import later picks up more) | hand-rsync; reading job.sqlite yourself |
| "re-run / continue / re-ship prompt X", "build a run for X", "generate the missing / un-generated rows" | the playbook below (`rebundle`) | `trawler bundle` with hand-chosen flags; ANY SQL |
| "dedup first", "exclude already-done rows" | **nothing** — bundle auto-excludes `ok`/`pending` rows, always | manual dedup (wasted work at best) |
| "...using gen.X" / "...from table gen.X" | read as: **X is the PROMPT name**; `gen.X` is its OUTPUT table, mentioned for orientation | `gen.X` as a `--source` |
| "resume / restart a parked or interrupted job" | re-enqueue decision rule (below) | — |
| brand-new prompt, never shipped before | first-ever-bundle checklist (below) — ask for missing fields | guessing any field |

### The playbook: re-run / continue prompt X (the common case)

```sh
trawler status                    # 0. orient — what's running / pending
trawler rebundle <X>              # 1. recipe + dry-run preview, ZERO side effects
trawler rebundle <X> --go         # 2. bundle for real → prints JOB_ID
trawler enqueue <JOB_ID> [-p N]   # 3. ship + queue on the box
trawler status                    # 4. watch; when done: trawler import <JOB_ID>
```

Step 1 has exactly three outcomes — follow the matching branch, no others exist:

- **Recipe + `pending > 0`** → proceed to step 2.
- **"nothing to bundle" (0 pending)** → the staging table may be stale. Find
  the task's OWN refresh script and run it, then re-run step 1:
  ```sh
  find ~/work/finetune/<X>/ -name 'stage_*.py'    # then: uv run python <that file>
  ```
  No `stage_*.py` exists, or still 0 after running it → **report "0 pending,
  nothing to ship" and stop.** That is a valid, complete answer. **Never
  write a `stage_*.py` yourself** in response to "none exists" either — that
  is the same hard NO below, just phrased as an absence instead of a
  guess. If a human explicitly asks you to write one (rare — a genuinely new
  task), its docstring MUST open by stating: (1) this is a Trawler
  pre-offload staging script, (2) which prompt/bundle it feeds, (3) why it
  exists (bundle can't ship a Python doc_fn — see `templates/05_custom_doc_fn.py`'s
  offload-variant section for the required header + an upsert-only sketch).
- **Exit 1 naming missing fields** → **stop and ask the user** for exactly
  those fields. Do not fill any of them in yourself.

### Hard NOs (each one caused a real shipped-bad-data incident — details in LESSON.md)

- **NO SQL writes in this workflow, ever** — no `CREATE TABLE`, `INSERT`,
  `ALTER`, no "temporary" staging tables. bundle/import do every needed DB
  write internally; staging tables are built ONLY by the task's `stage_*.py`.
- **NO hand-assembled recipes** for a prompt that has shipped before — the
  recipe comes from `rebundle` (or `job-config <job-id>` for a specific past
  job), never from your own reading of gen tables.
- **`gen.<X>` is never a `--source`** — it's where results GO.
- **NO raw ssh to the box** outside the three sanctioned uses above — ssh
  reads bypass the liveness logic (a "stopped-looking" job may be live) and
  ssh writes desync `gen._gen_log` from remote reality.
- **"0 pending" is a result to report, not a problem to fix.** A wrong guess
  here doesn't fail loudly — it runs to completion and silently poisons the
  output table with wrong-format inputs.

### First-ever bundle for a NEW prompt (rare — `rebundle` has nothing to recover)

Six required fields; each must come from the user or the cfg tables, never invented:
`--prompt` (a `cfg.prompt` row) · `--decoder` (`cfg.model_name`) ·
`--model-type` (`cfg.model_type`, must match what's actually serving on the
remote — check `trawler models`) · `--source` (a plain table; never
`gen.<prompt>`) · `--pk` · `--doc-col` (columns already holding final text —
no doc_fn ships). Missing any → ask for it by name. Then:
`bundle ... --dry-run` → check counts → `bundle` → `enqueue`.

### The loop

```
[local, has Postgres]   trawler bundle    → output/jobs/<job-id>/  (job.toml, rows.jsonl, job.sqlite)
[remote, no Postgres]   trawler run-bundle <job-dir>   → results into job.sqlite
[local, has Postgres]   trawler import <job-dir>       → gen.<prompt> + gen._gen_log
```

### Commands (remote verbs take `-r <name>`; default = first in TRAWLER_REMOTES)

```sh
trawler bundle --prompt <P> --decoder <M> --model-type <MT> \
    --source <schema.table> --pk <COL...> --doc-col <COL...> [--limit N] [--out DIR] [--dry-run]
trawler enqueue <job-id> [-p N] [run-bundle args]  # push + priority queue (preferred);
                                             # -p signed int, default 0, right after job-id:
                                             # higher runs first, strictly-higher PREEMPTS the
                                             # active job (~1min; it requeues, resumes later)
trawler interrupt <job-id>                   # interrupt a running/queued job — partial kept, resumable
trawler status                               # ONE-STOP OVERVIEW: remote progress/rate/ETA/log tail
                                             # + queue health + local pending jobs, in one output
                                             # (three headed sections). Use this, not queue+jobs by hand.
trawler status <job-id>|--all                # focused: single remote job, or every remote (unchanged)
trawler queue                                # queue section alone: active/waiting/cooling/done/interrupted
trawler jobs [--all]                         # local-jobs section alone: control-plane view from gen._gen_log
trawler job-config <job-id>                  # recipe of one SPECIFIC job + copyable bundle line
trawler rebundle <prompt> [--go] [--limit N] # recipe of the prompt's LATEST job + dry-run preview
                                             # (see playbook above; full behavior in Rules below)
trawler import <job-id|job-dir> [--force]    # job-id: pulls first; dir: import as-is
# occasional: push --with-repo / run / pull / models / fetch-model <hf-repo> <file>
trawler run-bundle <job-dir> [--concurrency N] [--retries N] [--limit N] \
    [--temperature F] [--max-tokens N] [--timeout S]   # what the remote executes
```

### Re-enqueue (resume) a parked job — decision rule, follow verbatim

Run `trawler queue` first, then act by state:

- **cooling** → do NOTHING (auto-retries when `retry_in` hits 0).
- **active (RUNNING)** → do NOTHING (and `enqueue` now refuses without `--force`).
- **interrupted / stopped / cycling cooling→stall** → `trawler enqueue <JOB_ID>`,
  then `trawler status <JOB_ID>` to confirm active/waiting.
- Urgent job stuck behind a low-value one → don't interrupt; `trawler enqueue
  <JOB_ID> -p 10` preempts within ~1 min, the preempted job resumes by itself.
- Caveat: enqueue purges the old task file, so a bare re-enqueue runs with
  DEFAULT run-bundle args — repeat any custom `--retries`/`--max-tokens`/...
  on the enqueue line (check first: `ssh <box> "sed -n 2p <jobs-dir>/queue/*/<JOB_ID>.task"`).

### Status model (offload jobs) — psql is the live status view

`exported` (bundled) → `running` (remote actively processing) → `partial`
(rows imported, incomplete, confirmed NOT running) | `interrupted` (confirmed
parked in `queue/interrupted/` — actually stopped) → `complete` (every bundled
row attempted). `cleaned` = ghost row after `clean --yes`.

- **Liveness wins over coverage**: a partial import of a still-running job is
  `running`, never `partial`/`interrupted` (the old overload lied that a live
  job was stopped). Coverage comes from `job_meta.row_count`; stopped-ness
  ONLY from remote queue state (same liveness check `pull` uses).
- `status`/`jobs`/`pull`/`import` write fresh `n_done`/`n_failed`/`status`
  back to `gen._gen_log` after every remote poll (best-effort: DB-unreachable
  warns, never fails the command).
- Pending for `jobs`/`status` = `exported`/`running`/`partial`/`interrupted`;
  `clean --imported` targets `complete` only; default views exclude
  `cleaned` (`jobs --all` shows them).
- **One log row per job, whole lifecycle**: `bundle` registers
  `status='exported'`; `push`/`run`/`enqueue` stamp `config->>'remote'` +
  `stage`; `import` UPDATEs the same row in place. Re-import over any
  non-`complete` status is allowed; `complete` is refused without `--force`.

### Rules

- `--doc-col` required (cols joined by `\n`, like `set_doc_fn(list)`); a bundle without it can't run.
- base_url is NEVER in the bundle — the remote resolves `model_type.base_url_env` from its own environment.
- `run-bundle` is resumable: ok rows in job.sqlite skipped, fail rows retried. Row errors never abort a pass; `error_category` mirrors live-run categories.
- `interrupt` = graceful pull-back: drops an `INTERRUPT` flag; watchdog kills the pass and exits **2** → job parked in `queue/interrupted/`, no requeue. Watchdog exit codes the queue routes on: `0`=complete→`done/`, `1`=stalled→`cooling/`, `2`=interrupted→`interrupted/`, `3`=preempted→back to waiting. Ok rows survive (per-row commit); `import` gets the partial, `enqueue` resumes. Smoke tests: `scripts/smoke_interrupt.sh`, `smoke_priority.sh` (local, no remote).
- **Priority**: `enqueue -p N` (signed, default 0; backfill `-1`, urgent `10`). Highest prio wins, FIFO within a level. Watchdog self-checks per minute, exits 3 when a strictly-higher-prio task waits — active job returns to *waiting* (position intact) and resumes later. Never fires for direct `trawler run` jobs. NOTE: the box runs the queue code shipped at last enqueue — scheduler changes need queue-session kill + re-enqueue, deploy sequence in MANUAL.md VERBATIM (use full path `/opt/homebrew/bin/tmux` over ssh; bare `tmux` false-reports "no session").
- `import` never deletes job.sqlite (local dir + remote copy persist for resume/audit). Reclaim disk: `trawler clean --imported` or `clean <job-id>` — dry-run by default, `--yes` deletes, `--remote` also rms the box's copy. `clean --yes` stamps `status='cleaned'` + `config->>'stage'='cleaned'` (back-compat) and releases the job's `'pending'` claims first; DB unreachable → dirs still deleted, warning printed.
- **`enqueue`/`push` refuse a RUNNING job without `--force`** (checks `queue/active/<id>.task` + live run-bundle process — prevents a job being both active and waiting). `--force` bypasses (stripped before `offload.sh`). Read-only verbs are never guarded.
- `import` creates the gen table if missing; `'j'` outputs parsed → `json_output`.
- **Claim rows**: `bundle` inserts a `status='pending'` placeholder per shipped `row_key` into `gen."<prompt>"` (same run_id, one transaction — failed bundle leaves no orphans). Exclusion query uses `status IN ('ok','pending')`, so concurrent bundles can't double-ship. `import` upserts over placeholders (`ON CONFLICT DO UPDATE`); rows still `'pending'` after import are warned about. `run_id` is recorded in `job.toml [job].run_id` for claim release without a DB query.
- **Dedup**: duplicate source `row_key`s are deduped at bundle with a warning; deduped count written to `job.toml`/`job_meta` so completion doesn't stick at `'partial'`.
- Pending (`bundle`) = source rows minus ALL ok/pending rows in `gen.<prompt>` — bundle→run→import→bundle converges to 0.
- Limits: source must be a plain table (no `where=`/run_id filter); doc_fn logic doesn't ship — precompute into a source column via a staging table. `carry` doesn't ship; imported rows have `carry = NULL`.
- Raw `SELECT *` on gen tables shows bundle's `'pending'` rows — invisible to `from_gen(latest_ok=True)`, resolved by import or clean. Expected.
- **`pull` warns but proceeds if the job is still RUNNING remotely** (partial snapshot; import marks it `running`, re-pull later for more). Stalled/finished jobs pull silently.
- **`job-config <job-id>`**: prints the job's recipe + copyable `trawler bundle ...` line. Reads `output/jobs/<id>/job.toml` first, falls back to `gen._gen_log` (config carries `pk`/`doc_cols` for this) — works even after `clean`.
- **`rebundle <prompt> [--go] [--limit N]`**: resolves the prompt's latest job recipe (job.toml vs newest log row, whichever more complete). Without `--go`: recipe + `--dry-run` preview (pending/total/claimed). With `--go`: bundles + prints enqueue hint. Incomplete recipe → exit 1 naming the missing fields, never guesses. 0 pending → points at the task's `stage_*.py`, never hand-rolled SQL.

### Python API

```python
from trawler.offload.bundle import bundle
from trawler.offload.runner import run_bundle
from trawler.offload.importer import import_bundle

job_dir = bundle("p", "m", "mt", "raw.jobs", pk="id", doc_cols=["title", "body"])
run_bundle(job_dir, concurrency=8, retries=2, params={"max_tokens": 4000})
import_bundle(job_dir)   # → {"run_id", "ok", "failed", "table"}
```

---

## skill: trawler-evolve

**Trigger**: auditing Trawler for improvements, running a self-improvement / evolution pass, "what's wrong with this repo"

**Scope**: `EVOLVE.md`, `scripts/audit.py`, whole repo (read), AGENTS.md checklists

### Procedure

1. Read `EVOLVE.md` (repo root) — it owns the full playbook; this skill is only the entry point.
2. Mechanical audit first: `uv run python scripts/audit.py`. Fix every FAIL before hunting anything else.
3. Manual smell hunt using the ranked heuristics table in EVOLVE.md (transient-failure handling, boundary overshoot, double-paid side effects, resource churn, contract drift, doc drift, untested hard paths).
4. Prioritize: stored-data correctness > run reliability > cost > performance > DX > style. Small in-contract fixes → just do them. Scope changes (new dep, new public API) → list options + recommendation, let the user pick.
5. Ship via the feature loop (CLAUDE.md). Every behavior fix leaves behind a DB-free regression test; every new mechanical contract leaves behind an `audit.py` check.
6. Append a dated entry to the evolution log at the bottom of EVOLVE.md: findings, fixes, commit hash, deferred items.

### Rules

- Never delete a failing test to make the audit pass.
- Tests stay DB-free — stub the DB layer like `tests/test_run_lifecycle.py::FakeRun`.
- Check the "Known backlog" section of EVOLVE.md before proposing work — it may already be listed with context.

---

## Schema quick-ref

| cfg table | key col | used by |
|-----------|---------|---------|
| `cfg.system_prompt` | name → `gen.<name>` output table | `set_system_prompt` (dec) |
| `cfg.decoder` | name | `set_model` (dec) |
| `cfg.encoder` | name → `enc.<name>` output table, dim | `set_model` (enc) |
| `cfg.model_type` | name, protocol, base_url_env | `set_model_type` (dec + enc) |

| log table | written by |
|-----------|------------|
| `gen._gen_log` | TextGenRun / JsonGenRun |
| `enc._enc_log` | MinimalEncodeRun |

DSN: `TRAWLER_DSN` env var (or `ROWINFER_DSN` for back-compat) or explicit `dsn=` param on every tool call.
