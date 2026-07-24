"""trawler import — merge job.sqlite results back into Postgres.

Creates the gen output table if missing (same DDL a live run would create),
registers a run log row in gen._gen_log (fresh run_id, config carries the
job_id), and inserts every result row. Re-importing the same job is refused
unless force=True (the log is checked for the job_id).

Exports:
  import_bundle()     — full import
  _prepare_rows()     — pure: sqlite result dicts → gen-table row dicts (tests)
"""
from __future__ import annotations

import json
import sqlite3
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from trawler.dsn import resolve_dsn
from trawler.errors import ConfigError, ParseError
from trawler.generate.json_gen import _coerce_json

_STATUS_MAP = {"ok": "ok", "fail": "failed"}   # sqlite → gen-table status


def _completion_status(n_attempted: int, total: int, *, is_running: bool = False) -> str:
    """Offload-job status for the _gen_log row after an import.

    Precedence: liveness wins over coverage. 'running' while the job is
    confirmed live on the remote (regardless of how much is imported so
    far) — n_done/n_rows already carry the partial-coverage information,
    status carries process state. Only once the job is confirmed NOT live
    does incomplete coverage become 'partial'. 'complete' when every bundled
    row was attempted (ok or fail) — this always wins regardless of
    liveness, since a finished job can still show as "live" for a few
    seconds until the remote process exits.
    'interrupted' is never returned here — that status is reserved for a
    job confirmed PARKED in queue/interrupted/ (watchdog exit 2 / local ^C),
    which is remote-queue-state, not an import-time coverage question; see
    the item-4 sync helper.
    """
    if n_attempted >= total > 0:
        return "complete"
    return "running" if is_running else "partial"


def _prepare_rows(results: list[dict], expected_output: str) -> list[dict]:
    """Map sqlite result rows → gen-table row dicts (no DB).

    For 'j' prompts, ok rows are re-parsed; a row whose output no longer
    parses is demoted to failed/ParseError instead of aborting the import.
    """
    out: list[dict] = []
    for r in results:
        row: dict[str, Any] = {
            "row_key": r["row_key"],
            "status": _STATUS_MAP.get(r["status"], "failed"),
            "error": r.get("error"),
            "error_category": r.get("error_category"),
            "raw_output": r.get("output"),
            "doc": r.get("doc"),
            "json_output": None,
        }
        if expected_output == "j" and row["status"] == "ok":
            try:
                row["json_output"] = _coerce_json(r["output"])
            except ParseError as e:
                row["status"] = "failed"
                row["error"] = f"ParseError: {e}"
                row["error_category"] = "ParseError"
        out.append(row)
    return out


def _ensure_gen_table(conn, prompt_name: str, expected_output: str) -> None:
    """Mirror the DDL MinimalGenRun/JsonGenRun would create, so live runs
    and imports share one table."""
    json_col = '"json_output" jsonb,' if expected_output == "j" else ""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS gen."{prompt_name}" (
          run_id     uuid NOT NULL,
          row_key    text NOT NULL,
          status     text NOT NULL,
          error      text,
          "raw_output" text,
          "doc" text,
          "carry" jsonb,
          {json_col}
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (run_id, row_key)
        )
    """)
    conn.execute(
        f'ALTER TABLE gen."{prompt_name}" '
        'ADD COLUMN IF NOT EXISTS "error_category" text'
    )


def import_bundle(
    job_dir: str | Path,
    *,
    dsn: str | None = None,
    force: bool = False,
    is_running: bool = False,
) -> dict:
    """Import a finished bundle. Returns {"run_id", "ok", "failed", "table"}.

    is_running: caller-supplied liveness signal (queue/active task file OR a
    live run-bundle process on the remote, matched on job id — the same
    check `pull` already performs before rsyncing). Determines whether an
    incomplete-coverage import lands as 'running' (still live) or 'partial'
    (confirmed stopped). Default False so callers that don't check liveness
    (e.g. importing a bare directory with no remote) get the coverage-only
    behavior.
    """
    job_dir = Path(job_dir)
    toml_path = job_dir / "job.toml"
    if not toml_path.exists():
        raise ConfigError(f"no job.toml in {job_dir} — not a bundle dir?")
    with open(toml_path, "rb") as f:
        job = tomllib.load(f)

    job_id = job["job"]["id"]
    prompt_name = job["prompt"]["name"]
    expected_output = job["prompt"]["expected_output"]

    sqlite_path = job_dir / "job.sqlite"
    if not sqlite_path.exists():
        raise ConfigError(f"no job.sqlite in {job_dir}")
    sdb = sqlite3.connect(str(sqlite_path))
    sdb.row_factory = sqlite3.Row
    try:
        results = [dict(r) for r in sdb.execute("SELECT * FROM results")]
        meta = sdb.execute(
            "SELECT value FROM job_meta WHERE key='row_count'").fetchone()
    finally:
        sdb.close()
    if not results:
        raise ConfigError(f"job.sqlite has no results — run the bundle first "
                          f"(`trawler run-bundle {job_dir}`)")

    rows = _prepare_rows(results, expected_output)
    n_ok = sum(1 for r in rows if r["status"] == "ok")
    n_failed = len(rows) - n_ok
    run_id = str(uuid.uuid4())

    # A job is 'complete' only when every bundled row was attempted (ok or
    # fail); a partial import (the box was interrupted, or only some rows ran)
    # is 'interrupted' — NOT complete. This keeps a job whose main task is
    # unfinished visible in `trawler jobs` and safe from `clean --imported`.
    total = int(meta["value"]) if meta else len(rows)
    job_status = _completion_status(len(rows), total, is_running=is_running)

    dsn = resolve_dsn(dsn)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # One log row per job: bundle registers it as status='exported';
        # import updates that row. Re-importing a partial/still-running/
        # confirmed-stopped job (status in exported/running/partial/
        # interrupted) after more rows ran is legitimate — only a fully
        # 'complete' job is a true double-import, refused unless forced.
        existing = conn.execute(
            "SELECT run_id, status FROM gen._gen_log "
            "WHERE config->>'job_id' = %s ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if existing and existing["status"] == "complete" and not force:
            raise ConfigError(
                f"job {job_id!r} already fully imported as run "
                f"{existing['run_id']} — pass force=True / --force to import again"
            )

        _ensure_gen_table(conn, prompt_name, expected_output)

        config = {
            "job_id": job_id,
            "offload": True,
            "stage": "imported",
            "imported": True,
            "model_type": job["model_type"]["name"],
            "trawler_version": job["job"].get("trawler_version"),
            "bundle_created_at": job["job"].get("created_at"),
        }
        if existing and existing["status"] in ("exported", "interrupted", "running", "partial"):
            run_id = str(existing["run_id"])   # update the registered row in place
            conn.execute(
                "UPDATE gen._gen_log SET status=%s, ended_at=%s, "
                "n_rows=%s, n_done=%s, n_failed=%s, "
                "config = config || %s::jsonb WHERE run_id=%s",
                # n_rows = the job's TOTAL (job_meta.row_count), not the count
                # imported — so an interrupted job reads n_done/n_rows = 2019/487947.
                (job_status, datetime.now(timezone.utc), total, n_ok, n_failed,
                 json.dumps(config, default=str), run_id),
            )
        else:  # pre-registration bundle (or forced re-import): fresh row
            conn.execute(
                "INSERT INTO gen._gen_log "
                "(run_id, name, model, status, started_at, ended_at, "
                " n_rows, n_done, n_failed, source_table, "
                " system_prompt_content, config) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (run_id, job_id, job["decoder"]["name"], job_status,
                 datetime.now(timezone.utc), datetime.now(timezone.utc),
                 total, n_ok, n_failed,          # n_rows = job total, not imported count
                 job["source"]["table"],
                 job["prompt"].get("content"),  # from job.toml — what actually ran
                 json.dumps(config, default=str)),
            )

        cols = ["run_id", "row_key", "status", "error", "error_category",
                "raw_output", "doc", "json_output"]
        has_json = expected_output == "j"
        if not has_json:
            cols.remove("json_output")
        col_list = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join(["%s"] * len(cols))
        update_set = ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in cols
                               if c not in ("run_id", "row_key"))
        # ON CONFLICT upserts over 'pending' placeholder rows written by bundle.
        # created_at is bumped on conflict (inherits from the INSERT default)
        # because the claim timestamp has no downstream readers and bumping
        # simplifies the SQL — the placeholder's created_at is not meaningful.
        sql = (f'INSERT INTO gen."{prompt_name}" ({col_list}) VALUES ({ph}) '
               f"ON CONFLICT (run_id, row_key) DO UPDATE SET {update_set}")
        for r in rows:
            vals: list[Any] = [run_id, r["row_key"], r["status"], r["error"],
                               r["error_category"], r["raw_output"], r["doc"]]
            if has_json:
                vals.append(Jsonb(r["json_output"])
                            if r["json_output"] is not None else None)
            conn.execute(sql, vals)
        # Count rows still 'pending' after import — these were claimed at bundle
        # time but never attempted remotely (e.g. box interrupted before starting
        # them). Log the count so the user can investigate; leave them in place.
        pending_row = conn.execute(
            f'SELECT COUNT(*) AS n FROM gen."{prompt_name}" '
            "WHERE run_id=%s AND status='pending'",
            (run_id,),
        ).fetchone()
        n_still_pending = pending_row["n"] if pending_row else 0
        conn.commit()

    result: dict = {"run_id": run_id, "ok": n_ok, "failed": n_failed,
                    "table": f'gen."{prompt_name}"',
                    "status": job_status, "total": total}
    if n_still_pending:
        import warnings
        warnings.warn(
            f"import: {n_still_pending} row(s) still 'pending' after import "
            f"(run_id={run_id}) — claimed at bundle time but never attempted "
            "remotely; re-enqueue the job or clean to release claims",
            stacklevel=2,
        )
        result["n_still_pending"] = n_still_pending
    return result
