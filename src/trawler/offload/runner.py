"""trawler run-bundle — execute a bundled job dir locally, no Postgres.

Reads job.toml + rows.jsonl, calls the model endpoint (base_url resolved
from THIS machine's env via the base_url_env named in job.toml), writes
per-row results into job.sqlite. Resumable: rows already 'ok' in
job.sqlite are skipped, 'fail' rows are retried (attempts incremented).

Exports:
  run_bundle()   — full execution
  _load_job()    — pure-ish: parse job.toml into a JobSpec (used in tests)
  _build_doc()   — pure: user message from row + doc_cols (used in tests)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from trawler.errors import ConfigError, EndpointError

logger = logging.getLogger("trawler")
from trawler.generate.json_gen import _coerce_json
from trawler.model.clients import call
from trawler.model.types import DecoderConfig, ResolvedEndpoint
from trawler.run.base import _compute_row_key, _ensure_default_handler


@dataclass
class JobSpec:
    job_id: str
    prompt_name: str
    system: str
    expected_output: str            # 't' | 'j'
    decoder: DecoderConfig
    model_type: str
    protocol: str
    base_url_env: str | None
    api_key_env: str | None
    pk: str | list[str]
    doc_cols: list[str] = field(default_factory=list)


def _load_job(job_dir: Path) -> JobSpec:
    """Parse job.toml into a JobSpec. No env resolution, no DB."""
    toml_path = job_dir / "job.toml"
    if not toml_path.exists():
        raise ConfigError(f"no job.toml in {job_dir} — not a bundle dir?")
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    src = data["source"]
    pk_list = src["pk"]
    pk: str | list[str] = pk_list[0] if len(pk_list) == 1 else pk_list
    doc_cols = src.get("doc_cols") or []
    if not doc_cols:
        raise ConfigError(
            "job.toml has no source.doc_cols — re-export with "
            "`trawler bundle --doc-col <COL>...` (older bundles can't say "
            "how to build the user message)"
        )

    dec = data["decoder"]
    mt = data["model_type"]
    return JobSpec(
        job_id=data["job"]["id"],
        prompt_name=data["prompt"]["name"],
        system=data["prompt"]["content"],
        expected_output=data["prompt"]["expected_output"],
        decoder=DecoderConfig(name=dec["name"], repo_name=dec["repo_name"],
                              format=dec.get("format")),
        model_type=mt["name"],
        protocol=mt["protocol"],
        base_url_env=mt.get("base_url_env"),
        api_key_env=mt.get("api_key_env"),
        pk=pk,
        doc_cols=list(doc_cols),
    )


def _resolve_endpoint(spec: JobSpec) -> ResolvedEndpoint:
    """Resolve base_url/api_key from THIS machine's env."""
    if not spec.base_url_env:
        raise ConfigError(
            f"model_type {spec.model_type!r} has no base_url_env — "
            "chat protocols need one"
        )
    base_url = os.environ.get(spec.base_url_env)
    if not base_url:
        raise ConfigError(
            f"env var {spec.base_url_env} not set on this machine "
            f"(required by model_type {spec.model_type!r})"
        )
    api_key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
    return ResolvedEndpoint(model_type=spec.model_type, protocol=spec.protocol,
                            base_url=base_url, api_key=api_key)


def _build_doc(row: dict, doc_cols: list[str]) -> str:
    """User message = doc_cols joined by newline (mirrors set_doc_fn(list))."""
    try:
        return "\n".join(str(row[c]) for c in doc_cols)
    except KeyError as e:
        raise ConfigError(f"doc col {e.args[0]!r} missing from bundled row") from None


def _do_row(spec: JobSpec, endpoint: ResolvedEndpoint, row: dict,
            params: dict, retries: int, backoff: float) -> tuple[str, dict]:
    """Process one row. Returns (row_key, result-dict for the sqlite write).
    Never raises — failures land in the result dict, mirroring BaseRun._do_row.
    """
    row_key = _compute_row_key(spec.pk, row)
    doc: str | None = None
    try:
        doc = _build_doc(row, spec.doc_cols)
        attempt = 0
        while True:
            try:
                raw = call(spec.decoder, endpoint, spec.system, doc, params)
                break
            except EndpointError:
                attempt += 1
                if attempt > retries:
                    raise
                time.sleep(backoff * 2 ** (attempt - 1))
        if spec.expected_output == "j":
            _coerce_json(raw)          # validate now; import re-parses to jsonb
        return row_key, {"output": raw, "doc": doc, "status": "ok",
                         "error": None, "error_category": None}
    except Exception as e:  # noqa: BLE001 — batch continues on any row error
        return row_key, {"output": None, "doc": doc, "status": "fail",
                         "error": f"{type(e).__name__}: {e}",
                         "error_category": type(e).__name__}


def run_bundle(
    job_dir: str | Path,
    *,
    concurrency: int = 1,
    retries: int = 0,
    backoff: float = 2.0,
    limit: int | None = None,
    early_stop: int | None = 10,
    params: dict | None = None,
    verbose: bool = True,
) -> dict:
    """Execute the bundle. Returns {"ok": n, "fail": n, "skipped": n, "total": n}.

    early_stop: abort the pass after N consecutive failures (None disables) —
    mirrors set_early_stop on live runs, so a dead endpoint fails fast instead
    of grinding a timeout per pending row. Aborting is safe: the pass is
    resumable, already-written results stay in job.sqlite.
    """
    job_dir = Path(job_dir)
    spec = _load_job(job_dir)
    endpoint = _resolve_endpoint(spec)
    params = dict(params or {})

    _ensure_default_handler()

    def _log(msg: str) -> None:
        if verbose:
            logger.info(msg)

    rows: list[dict] = []
    with open(job_dir / "rows.jsonl", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))

    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    try:
        ok_keys = {r[0] for r in db.execute(
            "SELECT row_key FROM results WHERE status='ok'")}
        prior_attempts = dict(db.execute("SELECT row_key, attempts FROM results"))

        pending = [r for r in rows
                   if _compute_row_key(spec.pk, r) not in ok_keys]
        if limit is not None:
            pending = pending[:limit]

        _log(f"[run-bundle] {spec.job_id}  model={spec.decoder.name}  "
             f"endpoint={endpoint.base_url}")
        _log(f"[run-bundle] rows={len(rows)}  ok-already={len(ok_keys)}  "
             f"pending={len(pending)}  concurrency={concurrency}")

        n_ok = n_fail = 0
        consec_fail = 0
        stopped_early = False

        def _write(row_key: str, res: dict) -> None:
            nonlocal n_ok, n_fail, consec_fail
            attempts = prior_attempts.get(row_key, 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO results"
                "(row_key, output, doc, status, error, error_category, attempts,"
                " updated_at) VALUES (?,?,?,?,?,?,?, datetime('now'))",
                (row_key, res["output"], res["doc"], res["status"],
                 res["error"], res["error_category"], attempts),
            )
            db.commit()
            if res["status"] == "ok":
                n_ok += 1
                consec_fail = 0
            else:
                n_fail += 1
                consec_fail += 1
            _log(f"  [{row_key}] → {res['error_category'] or 'ok'}")

        def _tripped() -> bool:
            return early_stop is not None and consec_fail >= early_stop

        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futs = [pool.submit(_do_row, spec, endpoint, r, params,
                                    retries, backoff) for r in pending]
                for fut in as_completed(futs):
                    if _tripped():
                        # cancel whatever hasn't started; in-flight rows finish
                        for f in futs:
                            f.cancel()
                        stopped_early = True
                        break
                    _write(*fut.result())
        else:
            for r in pending:
                if _tripped():
                    stopped_early = True
                    break
                _write(*_do_row(spec, endpoint, r, params, retries, backoff))

        if stopped_early:
            _log(f"[run-bundle] EARLY STOP after {consec_fail} consecutive "
                 f"failures — endpoint likely down. Re-run to resume.")
        _log(f"[run-bundle] {'stopped' if stopped_early else 'complete'}  "
             f"{n_ok} ok, {n_fail} failed, {len(ok_keys)} skipped")
        return {"ok": n_ok, "fail": n_fail,
                "skipped": len(ok_keys), "total": len(rows),
                "stopped_early": stopped_early}
    finally:
        db.close()
