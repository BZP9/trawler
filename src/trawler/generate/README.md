# generate

Batch LLM generation. Subclasses `run.BaseRun` — see `run/README.md` for lifecycle, log integrity, pre-flight, and error categorization.

Two concrete variants:

| class | expected_output | extra output col |
|-------|-----------------|------------------|
| `TextGenRun` | `'t'` | (none — raw_output only) |
| `JsonGenRun` | `'j'` | `json_output` (jsonb) |

Both write output to `gen.<system_prompt.name>` and log to `gen._gen_log`.

## MinimalGenRun — scaffold

Owns: setters, prompt lookup, snapshot, default `pre_step` + `step`. Variants override `post_step` and the `EXPECTED_OUTPUT` class var.

### Setters (each validates at call time)

| setter | check |
|--------|-------|
| `set_gen_name(name)` | non-empty; optional — defaults to `f"{system_prompt}-{utc_iso}"` |
| `set_model(name)` | `cfg.decoder` row exists → stores `DecoderConfig` |
| `set_model_type(name)` | `cfg.model_type` row exists + env var set → stores `ResolvedEndpoint` |
| `set_system_prompt(name)` | row exists + `expected_output` matches class's `EXPECTED_OUTPUT` |
| `set_data_source(data, source_uid)` | iterable + first-row peek confirms `source_uid` key present |
| `set_config(**params)` | stored as-is; `timeout` (s) sets per-request HTTP timeout, default 600 |
| `set_carry_cols(cols)` | `None` = all data_source cols except source_uid |
| `set_limit(n, total=False)` | positive int or `None`. Default caps rows processed THIS pass (resume does `n` more). `total=True` caps cumulative ok rows — resume tops up until the table has `n` ok rows |
| `set_preflight(enabled)` | default `True` |
| `set_retries(n, backoff=2.0)` | retry `step` on `EndpointError` up to `n` extra times; delay doubles per attempt; default `0` |
| `set_verbose(enabled)` | default `True`; `False` silences console output |

### Default behaviors (overridable)

| method | default |
|--------|---------|
| `pre_step(row)` | `{"system": prompt.content, "user": json(carry_cols_subset)}` |
| `step(payload)` | dispatch to `model.clients.call` (ollama / openai / anthropic) |
| `_extras_floor(row, raw)` | `{"raw_output": str(raw)}` + carry cols as jsonb — survives parse fail |
| `_out_table_cols()` | `{"raw_output": "text"}` + carry cols as jsonb |
| `_extra_log_cols()` | `{"system_prompt_content": prompt.content}` |
| `_build_snapshot()` | extends base with `system_prompt` row + `carry_cols` list |

## TextGenRun

```python
class TextGenRun(MinimalGenRun):
    EXPECTED_OUTPUT = "t"

    def post_step(self, row, out) -> dict:
        return {}   # raw_output already saved via _extras_floor
```

## JsonGenRun

```python
class JsonGenRun(MinimalGenRun):
    EXPECTED_OUTPUT = "j"

    def post_step(self, row, out) -> dict:
        parsed = _coerce_json(out)
        return {"json_output": Jsonb(parsed)}
```

### `_coerce_json(out)` — best-effort JSON extraction

Tries in order:
1. `dict` / `list` passthrough (clients with json_mode return parsed)
2. `bytes` → decode
3. ` ```json … ``` ` fence (regex search)
4. `json.loads` on trimmed string
5. First balanced `{…}` / `[…]` block (brace-counting scan)
6. `ParseError` with first 200 chars of output

## Usage

```python
from trawler import JsonGenRun, from_db

gen = JsonGenRun()
gen.set_model("gemma4_31b")
gen.set_model_type("remote_lms")
gen.set_system_prompt("extract_skills")
gen.set_data_source(from_db("jobs"), source_uid="id")
gen.set_config(temperature=0.0, max_tokens=2000)
gen.set_limit(5)
run_id = gen.run()
```

## Output table — `gen.<system_prompt.name>`

| col | type | notes |
|-----|------|-------|
| run_id | uuid | FK to _gen_log |
| row_key | text | value of source_uid col |
| status | text | `ok` / `failed` |
| error | text | traceback on failure |
| error_category | text | exception class name |
| raw_output | text | verbatim LLM output — always saved (floor) |
| json_output | jsonb | parsed object (JsonGenRun only; null on parse fail) |
| *carry cols* | jsonb each | data_source cols copied through |
| created_at | timestamptz | |

## `gen._gen_log`

| col | meaning |
|-----|---------|
| run_id | uuid PK |
| name | run label |
| model | decoder name |
| status | running / complete / failed / interrupted / early_stopped (+ exported for offload jobs in gen) |
| started_at / ended_at | timestamps |
| n_rows | from limit or len(data_source); null if neither |
| n_done | rows processed (throttled UPDATE; exact at finalize) |
| n_failed | rows with status='failed' |
| error | run-level traceback (whole-batch break only) |
| system_prompt_content | full prompt text — audit without digging into config jsonb |
| config | full snapshot: model row, endpoint, prompt row, carry_cols, params, source_uid, limit |

Query log via `trawler.inspect.list_runs()` / `run_stats()`.
