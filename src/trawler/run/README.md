# run

Shared backbone for batch row-by-row pipelines. Both `generate/` and `encode/` subclass this. Owns: lifecycle, log integrity, iteration loop, error categorization. Abstract — concrete step logic lives in subclasses.

## Why its own folder

Gen and enc lifecycle is identical except for the per-row call. Pulling the loop here keeps `generate/` + `encode/` thin — just per-row hooks and the log table name.

## BaseRun

```python
class BaseRun(ABC):
    # ---- subclass declares ----
    LOG_TABLE: ClassVar[str]    # "gen._gen_log" or "enc._enc_log"
    OUT_SCHEMA: ClassVar[str]   # "gen" or "enc"

    # ---- subclass implements ----
    @abstractmethod
    def _out_table_name(self) -> str: ...       # gen → system_prompt.name; enc → model.name
    @abstractmethod
    def _out_table_cols(self) -> dict[str, str]: ...  # {col: sql_type}
    @abstractmethod
    def pre_run_check(self) -> None: ...
    @abstractmethod
    def post_run_check(self) -> None: ...
    @abstractmethod
    def pre_step(self, row) -> Any: ...
    @abstractmethod
    def step(self, payload) -> Any: ...
    @abstractmethod
    def post_step(self, row, out) -> dict: ...

    # ---- backbone provides ----
    def run(self) -> str: ...                   # returns run_id
    def _do_row(self, row) -> str: ...          # 'ok' | 'failed'
    def _pre_flight(self) -> None: ...
    def _register_run(self) -> None: ...
    def _finalize(self, status, error=None) -> None: ...
    def _bump_progress(self, n_done, n_failed) -> None: ...
    def _ensure_out_table(self) -> None: ...
    def _ensure_column(self, col, sql_type='jsonb') -> None: ...
    def _extras_floor(self, row, raw) -> dict: ...
    def _resolve_endpoint(self, name) -> ResolvedEndpoint: ...
```

## Lifecycle

```
pre_run_check()
  │
  ├─ _snapshot = _build_snapshot()
  ├─ _pre_flight()          # dry first-row before register (if preflight=True)
  ├─ _ensure_out_table()    # CREATE TABLE IF NOT EXISTS
  ├─ _ensure_column("error_category", "text")
  ├─ _register_run()        # INSERT into LOG_TABLE, assign run_id
  │
  ├─ for row in data_source (up to limit):
  │     _do_row(row)         # pre_step → step → post_step, catch/log per-row
  │     _bump_progress()     # UPDATE n_done / n_failed, throttled to ~1 write / 2s
  │
  ├─ post_run_check()
  └─ finally: _finalize(status)   # always runs — Ctrl-C, exception, normal exit
```

## Setup attrs

| attr | required | notes |
|------|----------|-------|
| `model` | yes | set by subclass setter |
| `model_type` | yes | set by subclass setter, resolves env → ResolvedEndpoint |
| `data_source` | yes | set by subclass setter |
| `source_uid` | yes | col name whose value becomes row_key |
| `run_name` | no | defaults to `f"{prompt/model}-{utc_iso}"` |
| `config` | no | params dict, default `{}` |
| `limit` | no | cap on rows, default None (all); enforced exactly in all three modes (serial / batch / concurrent). `set_limit(n, total=True)` counts prior-ok rows too — resume tops up until the table has `n` ok rows |
| `preflight` | no | default True |
| `retries` | no | `set_retries(n, backoff=2.0)` — retry step/step_batch on `EndpointError` only; delay doubles per attempt; default 0 |
| `verbose` | no | `set_verbose(False)` silences console output; run/log rows unaffected |
| `config` `timeout` | no | `set_config(timeout=...)` — per-request HTTP timeout in seconds, default 600 |

## Backbone hooks

| hook | purpose |
|------|---------|
| `_pre_flight()` | Dry first-row call before register. Catches Budget/Endpoint/Protocol/Parse without writing a log row. On success the result is cached and written after register — row 1's model call is not repeated. Toggle via `set_preflight(False)`. |
| `_extras_floor(row, raw)` | Extras saved EVEN IF post_step raises. Subclass overrides to guarantee fields (raw_output, doc, carry cols) survive parse failures. |
| `_extra_log_cols()` | Subclass injects extra cols into the log INSERT at register time (e.g. system_prompt_content, dim). |
| `_ensure_column(col, sql_type)` | Idempotent ALTER TABLE ADD COLUMN IF NOT EXISTS on the out table. |
| `_resolve_endpoint(name)` | Lookup cfg.model_type, resolve env vars → ResolvedEndpoint. Missing env → raise before run registers. |

## Error categorization

Per-row exceptions are caught; `type(e).__name__` written to `error_category` col. Batch continues. Whole-batch break only on infrastructure errors (DB down, etc.).

| category | exception |
|----------|-----------|
| `ConfigError` | setup / cfg / env problem |
| `EndpointError` | 5xx, 429, timeout, DNS — transient; auto-retried when `set_retries(n)` is set |
| `ProtocolError` | other 4xx, malformed response, empty content |
| `BudgetError` | finish_reason='length' or reasoning model exhausted budget |
| `ParseError` | post_step failed to parse step output |

## Log integrity guarantees

- Register before first step → no unlogged run.
- `finalize` in `finally:` → Ctrl-C, exception, and normal exit all close the log row, with final `n_done`/`n_failed` counts.
- `n_done` bumped during the loop (throttled to ~1 UPDATE per 2s) → progress visible live via `inspect.list_runs()`; exact counts written at finalize.
- Resume refreshes `n_rows` and seeds `n_done` with prior-ok rows → `pct_done` stays meaningful across passes.
- `config` col frozen at register → full snapshot for audit and resume even if cfg rows change later.
- All log/out-table writes go through a `psycopg_pool.ConnectionPool` opened for the duration of `run()` — no per-row connection churn. If `psycopg_pool` is missing (stale env), the run logs a warning and falls back to per-call connections instead of crashing.

## Subclass contract

| subclass | LOG_TABLE | OUT_SCHEMA | out table name |
|----------|-----------|------------|----------------|
| `MinimalGenRun` / variants | `gen._gen_log` | `gen` | `system_prompt.name` |
| `MinimalEncodeRun` | `enc._enc_log` | `enc` | `model.name` (encoder) |
