from __future__ import annotations
import json
import logging
import os
import sys
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

import psycopg
from psycopg.rows import dict_row

from trawler.errors import ConfigError, EndpointError, RowInferError
from trawler.model.types import ResolvedEndpoint
from trawler.source import RowSource


logger = logging.getLogger("trawler")


def _compute_row_key(uid: str | list[str], row) -> str:
    """Encode a source row's PK columns into a stable text key.

    Single-col PK   → str(row[uid])
    Composite PK    → json.dumps([str(row[col]) for col in uid])

    This is the canonical encoding used by BaseRun._row_key and by the
    bundle command so that job.sqlite keys match gen-table row_key values.
    """
    if isinstance(uid, list):
        if isinstance(row, dict):
            return json.dumps([str(row[c]) for c in uid])
        return json.dumps([str(getattr(row, c)) for c in uid])
    if isinstance(row, dict):
        return str(row[uid])
    return str(getattr(row, uid))


def _ensure_default_handler() -> None:
    """Console output out of the box; a user-configured root logger wins."""
    if logger.handlers or logging.getLogger().handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _prepend(first, it):
    yield first
    yield from it


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _make_doc_fn(fn: str | list[str] | Callable) -> tuple[Callable, list[str]]:
    """Parse doc_fn spec into (callable, source_cols).

      "col"              → lambda r: r["col"]
      ["col1", "col2"]   → lambda r: f"{r['col1']}\n{r['col2']}"
      callable           → used as-is, source_cols=[]
    """
    if callable(fn):
        return fn, []
    cols: list[str] = [fn] if isinstance(fn, str) else list(fn)
    if len(cols) == 1:
        col = cols[0]
        return (lambda r, c=col: r[c] if isinstance(r, dict) else getattr(r, c)), cols
    def _multi(r, cs=cols):
        return "\n".join(str(r[c] if isinstance(r, dict) else getattr(r, c)) for c in cs)
    return _multi, cols


class BaseRun(ABC):
    """Shared lifecycle for batch row-by-row pipelines.

    Subclass contract:
      LOG_TABLE, OUT_SCHEMA — class vars
      _out_table_name()     — gen → system_prompt.name; enc → model.name
      _out_table_cols()     — extra cols on the output table {col: sql_type}
      pre_run_check / post_run_check
      pre_step / step / post_step
    """

    LOG_TABLE: ClassVar[str]
    OUT_SCHEMA: ClassVar[str]

    def __init__(self, dsn: str | None = None):
        from trawler.dsn import resolve_dsn
        from trawler.env import load_env
        load_env()                   # .env → os.environ (exported vars win)
        self.dsn = resolve_dsn(dsn)
        _ensure_default_handler()

        # caller-set attrs
        self.run_name: str | None = None
        self.model: Any = None
        self.model_type: str | None = None
        self.data_source: Any = None
        self.source_uid: str | list[str] | None = None
        self.config: dict = {}
        self.limit: int | None = None        # cap on rows iterated (None = all)
        self.limit_total: bool = False       # True: limit counts prior-ok rows too
        self.preflight: bool = True          # dry first-row call before register
        self.early_stop_after: int | None = 10  # stop after N consecutive failures
        self.batch_size: int = 1             # rows per step_batch call (enc)
        self.concurrency: int = 1            # parallel workers (gen)
        self.retries: int = 0                # extra attempts on EndpointError
        self.retry_backoff: float = 2.0      # first retry delay (s); doubles each attempt
        self.verbose: bool = True

        # source provenance — populated by set_data_source when data is a RowSource
        self._source_table: str | None = None
        self._source_run_id: str | None = None
        self._row_source: RowSource | None = None

        # state filled by run()
        self.run_id: str | None = None
        self._endpoint: ResolvedEndpoint | None = None
        self._snapshot: dict = {}
        self._pool: Any = None
        self._preflight_cache: tuple | None = None  # (row, raw, extras, elapsed)

        # resume state
        self._resume_run_id: str | None = None
        self._ok_keys: set[str] = set()

    # ---- subclass implements ----
    @abstractmethod
    def _out_table_name(self) -> str: ...
    @abstractmethod
    def _out_table_cols(self) -> dict[str, str]: ...
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

    # ---- shared setters ----

    def set_data_source(self, data: Any, source_uid: str | list[str]) -> None:
        if isinstance(data, RowSource):
            self._source_table = data.table
            self._source_run_id = data.run_id
            self._row_source = data
        it = iter(data)
        try:
            first = next(it)
        except StopIteration:
            raise ConfigError("data_source is empty")
        uid_cols = [source_uid] if isinstance(source_uid, str) else source_uid
        if isinstance(first, dict):
            missing = [c for c in uid_cols if c not in first]
            if missing:
                raise ConfigError(
                    f"source_uid cols {missing!r} not in first row keys: "
                    f"{list(first.keys())}"
                )
        else:
            missing = [c for c in uid_cols if not hasattr(first, c)]
            if missing:
                raise ConfigError(
                    f"source_uid cols {missing!r} not on first row attrs"
                )
        self.data_source = _prepend(first, it)
        self.source_uid = source_uid

    def set_limit(self, n: int | None, total: bool = False) -> None:
        """Cap processed rows. Default: n rows THIS pass (a resume with n=200
        does 200 more). total=True: n rows across passes — a resume tops up
        until ok-count reaches n, so 101 prior-ok + limit 200 does 99 more."""
        if n is not None and n <= 0:
            raise ValueError("limit must be positive or None")
        self.limit = n
        self.limit_total = total

    def _effective_limit(self) -> int | None:
        """Per-pass row cap. In total mode, prior-ok rows already spent part
        of the budget. Call after _load_ok_keys on resume."""
        if self.limit is None:
            return None
        if self.limit_total:
            return max(0, self.limit - len(self._ok_keys))
        return self.limit

    def set_preflight(self, enabled: bool) -> None:
        self.preflight = enabled

    def set_early_stop(self, n: int | None) -> None:
        """Stop run after n consecutive failed rows. None to disable. Default 10."""
        self.early_stop_after = n

    def set_batch_size(self, n: int) -> None:
        if n < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = n

    def set_concurrency(self, n: int) -> None:
        if n < 1:
            raise ValueError("concurrency must be >= 1")
        self.concurrency = n

    def set_retries(self, n: int, backoff: float = 2.0) -> None:
        """Retry step/step_batch up to n extra times on EndpointError only.
        Delay before attempt k is backoff * 2**(k-1) seconds. Default 0 = no retry."""
        if n < 0:
            raise ValueError("retries must be >= 0")
        if backoff < 0:
            raise ValueError("backoff must be >= 0")
        self.retries = n
        self.retry_backoff = backoff

    def set_verbose(self, enabled: bool) -> None:
        """Silence per-row console output. Log rows still written either way."""
        self.verbose = enabled

    def _log(self, msg: str = "") -> None:
        if self.verbose:
            logger.info(msg)

    def set_resume(self, run_id: str) -> None:
        """Resume an existing run: reuse run_id, skip already-ok rows, retry the rest."""
        self._resume_run_id = run_id
        self.run_id = run_id
        self.preflight = False

    def _load_ok_keys(self) -> set[str]:
        table = f'{self.OUT_SCHEMA}."{self._out_table_name()}"'
        with self._conn() as conn:
            rows = conn.execute(
                f'SELECT row_key FROM {table} WHERE run_id=%s AND status=%s',
                (self._resume_run_id, "ok"),
            ).fetchall()
        return {r["row_key"] for r in rows}

    def _resume_register(self) -> None:
        # refresh n_rows/n_done so pct_done reflects this pass's source + prior ok rows;
        # n_failed resets because failed rows are retried and re-counted
        pass_total = self._pass_total()
        n_rows = len(self._ok_keys) + pass_total if pass_total is not None else None
        sets = "status='running', ended_at=NULL, error=NULL, n_done=%s, n_failed=0"
        vals: list[Any] = [len(self._ok_keys)]
        if n_rows is not None:
            sets += ", n_rows=%s"
            vals.append(n_rows)
        vals.append(self.run_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {self.LOG_TABLE} SET {sets} WHERE run_id=%s", vals
            )
        self._log(f'[run] {self.run_id}  →  {self.OUT_SCHEMA}."{self._out_table_name()}" (resumed)')

    # ---- snapshot for log.config (override to extend) ----
    def _build_snapshot(self) -> dict:
        assert self._endpoint is not None
        ep = self._endpoint
        return {
            "model": {"name": self.model.name, "repo_name": self.model.repo_name},
            "endpoint": {
                "model_type": ep.model_type,
                "protocol": ep.protocol,
                "base_url": ep.base_url,
            },
            "params": dict(self.config),
            "source_uid": self.source_uid,
            "source_table": self._source_table,
            "source_run_id": self._source_run_id,
            "limit": self.limit,
            "limit_total": self.limit_total,
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "retries": self.retries,
        }

    # ---- DB helpers ----
    def _conn(self):
        if self._pool is not None:
            return self._pool.connection()
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _open_pool(self) -> None:
        if self._pool is not None:
            return
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            # stale env installed before the psycopg[binary,pool] dep was added;
            # _conn() falls back to one connection per call — slower, still correct
            logger.warning(
                "[trawler] psycopg_pool not installed — using per-call "
                "connections. Refresh the env (e.g. `uv lock --upgrade-package "
                "trawler && uv sync`) to enable pooling."
            )
            return
        self._pool = ConnectionPool(
            self.dsn,
            min_size=1,
            max_size=self.concurrency + 2,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def _close_pool(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _resolve_endpoint(self, name: str) -> ResolvedEndpoint:
        """Lookup cfg.model_type by name + resolve env. Used by setters.

        Local protocols (e.g. sentence_transformers) may have null base_url_env —
        then base_url is empty string. HTTP protocols MUST have base_url_env set
        to a populated env var, else raise.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name, protocol, base_url_env, api_key_env "
                "FROM cfg.model_type WHERE name=%s",
                (name,),
            ).fetchone()
        if row is None:
            raise ConfigError(f"cfg.model_type {name!r} not found")
        base_url = ""
        if row["base_url_env"]:
            base_url = os.environ.get(row["base_url_env"]) or ""
            if not base_url:
                raise ConfigError(
                    f"env var {row['base_url_env']!r} not set "
                    f"(required by cfg.model_type {name!r})"
                )
        api_key = os.environ.get(row["api_key_env"]) if row["api_key_env"] else None
        return ResolvedEndpoint(
            model_type=row["name"],
            protocol=row["protocol"],
            base_url=base_url,
            api_key=api_key,
        )

    def _ensure_column(self, col: str, sql_type: str = "jsonb") -> None:
        """Idempotently add `col` to this run's out table.

        Use to grow the out table at runtime when extras dict has keys
        not declared in _out_table_cols(). Default type is jsonb (handles
        any json-serializable scalar/struct); use text or vector(dim) etc.
        when the col genuinely needs that type.
        """
        table = f'{self.OUT_SCHEMA}."{self._out_table_name()}"'
        with self._conn() as conn:
            conn.execute(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {sql_type}'
            )

    def _ensure_out_table(self) -> None:
        extras = self._out_table_cols()
        extras_sql = ",\n  ".join(f'"{c}" {t}' for c, t in extras.items())
        if extras_sql:
            extras_sql += ","
        table = f'{self.OUT_SCHEMA}."{self._out_table_name()}"'
        ddl = f"""
            CREATE TABLE IF NOT EXISTS {table} (
              run_id     uuid NOT NULL,
              row_key    text NOT NULL,
              status     text NOT NULL,
              error      text,
              {extras_sql}
              created_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (run_id, row_key)
            )
        """
        with self._conn() as conn:
            conn.execute(ddl)

    def _extra_log_cols(self) -> dict[str, Any]:
        """Extra cols on the log row at register time. Subclasses extend via super()."""
        return {
            "source_table": self._source_table,
            "source_run_id": self._source_run_id,
        }

    def _estimate_n_rows(self) -> int | None:
        """Pre-iter estimate. limit wins; else try len(); else COUNT from source."""
        if self.limit is not None:
            return self.limit
        return self._source_count()

    def _source_count(self) -> int | None:
        try:
            return len(self.data_source)  # type: ignore[arg-type]
        except TypeError:
            pass
        if self._row_source is not None:
            return self._row_source.count()
        return None

    def _pass_total(self) -> int | None:
        """Expected rows to process THIS pass. Already-ok rows are skipped and
        don't count toward the per-pass limit, so: min(limit, source - prior_ok).
        Naive `limit - prior_ok` undercounts when the source is bigger than
        limit (resume overshoots 100% and ETA goes negative)."""
        avail = self._source_count()
        if avail is not None:
            avail = max(0, avail - len(self._ok_keys))
        lim = self._effective_limit()
        if lim is not None:
            return min(lim, avail) if avail is not None else lim
        return avail or None

    def _register_run(self) -> None:
        self.run_id = str(uuid.uuid4())
        self._log(f'[run] {self.run_id}  →  {self.OUT_SCHEMA}."{self._out_table_name()}"')
        base_cols = {
            "run_id": self.run_id,
            "name": self.run_name,
            "model": self.model.name,
            "status": "running",
            "started_at": datetime.now(timezone.utc),
            "n_rows": self._estimate_n_rows(),
            "n_done": 0,
        }
        base_cols.update(self._extra_log_cols())
        cols = list(base_cols.keys()) + ["config"]
        vals = list(base_cols.values()) + [json.dumps(self._snapshot, default=str)]
        placeholders = ["%s"] * (len(cols) - 1) + ["%s::jsonb"]
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = (
            f"INSERT INTO {self.LOG_TABLE} ({col_list}) "
            f"VALUES ({', '.join(placeholders)})"
        )
        with self._conn() as conn:
            conn.execute(sql, vals)

    def _bump_progress(self, n_done: int, n_failed: int) -> None:
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {self.LOG_TABLE} SET n_done=%s, n_failed=%s WHERE run_id=%s",
                (n_done, n_failed, self.run_id),
            )

    def _finalize(self, status: str, error: str | None = None, *,
                  n_done: int | None = None, n_failed: int | None = None) -> None:
        sets = ["status=%s", "ended_at=%s", "error=%s"]
        vals: list[Any] = [status, datetime.now(timezone.utc), error]
        if n_done is not None:
            sets.append("n_done=%s")
            vals.append(n_done)
        if n_failed is not None:
            sets.append("n_failed=%s")
            vals.append(n_failed)
        vals.append(self.run_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {self.LOG_TABLE} SET {', '.join(sets)} WHERE run_id=%s",
                vals,
            )

    # ---- per row ----
    def _row_key(self, row) -> str:
        assert self.source_uid is not None
        return _compute_row_key(self.source_uid, row)

    def _write_row(self, row_key: str, status: str, error: str | None, extras: dict) -> None:
        cols = ["run_id", "row_key", "status", "error"] + list(extras.keys())
        vals = [self.run_id, row_key, status, error] + list(extras.values())
        placeholders = ",".join(["%s"] * len(cols))
        col_list = ",".join(f'"{c}"' for c in cols)
        update_set = ", ".join(
            f'"{c}"=EXCLUDED."{c}"' for c in cols if c not in ("run_id", "row_key")
        )
        table = f'{self.OUT_SCHEMA}."{self._out_table_name()}"'
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT (run_id, row_key) DO UPDATE SET {update_set}"
        )
        with self._conn() as conn:
            conn.execute(sql, vals)

    def _extras_floor(self, row, raw) -> dict:
        """Extras saved EVEN IF post_step raises.

        Override to guarantee fields (e.g. raw_output, carry cols) are
        persisted regardless of parse / post-processing outcome.
        `raw` is None if step itself never ran.
        """
        return {}

    # ---- verbose hooks (override in subclasses) ----
    def _fmt_payload(self, payload: Any) -> str:
        """Short string describing what goes TO the model. Empty = skip line."""
        return ""

    def _fmt_raw(self, raw: Any) -> str:
        """Short string describing raw model output."""
        s = str(raw).replace("\n", " ")
        return (s[:150] + "…") if len(s) > 150 else s

    def _fmt_post(self, extras: dict) -> str:
        """Short string describing post_step result. Empty = skip."""
        return ""

    # ---- retry ----
    def _retry_loop(self, fn: Callable, *args) -> Any:
        attempt = 0
        while True:
            try:
                return fn(*args)
            except EndpointError as e:
                if attempt >= self.retries:
                    raise
                delay = self.retry_backoff * (2 ** attempt)
                attempt += 1
                msg = str(e).replace("\n", " ")
                self._log(
                    f"  [retry {attempt}/{self.retries}] EndpointError: "
                    f"{msg[:120]} — sleeping {delay:.1f}s"
                )
                time.sleep(delay)

    def _step_with_retry(self, payload: Any) -> Any:
        return self._retry_loop(self.step, payload)

    def _step_batch_with_retry(self, payloads: list) -> list:
        return self._retry_loop(self.step_batch, payloads)

    def _do_row(self, row, row_num: int = 0, n_total: int | None = None) -> str:
        """Returns row status ('ok', 'failed', or 'skipped')."""
        row_key = self._row_key(row)
        if row_key in self._ok_keys:
            return "skipped"
        display_key = row_key[:12] + "…" if len(row_key) > 12 else row_key
        counter = f"{row_num}/{n_total}" if n_total else str(row_num)
        tag = f"#{counter} {display_key}"
        status, error, category = "ok", None, None
        raw: Any = None
        extras: dict = {}
        payload: Any = None
        t0 = time.monotonic()
        self._log()
        try:
            payload = self.pre_step(row)
            pre_hint = self._fmt_payload(payload)
            if pre_hint:
                self._log(f"  [{tag}] pre:  {pre_hint}")
            raw = self._step_with_retry(payload)
            raw_hint = self._fmt_raw(raw) if raw is not None else ""
            if raw_hint:
                self._log(f"  [{tag}] raw:  {raw_hint}")
            extras = self.post_step(row, raw)
            post_hint = self._fmt_post(extras)
            if post_hint:
                self._log(f"  [{tag}] post: {post_hint}")
        except Exception as e:
            status = "failed"
            error = traceback.format_exc()
            category = type(e).__name__
        elapsed = _fmt_duration(time.monotonic() - t0)
        label = "ok" if status == "ok" else f"FAILED ({category})"
        self._log(f"  [{tag}] → {label}  ({elapsed})")
        extras = {**self._extras_floor(row, raw), **extras}
        extras["error_category"] = category
        self._write_row(row_key, status, error, extras)
        return status

    def step_batch(self, payloads: list) -> list:
        """Batch inference. Override in subclasses for true batching.
        Default: serial step() calls (correct for any protocol, fast for none)."""
        return [self.step(p) for p in payloads]

    def _do_batch(self, rows: list, start_idx: int, n_total: int | None) -> list[str]:
        """Process a batch of rows. Returns one status per row."""
        end_idx = start_idx + len(rows) - 1
        counter = f"#{start_idx}-{end_idx}/{n_total}" if n_total else f"#{start_idx}-{end_idx}"
        self._log()

        proc_idxs: list[int] = []
        proc_rows: list = []
        for i, row in enumerate(rows):
            if self._row_key(row) not in self._ok_keys:
                proc_idxs.append(i)
                proc_rows.append(row)

        final_statuses = ["skipped"] * len(rows)
        if not proc_rows:
            return final_statuses

        t0 = time.monotonic()
        n_ok = n_fail = 0
        failed_keys: list[str] = []

        def _fail(orig_i: int, row, tb: str, category: str) -> None:
            nonlocal n_fail
            fl = {**self._extras_floor(row, None), "error_category": category}
            self._write_row(self._row_key(row), "failed", tb, fl)
            final_statuses[orig_i] = "failed"
            failed_keys.append(self._row_key(row)[:12])
            n_fail += 1

        # pre_step per-row: an error here isolates one row, not the whole batch.
        good_idxs: list[int] = []
        good_rows: list = []
        payloads: list = []
        for orig_i, row in zip(proc_idxs, proc_rows):
            try:
                payloads.append(self.pre_step(row))
                good_idxs.append(orig_i)
                good_rows.append(row)
            except Exception as e:
                _fail(orig_i, row, traceback.format_exc(), type(e).__name__)

        # step_batch is all-or-nothing (can't attribute a batch call failure to a row).
        outs: list | None = None
        if good_rows:
            try:
                outs = self._step_batch_with_retry(payloads)
            except Exception as e:
                tb, category = traceback.format_exc(), type(e).__name__
                for orig_i, row in zip(good_idxs, good_rows):
                    _fail(orig_i, row, tb, category)

        if outs is not None:
            for orig_i, row, out in zip(good_idxs, good_rows, outs):
                row_key = self._row_key(row)
                try:
                    extras = self.post_step(row, out)
                    status, err_str, category = "ok", None, None
                    n_ok += 1
                except Exception as e:
                    extras = {}
                    status, err_str, category = "failed", traceback.format_exc(), type(e).__name__
                    n_fail += 1
                    failed_keys.append(row_key[:12])
                # preserve `out` in the floor even on post_step failure (matches serial)
                fl = {**self._extras_floor(row, out), **extras, "error_category": category}
                self._write_row(row_key, status, err_str, fl)
                final_statuses[orig_i] = status

        elapsed = _fmt_duration(time.monotonic() - t0)
        parts: list[str] = []
        if n_ok:
            parts.append(f"{n_ok} ok")
        if n_fail:
            sample = ", ".join(failed_keys[:5])
            if len(failed_keys) > 5:
                sample += f", +{len(failed_keys) - 5} more"
            parts.append(f"{n_fail} FAILED ({sample})")
        n_skipped = len(rows) - len(proc_rows)
        if n_skipped:
            parts.append(f"{n_skipped} skipped")
        self._log(f"  [{counter}] → {'  '.join(parts)}  ({elapsed})")
        return final_statuses

    # ---- entry ----
    def _pre_flight(self) -> None:
        """Dry one-row call BEFORE register, so config/budget/endpoint problems
        surface without burning a log row + N other rows. On success the result
        is cached and written after register — the model is not called twice."""
        self._log("[preflight] testing first row …")
        it = iter(self.data_source)
        try:
            first = next(it)
        except StopIteration:
            return
        t0 = time.monotonic()
        try:
            payload = self.pre_step(first)
            pre_hint = self._fmt_payload(payload)
            if pre_hint:
                self._log(f"  pre:  {pre_hint}")
            raw = self._step_with_retry(payload)
            raw_hint = self._fmt_raw(raw) if raw is not None else ""
            if raw_hint:
                self._log(f"  raw:  {raw_hint}")
            extras = self.post_step(first, raw)
            post_hint = self._fmt_post(extras)
            if post_hint:
                self._log(f"  post: {post_hint}")
        except RowInferError as e:
            # Re-raise as-is: wrapping in RuntimeError would erase the error
            # category callers dispatch on (ConfigError vs BudgetError ...).
            self._log(f"[preflight] FAILED ({type(e).__name__}): {e}")
            raise
        self._preflight_cache = (first, raw, extras, time.monotonic() - t0)
        self._log("[preflight] ok — proceeding (result will be reused for row 1)")
        # restore the row we consumed
        self.data_source = _prepend(first, it)

    def run(self) -> str:
        """Full lifecycle. Returns run_id. Assumes setup via set_* funcs done."""
        self.pre_run_check()
        self._snapshot = self._build_snapshot()
        n_rows = self._estimate_n_rows()
        if self.concurrency > 1:
            _mode = f"concurrent={self.concurrency}"
        elif self.batch_size > 1:
            _mode = f"batch={self.batch_size}"
        else:
            _mode = "serial"
        self._log(
            f"[{type(self).__name__}] model={self.model.name}"
            f"  model_type={self.model_type}"
            f"  rows={n_rows if n_rows is not None else '?'}"
            f"  limit={self.limit}"
            f"  mode={_mode}"
        )
        self._open_pool()
        try:
            return self._run_inner()
        finally:
            self._close_pool()

    def _run_inner(self) -> str:
        if self.preflight:
            self._pre_flight()
        self._ensure_out_table()
        self._ensure_column("error_category", "text")
        if self._resume_run_id:
            self._ok_keys = self._load_ok_keys()
            self._log(f"[resume] {len(self._ok_keys)} rows already ok — skipping")
            self._resume_register()
        else:
            self._register_run()

        n = 0
        n_failed = 0
        _n_prior_ok = len(self._ok_keys)
        _limit = self._effective_limit()
        n_total = self._pass_total()
        # counters shown to the user are cumulative across passes: "#430/1000"
        # = the 430th ok row of a 1000-row target, so a resume reads as
        # "picking up where we left off" instead of restarting from #1
        _disp_total = _n_prior_ok + n_total if n_total is not None else None
        _times: deque[float] = deque(maxlen=20)
        _consec = 0
        _early_stopped = False
        _proc_idx = 0  # rows sent for processing this pass; skipped rows don't advance it
        _last_bump = time.monotonic()
        _t_start = time.monotonic()

        # preflight already produced row 1's result — write it, don't re-call the model
        if self._preflight_cache is not None:
            _pf_row, _pf_raw, _pf_extras, _pf_elapsed = self._preflight_cache
            self._preflight_cache = None
            _pf_key = self._row_key(_pf_row)
            _pf_fl = {**self._extras_floor(_pf_row, _pf_raw), **_pf_extras,
                      "error_category": None}
            self._write_row(_pf_key, "ok", None, _pf_fl)
            self._ok_keys.add(_pf_key)
            n = 1
            _proc_idx = 1
            _times.append(_pf_elapsed)
            # preflight row completed before the loop timer started — count its time
            _t_start -= _pf_elapsed

        def _bump_now() -> None:
            nonlocal _last_bump
            self._bump_progress(_n_prior_ok + n, n_failed)
            _last_bump = time.monotonic()

        def _eta_print() -> None:
            if n % 10 == 0 and n and _times:
                recent = sum(_times) / len(_times)
                elapsed = time.monotonic() - _t_start
                # ETA from overall wall-clock rate, not the recent-latency window:
                # wall rate already includes concurrency/batch speedup, and one slow
                # stretch can't freeze the estimate ("half done but ETA unchanged").
                wall = elapsed / n
                # estimates can drift (source grew, key mismatch) — never negative
                remaining = max(0, n_total - n) * wall if n_total else None
                eta = f"~{_fmt_duration(remaining)}" if remaining is not None else "?"
                pct = f" ({100 * (_n_prior_ok + n) / _disp_total:.0f}%)" if _disp_total else ""
                self._log(
                    f"[eta] recent={recent:.1f}s  wall/row={wall:.1f}s"
                    f"  done={_n_prior_ok + n}/{_disp_total or '?'}{pct}"
                    f"  elapsed={_fmt_duration(elapsed)}  eta={eta}"
                )

        def _tick(row_status: str) -> None:
            nonlocal n, n_failed, _consec, _early_stopped
            if row_status == "skipped":
                return
            n += 1
            if row_status == "failed":
                n_failed += 1
                _consec += 1
                if self.early_stop_after and _consec >= self.early_stop_after:
                    _early_stopped = True
            else:
                _consec = 0
            # progress UPDATE throttled to one write per ~2s, not one per row
            if time.monotonic() - _last_bump >= 2.0:
                _bump_now()
            _eta_print()

        try:
            if self.concurrency > 1:
                from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
                _active: dict = {}
                _row_iter = iter(self.data_source)

                def _submit_one(_pool) -> bool:
                    nonlocal _proc_idx
                    # count in-flight rows too, else up to concurrency-1 rows overshoot limit
                    if _limit is not None and n + len(_active) >= _limit:
                        return False
                    while True:
                        try:
                            _row = next(_row_iter)
                        except StopIteration:
                            return False
                        if self._row_key(_row) not in self._ok_keys:
                            break
                    _proc_idx += 1
                    _f = _pool.submit(self._do_row, _row, _n_prior_ok + _proc_idx, _disp_total)
                    _active[_f] = time.monotonic()
                    return True

                with ThreadPoolExecutor(max_workers=self.concurrency) as _pool:
                    for _ in range(self.concurrency):
                        if not _submit_one(_pool):
                            break
                    while _active:
                        _done, _ = wait(list(_active.keys()), return_when=FIRST_COMPLETED)
                        for _f in _done:
                            _t0 = _active.pop(_f)
                            _elapsed = time.monotonic() - _t0
                            _row_status = _f.result()
                            if _row_status != "skipped":
                                _times.append(_elapsed)
                            _tick(_row_status)
                        if _early_stopped:
                            break
                        while len(_active) < self.concurrency:
                            if not _submit_one(_pool):
                                break

            elif self.batch_size > 1:
                _batch: list = []
                _batch_start = 1
                for row in self.data_source:
                    if _limit is not None and n >= _limit:
                        break
                    if self._row_key(row) in self._ok_keys:
                        continue
                    _proc_idx += 1
                    _batch.append(row)
                    # flush early when the batch would cross the limit
                    if len(_batch) >= self.batch_size or (
                        _limit is not None and n + len(_batch) >= _limit
                    ):
                        _t0 = time.monotonic()
                        _statuses = self._do_batch(_batch, _n_prior_ok + _batch_start, _disp_total)
                        _elapsed = time.monotonic() - _t0
                        _n_proc = sum(1 for s in _statuses if s != "skipped")
                        if _n_proc:
                            _times.append(_elapsed / _n_proc)
                        for _s in _statuses:
                            _tick(_s)
                        _batch = []
                        _batch_start = _proc_idx + 1
                        if _early_stopped:
                            break
                if _batch and not _early_stopped:
                    _t0 = time.monotonic()
                    _statuses = self._do_batch(_batch, _n_prior_ok + _batch_start, _disp_total)
                    _elapsed = time.monotonic() - _t0
                    _n_proc = sum(1 for s in _statuses if s != "skipped")
                    if _n_proc:
                        _times.append(_elapsed / _n_proc)
                    for _s in _statuses:
                        _tick(_s)

            else:
                for row in self.data_source:
                    if _limit is not None and n >= _limit:
                        break
                    if self._row_key(row) in self._ok_keys:
                        continue
                    _proc_idx += 1
                    t0 = time.monotonic()
                    row_status = self._do_row(row, _n_prior_ok + _proc_idx, _disp_total)
                    if row_status != "skipped":
                        _times.append(time.monotonic() - t0)
                    _tick(row_status)
                    if _early_stopped:
                        break

            self.post_run_check()
            if _early_stopped:
                msg = f"early stop: {_consec} consecutive failures"
                self._log(f"[run:{self.run_id}] {msg} — {n - n_failed} ok, {n_failed} failed")
                self._finalize("early_stopped", error=msg,
                               n_done=_n_prior_ok + n, n_failed=n_failed)
            else:
                self._finalize("complete", n_done=_n_prior_ok + n, n_failed=n_failed)
                self._log(f"[run:{self.run_id}] complete  {n - n_failed} ok, {n_failed} failed")
        except KeyboardInterrupt:
            self._finalize("interrupted", n_done=_n_prior_ok + n, n_failed=n_failed)
            self._log(f"[run:{self.run_id}] interrupted  {n} done, {n_failed} failed")
            raise
        except Exception:
            self._finalize("failed", error=traceback.format_exc(),
                           n_done=_n_prior_ok + n, n_failed=n_failed)
            raise

        assert self.run_id is not None
        return self.run_id
