# table

Postgres layout: schemas, table naming, run-tagging convention. DDL lives in `table/sql/init.sql` (idempotent — safe to re-run via `trawler-init`).

## Schemas

| schema | role |
|--------|------|
| `gen` | generate runs — `_gen_log` + per-prompt output tables |
| `enc` | encode runs — `_enc_log` + per-encoder vector tables |
| `cfg` | shared config: system_prompt, decoder, encoder, model_type |
| `raw` | user data tables; default schema for `source.from_db()` |

## Output table naming

| schema | name source | rationale |
|--------|-------------|-----------|
| `gen.<system_prompt.name>` | `cfg.system_prompt.name` | prompt defines what's generated; same prompt across models → one table |
| `enc.<model.name>` | encoder `name` | encoder defines the vector space; same data through different encoders → separate tables |

Rows are tagged with `run_id` so single-batch reads filter by run_id; cross-batch reads union multiple run_ids.

## Common output table columns

| col | type | meaning |
|-----|------|---------|
| run_id | uuid | FK to `_gen_log` / `_enc_log` |
| row_key | text | value of `source_uid` col from data_source |
| status | text | `ok` / `failed` |
| error | text | traceback if failed |
| error_category | text | exception class name — query like `WHERE error_category='BudgetError'` |
| created_at | timestamptz | |

### `gen.<system_prompt.name>` extra cols

| col | type | notes |
|-----|------|-------|
| raw_output | text | verbatim LLM output — always saved via _extras_floor |
| json_output | jsonb | parsed object (JsonGenRun only); null on parse fail |
| *carry cols* | jsonb each | all data_source cols by default; subset via set_carry_cols |

### `enc.<model.name>` extra cols

| col | type | notes |
|-----|------|-------|
| vec | vector(dim) | pgvector embedding |
| doc | text | text that was embedded — saved via _extras_floor |
| *carry cols* | jsonb each | passthrough from data_source |

## Log tables

Both share these core cols:

| col | meaning |
|-----|---------|
| run_id | uuid PK |
| name | run label |
| model | model name |
| status | running / complete / failed / interrupted / early_stopped (+ exported for offload jobs in gen) |
| started_at / ended_at | timestamps |
| n_rows | estimated total (from limit or len) |
| n_done | rows processed (updated per row) |
| n_failed | rows with status='failed' |
| error | run-level traceback (whole-batch break only) |
| config | full jsonb snapshot at register (model row, endpoint, params, source_uid, limit) |

Gen-only extra: `system_prompt_content` — full prompt text at register time for SQL-side audit without jsonb dig.

Enc-only extra: `dim` — encoder dimension.

`init.sql` uses `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for new cols so `trawler-init` migrates older DBs.

## cfg schema

All cross-cutting config. Managed via `trawler.cfg` (see `cfg.py`).

### cfg.system_prompt

| col | meaning |
|-----|---------|
| name | PK — stable id, becomes gen table name |
| content | full prompt text |
| expected_output | `'t'` (TextGenRun) / `'j'` (JsonGenRun) |
| description | free text |
| created_at / updated_at | timestamps |

At register, gen snapshots `content` + `expected_output` into `_gen_log.config`. Reruns work even if the live cfg row is later edited or deleted.

### cfg.decoder

| col | meaning |
|-----|---------|
| name | PK — short stable id; referenced from `_gen_log.model` |
| repo_name | HF repo id / local path / API model id |
| format | jsonb, optional schema hint |
| description | free text |
| created_at / updated_at | |

### cfg.encoder

| col | meaning |
|-----|---------|
| name | PK — becomes enc table name (`enc.<name>`) |
| repo_name | HF repo id / local path / API model id |
| dim | vector dimension — validated against returned vec at post_step |
| format | jsonb, optional |
| description | free text |
| created_at / updated_at | |

### cfg.model_type

Transport profile — chosen per run, not per model.

| col | meaning |
|-----|---------|
| name | PK — e.g. `local_ollama`, `remote_lms`, `openai`, `local_sentence_transformer` |
| protocol | `ollama` / `openai` / `anthropic` / `sentence_transformers` — selects client function |
| base_url_env | env var name for base URL; nullable for local protocols |
| api_key_env | env var name for api key; nullable |
| description | free text |
| created_at / updated_at | |

Model and transport are decoupled. Same model can be called via different model_types per run. New deployment = INSERT row + export env var.

Seed rows are inserted by `trawler-init` (idempotent):

| name | protocol | env var |
|------|----------|---------|
| `local_ollama` | ollama | OLLAMA_LOCAL_BASE_URL |
| `remote_ollama` | ollama | OLLAMA_REMOTE_BASE_URL |
| `local_lms` | openai | LMS_LOCAL_BASE_URL |
| `remote_lms` | openai | LMS_REMOTE_BASE_URL |
| `openai` | openai | OPENAI_BASE_URL + OPENAI_API_KEY |
| `anthropic` | anthropic | ANTHROPIC_BASE_URL (bare host, no /v1) + ANTHROPIC_API_KEY |
| `local_sentence_transformer` | sentence_transformers | (none — local) |
