# encode

Batch embedding. Subclasses `run.BaseRun` — see `run/README.md` for lifecycle, log integrity, pre-flight, and error categorization.

Per-encoder table naming: `enc.<encoder.name>`. Cross-encoder vector queries are meaningless (different vector spaces), so per-model tables make that impossible by construction.

## MinimalEncodeRun

`src/trawler/encode/encode_run.py`

```python
class MinimalEncodeRun(BaseRun):
    LOG_TABLE  = "enc._enc_log"
    OUT_SCHEMA = "enc"
```

### Setters (each validates at call time)

| setter | check |
|--------|-------|
| `set_encode_name(name)` | optional; defaults to `f"{model.name}-{utc_iso}"` |
| `set_model(name)` | `cfg.encoder` row exists → stores `EncoderConfig` (incl. dim) |
| `set_model_type(name)` | `cfg.model_type` row exists; null `base_url_env` OK for local protocols |
| `set_data_source(data, source_uid)` | iterable + first-row peek confirms `source_uid` present |
| `set_text_col(col)` | required — which data_source col holds text to embed |
| `set_config(**params)` | embed params (e.g. `normalize=True`); `timeout` (s) sets per-request HTTP timeout, default 600 |
| `set_carry_cols(cols)` | `None` = all cols except source_uid + text_col |
| `set_limit(n, total=False)` | positive int or `None`. Default caps rows processed THIS pass (resume does `n` more). `total=True` caps cumulative ok rows — resume tops up until the table has `n` ok rows |
| `set_preflight(enabled)` | default `True` |
| `set_retries(n, backoff=2.0)` | retry `step`/`step_batch` on `EndpointError` up to `n` extra times; delay doubles per attempt; default `0` |
| `set_verbose(enabled)` | default `True`; `False` silences console output |

### Default behaviors

| method | default |
|--------|---------|
| `pre_step(row)` | `{"doc": str(row[text_col])}`; raises `ConfigError` if missing or blank |
| `step(payload)` | `model.clients.embed(model, endpoint, doc, params)` |
| `_extras_floor(row, raw)` | `{"doc": doc_text, <carry cols as jsonb>}` — survives post_step failure |
| `_out_table_cols()` | `{"vec": "vector(dim)", "doc": "text"}` + carry cols as jsonb |
| `_extra_log_cols()` | `{"dim": model.dim}` |
| `_build_snapshot()` | extends base with `text_col`, `carry_cols`, `dim` |
| `post_step(row, out)` | validates `len(vec) == model.dim` → `{"vec": _vector_literal(vec)}` |

`_vector_literal(vec)` formats `list[float]` as pgvector text `"[0.1,0.2,…]"` — auto-cast on insert.

## Output table — `enc.<encoder.name>`

| col | type | notes |
|-----|------|-------|
| run_id | uuid | FK to _enc_log |
| row_key | text | value of source_uid col |
| status | text | `ok` / `failed` |
| error | text | traceback on failure |
| error_category | text | exception class name |
| vec | vector(dim) | pgvector embedding |
| doc | text | text that was embedded (floor — saved even if step/post fails) |
| *carry cols* | jsonb each | passthrough from data_source |
| created_at | timestamptz | |

## `enc._enc_log`

| col | notes |
|-----|-------|
| run_id, name, model, status, started_at, ended_at, n_rows, n_done, n_failed, error, config | backbone |
| dim | from `_extra_log_cols` → model.dim |

Snapshot in `config` includes: model row, endpoint (no api_key), `text_col`, `carry_cols`, `dim`, params, `source_uid`, `limit`.

## Protocols

Dispatch via `model.clients.embed()` keyed on `endpoint.protocol`:

| protocol | impl | transport |
|----------|------|-----------|
| `ollama` | `ollama_embed` | POST `{base_url}/api/embed` |
| `openai` | `openai_embed` | POST `{base_url}/embeddings` (base_url must end in `/v1` for LMS) |
| `sentence_transformers` | `sentence_transformer_embed` | in-process; lazy import; model cached per process |

`sentence_transformers` needs no `base_url_env`. Requires `pip install sentence-transformers`.

## Usage

```python
from trawler import MinimalEncodeRun, from_db

enc = MinimalEncodeRun()
enc.set_model("jd2vector")                        # cfg.encoder.jd2vector (dim=384)
enc.set_model_type("local_sentence_transformer")
enc.set_data_source(from_db("jobs"), source_uid="id")
enc.set_text_col("description")
enc.set_carry_cols(["title", "company"])
enc.set_config(normalize=True)
enc.set_limit(5)
run_id = enc.run()
```

Via HTTP embedding (LMS / OpenAI-compatible):

```python
enc.set_model("nomic_embed")
enc.set_model_type("remote_lms")    # POST {LMS_REMOTE_BASE_URL}/embeddings
```

## Validation guarantees

- `set_model` fails if `cfg.encoder.<name>` not found.
- `set_model_type` fails if row not found or its `base_url_env` is set but env not exported.
- `set_data_source` peeks first row and verifies `source_uid` key present.
- `pre_step` raises `ConfigError` per row if `text_col` is empty or blank.
- `post_step` raises `ConfigError` if `len(vec) != model.dim`.
- Pre-flight runs the full chain (pre_step → step → post_step) before register — dim mismatches, missing env, wrong protocol surface before any rows are processed.
