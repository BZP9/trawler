"""Run log inspection helpers.

Query gen._gen_log / enc._enc_log without writing SQL. Covers the most
common post-run questions: what ran, what failed, breakdown by error type.

DSN resolution: explicit `dsn` param > TRAWLER_DSN env var > ROWINFER_DSN (back-compat).
"""
from __future__ import annotations

import re

import psycopg
from psycopg.rows import dict_row

from trawler.dsn import resolve_dsn


_LOG_TABLES = {"gen": "gen._gen_log", "enc": "enc._enc_log"}
_VALID_STATUSES = frozenset(
    {"running", "complete", "failed", "interrupted", "early_stopped", "exported"}
)


def _conn(dsn: str | None):
    return psycopg.connect(resolve_dsn(dsn), row_factory=dict_row)


def _log_table(schema: str) -> str:
    if schema not in _LOG_TABLES:
        raise ValueError("schema must be 'gen' or 'enc'")
    return _LOG_TABLES[schema]


def _derive_out_table(log_row: dict, schema: str) -> str:
    """Derive the output table name from a log row's config snapshot.

    Live runs snapshot config["system_prompt"]["name"]. Offload bundles
    write a different config shape ({"offload": True, "prompt": ..., ...});
    older bundles lack the "prompt" key, but their log name is
    "<prompt>-<YYYYMMDDTHHMMSSZ>" so the prompt is recoverable.
    """
    if schema == "gen":
        config = log_row.get("config") or {}
        if "system_prompt" in config:
            name = config["system_prompt"]["name"]
        elif "prompt" in config:
            name = config["prompt"]
        elif config.get("offload"):
            name = re.sub(r"-\d{8}T\d{6}Z$", "", log_row["name"])
        else:
            raise ValueError(
                f"cannot derive out table for run {log_row.get('run_id')}: "
                "config has neither 'system_prompt' nor offload markers"
            )
    else:
        name = log_row["model"]
    return f'{schema}."{name}"'


# ---- listing ----------------------------------------------------------------

def list_runs(
    schema: str = "gen",
    *,
    status: str | None = None,
    limit: int = 20,
    dsn: str | None = None,
) -> list[dict]:
    """List recent runs from gen._gen_log or enc._enc_log.

    Returns summary cols only (no config blob). Most recent first.
    """
    log = _log_table(schema)
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")
    where = "WHERE status=%s " if status else ""
    params: list = [status] if status else []
    params.append(limit)
    with _conn(dsn) as conn:
        return conn.execute(
            f"SELECT run_id, name, model, status, "
            f"started_at, ended_at, n_rows, n_done, n_failed "
            f"FROM {log} {where}ORDER BY started_at DESC LIMIT %s",
            params,
        ).fetchall()


# ---- single run -------------------------------------------------------------

def get_run(
    run_id: str,
    *,
    schema: str = "gen",
    dsn: str | None = None,
) -> dict | None:
    """Fetch the full log row for a run_id, including config snapshot."""
    log = _log_table(schema)
    with _conn(dsn) as conn:
        return conn.execute(
            f"SELECT * FROM {log} WHERE run_id=%s", (run_id,)
        ).fetchone()


def run_stats(
    run_id: str,
    *,
    schema: str = "gen",
    dsn: str | None = None,
) -> dict:
    """Summary stats for a run: progress + error breakdown by category.

    Derives the output table name from the log row's config snapshot so the
    caller doesn't need to know the table name.

    Returns:
        run_id, name, status, n_rows, n_done, n_failed, pct_done,
        out_table, by_category {error_category: count}
    """
    log = _log_table(schema)
    with _conn(dsn) as conn:
        log_row = conn.execute(
            f"SELECT * FROM {log} WHERE run_id=%s", (run_id,)
        ).fetchone()
        if log_row is None:
            raise ValueError(f"run_id {run_id!r} not found in {log}")

        out_table = _derive_out_table(log_row, schema)
        by_category: dict[str, int] = {}
        try:
            cats = conn.execute(
                f"SELECT error_category, COUNT(*) AS n "
                f"FROM {out_table} "
                f"WHERE run_id=%s AND status='failed' "
                f"GROUP BY error_category ORDER BY n DESC",
                (run_id,),
            ).fetchall()
            by_category = {(r["error_category"] or "unknown"): r["n"] for r in cats}
        except Exception:
            pass  # out table may not exist yet (preflight failure)

    n_done = log_row["n_done"] or 0
    n_rows = log_row["n_rows"]
    pct = round(100 * n_done / n_rows, 1) if n_rows else None

    return {
        "run_id": run_id,
        "name": log_row["name"],
        "status": log_row["status"],
        "n_rows": n_rows,
        "n_done": n_done,
        "n_failed": log_row["n_failed"] or 0,
        "pct_done": pct,
        "out_table": out_table,
        "by_category": by_category,
    }


def failed_rows(
    run_id: str,
    *,
    out_table: str | None = None,
    schema: str = "gen",
    limit: int = 100,
    dsn: str | None = None,
) -> list[dict]:
    """Fetch rows where status='failed' for a run.

    out_table: fully-qualified ('gen.extract_skills') or bare ('extract_skills').
    If omitted, derived from the log row's config snapshot.
    """
    with _conn(dsn) as conn:
        if out_table is None:
            log = _log_table(schema)
            log_row = conn.execute(
                f"SELECT * FROM {log} WHERE run_id=%s", (run_id,)
            ).fetchone()
            if log_row is None:
                raise ValueError(f"run_id {run_id!r} not found in {log}")
            table = _derive_out_table(log_row, schema)
        else:
            if "." not in out_table:
                table = f'{schema}."{out_table}"'
            else:
                s, n = out_table.split(".", 1)
                table = f'{s}."{n.strip(chr(34))}"'

        return conn.execute(
            f"SELECT * FROM {table} "
            f"WHERE run_id=%s AND status='failed' "
            f"ORDER BY created_at LIMIT %s",
            (run_id, limit),
        ).fetchall()
