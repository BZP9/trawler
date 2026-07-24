"""trawler bundle — snapshot a self-contained offline generation job.

Exports:
  bundle()          — connects to Postgres, fetches cfg + pending rows, writes job dir
  _compute_pending() — pure: filter source rows to pending (used directly in tests)
  _write_bundle()    — pure: write job.toml / rows.jsonl / job.sqlite (used directly in tests)
"""
from __future__ import annotations

import importlib.metadata
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from trawler.dsn import resolve_dsn
from trawler.errors import ConfigError
from trawler.run.base import _compute_row_key


# ---------------------------------------------------------------------------
# Table-name validation (injection guard for user-supplied source table)
# ---------------------------------------------------------------------------

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_SAFE_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}(\.[A-Za-z0-9_-]{1,63})?$")


def _check_source_table(table: str) -> None:
    if not _SAFE_TABLE_RE.match(table):
        raise ConfigError(
            f"invalid source table {table!r}: use schema.table (or bare table) "
            "with only letters, digits, _ or -, max 63 chars per part"
        )


def _quote_table(table: str) -> str:
    """Return a safely-quoted table reference: schema.table → "schema"."table"."""
    parts = table.split(".", 1)
    return ".".join(f'"{p}"' for p in parts)


# ---------------------------------------------------------------------------
# TOML serializer (stdlib-only, no tomli_w dep)
# ---------------------------------------------------------------------------

def _toml_str(v: str) -> str:
    """Escape a Python string as a TOML basic string (single-line)."""
    v = v.replace("\\", "\\\\")
    v = v.replace('"', '\\"')
    v = v.replace("\n", "\\n")
    v = v.replace("\r", "\\r")
    v = v.replace("\t", "\\t")
    return f'"{v}"'


def _toml_val(v: Any) -> str:
    """Recursively serialize a Python value to a TOML inline value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        pairs = ", ".join(f"{k} = {_toml_val(val)}" for k, val in v.items())
        return "{" + pairs + "}"
    return _toml_str(str(v))


def _write_toml(path: Path, sections: list[tuple[str, dict[str, Any]]]) -> None:
    """Write a TOML file from an ordered list of (section_name, {key: value}) pairs.

    None values are omitted. Sections with no non-None keys are still written
    (to preserve section order), but will have only the header line.
    """
    parts: list[str] = []
    for section_name, data in sections:
        lines = [f"[{section_name}]"]
        for k, v in data.items():
            if v is None:
                continue
            lines.append(f"{k} = {_toml_val(v)}")
        parts.append("\n".join(lines))
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Pending-row computation (pure, no DB)
# ---------------------------------------------------------------------------

def _compute_pending(
    source_rows: list[dict],
    ok_keys: set[str],
    pk: str | list[str],
    limit: int | None,
) -> tuple[list[dict], int]:
    """Return (pending_rows, n_dups) where pending_rows are source rows whose
    row_key is NOT in ok_keys (or already claimed as pending), capped by limit.

    Deduplicates within the pending set by row_key: keeps the first occurrence,
    counts and warns about duplicates (e.g. gen-table sources with multiple
    run_ids per row_key). The warning is always emitted so the user sees it at
    bundle time.

    Row-key encoding mirrors BaseRun._row_key (via _compute_row_key) so that
    keys written to job.sqlite match the keys in the gen output table.
    """
    import warnings
    seen_keys: set[str] = set()
    pending: list[dict] = []
    n_dups = 0
    for row in source_rows:
        rk = _compute_row_key(pk, row)
        if rk in ok_keys:
            continue
        if rk in seen_keys:
            n_dups += 1
            continue
        seen_keys.add(rk)
        pending.append(row)
    if n_dups:
        warnings.warn(
            f"bundle: source contains {n_dups} duplicate row_key(s) — "
            f"deduped to {len(pending)} distinct rows; check your source table "
            "for multiple runs producing the same row_key",
            stacklevel=2,
        )
    if limit is not None:
        pending = pending[:limit]
    return pending, n_dups


# ---------------------------------------------------------------------------
# File-writing (pure, no DB)
# ---------------------------------------------------------------------------

def _write_bundle(
    *,
    job_id: str,
    run_id: str | None = None,
    trawler_version: str,
    created_at: str,
    prompt: dict,
    decoder: dict,
    model_type: dict,
    pending_rows: list[dict],
    source_table: str,
    pk: str | list[str],
    doc_cols: list[str] | None = None,
    limit: int | None = None,
    out_dir: Path,
) -> Path:
    """Write job.toml, rows.jsonl, and job.sqlite into out_dir.

    All arguments are pre-fetched Python values — no DB calls here.
    Returns out_dir for convenience.

    run_id: the UUID minted for this job's gen._gen_log row (also used as the
    run_id for claim rows in the gen table). Written into job.toml so that
    `trawler clean` can look it up without a DB query.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pk_list = [pk] if isinstance(pk, str) else list(pk)

    # ---- job.toml ----
    decoder_data: dict[str, Any] = {
        "name": decoder["name"],
        "repo_name": decoder["repo_name"],
    }
    if decoder.get("format") is not None:
        decoder_data["format"] = decoder["format"]

    model_type_data: dict[str, Any] = {
        "name": model_type["name"],
        "protocol": model_type["protocol"],
        "base_url_env": model_type.get("base_url_env"),
        "api_key_env": model_type.get("api_key_env"),
    }

    sections: list[tuple[str, dict[str, Any]]] = [
        ("job", {
            "id": job_id,
            "run_id": run_id,
            "created_at": created_at,
            "trawler_version": trawler_version,
        }),
        ("prompt", {
            "name": prompt["name"],
            "content": prompt["content"],
            "expected_output": prompt["expected_output"],
        }),
        ("decoder", decoder_data),
        ("model_type", model_type_data),
        ("source", {
            "table": source_table,
            "pk": pk_list,
            "doc_cols": doc_cols,
            "row_count": len(pending_rows),
        }),
    ]
    if limit is not None:
        sections.append(("run", {"limit": limit}))

    _write_toml(out_dir / "job.toml", sections)

    # ---- rows.jsonl ----
    with open(out_dir / "rows.jsonl", "w", encoding="utf-8") as f:
        for row in pending_rows:
            f.write(json.dumps(row, default=str) + "\n")

    # ---- job.sqlite ----
    db_path = out_dir / "job.sqlite"
    with sqlite3.connect(str(db_path)) as db:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS job_meta("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS results("
            "  row_key TEXT PRIMARY KEY,"
            "  output TEXT,"
            "  doc TEXT,"
            "  status TEXT NOT NULL CHECK(status IN ('ok','fail')),"
            "  error TEXT,"
            "  error_category TEXT,"
            "  attempts INTEGER NOT NULL DEFAULT 0,"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ");"
        )
        db.executemany(
            "INSERT OR REPLACE INTO job_meta VALUES (?, ?)",
            [
                ("job_id", job_id),
                ("prompt_name", prompt["name"]),
                ("source_table", source_table),
                ("row_count", str(len(pending_rows))),
                ("created_at", created_at),
                ("trawler_version", trawler_version),
            ],
        )

    return out_dir


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def bundle(
    prompt_name: str,
    decoder_name: str,
    model_type_name: str,
    source_table: str,
    pk: str | list[str],
    *,
    doc_cols: list[str] | None = None,
    limit: int | None = None,
    out: str | Path = "output/jobs",
    dsn: str | None = None,
    dry_run: bool = False,
) -> Path | dict:
    """Export a self-contained offline job directory.

    Connects to Postgres to:
      - snapshot cfg rows (system_prompt, decoder, model_type)
      - compute pending source rows (source_table minus already-ok gen rows)
    Writes <out>/<job-id>/ with job.toml, rows.jsonl, job.sqlite.

    Returns the job directory path.
    base_url is NEVER written to job.toml — the remote resolves it from env.

    dry_run: compute and return the resolved recipe + row counts WITHOUT any
    side effects — no job dir, no gen._gen_log INSERT, no claim rows written.
    Returns a dict: {"pending": int, "total": int, "claimed": int, "config": {...}}
    instead of a Path.
    """
    _check_source_table(source_table)
    dsn = resolve_dsn(dsn)

    try:
        version = importlib.metadata.version("trawler")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # -- cfg rows --
        prompt_row = conn.execute(
            "SELECT name, content, expected_output "
            "FROM cfg.system_prompt WHERE name=%s",
            (prompt_name,),
        ).fetchone()
        if prompt_row is None:
            raise ConfigError(f"cfg.system_prompt {prompt_name!r} not found")

        decoder_row = conn.execute(
            "SELECT name, repo_name, format "
            "FROM cfg.decoder WHERE name=%s",
            (decoder_name,),
        ).fetchone()
        if decoder_row is None:
            raise ConfigError(f"cfg.decoder {decoder_name!r} not found")

        model_type_row = conn.execute(
            "SELECT name, protocol, base_url_env, api_key_env "
            "FROM cfg.model_type WHERE name=%s",
            (model_type_name,),
        ).fetchone()
        if model_type_row is None:
            raise ConfigError(f"cfg.model_type {model_type_name!r} not found")

        # -- ok/pending keys from gen output table (may not exist yet) --
        ok_keys: set[str] = set()
        try:
            ok_rows = conn.execute(
                f"SELECT DISTINCT row_key FROM gen.\"{prompt_name}\" "
                "WHERE status IN ('ok','pending')",
            ).fetchall()
            ok_keys = {r["row_key"] for r in ok_rows}
        except Exception:
            # Gen table doesn't exist yet (first run) — all rows are pending.
            # Roll back: the failed SELECT aborts the whole transaction and
            # every later query on this conn would raise InFailedSqlTransaction.
            conn.rollback()

        # -- source rows --
        quoted_src = _quote_table(source_table)
        source_rows = conn.execute(f"SELECT * FROM {quoted_src}").fetchall()
        source_rows_dicts = [dict(r) for r in source_rows]

    pending_rows, _n_dups = _compute_pending(source_rows_dicts, ok_keys, pk, limit)

    pk_list_for_config = [pk] if isinstance(pk, str) else list(pk)

    if dry_run:
        # Zero side effects: no job dir, no gen._gen_log INSERT, no claim rows.
        return {
            "pending": len(pending_rows),
            "total": len(source_rows_dicts),
            "claimed": len(ok_keys),
            "config": {
                "prompt": prompt_name,
                "decoder": decoder_name,
                "model_type": model_type_name,
                "source_table": source_table,
                "pk": pk_list_for_config,
                "doc_cols": doc_cols,
                "limit": limit,
            },
        }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_id = f"{prompt_name}-{ts}"
    created_at = datetime.now(timezone.utc).isoformat()
    out_dir = Path(out) / job_id

    # Register the job in the run log so pending offload work is visible
    # from the control plane (status='exported'; import completes this row).
    # In the same transaction, insert claim rows (status='pending') into the
    # gen table for every shipped row_key. This prevents a second concurrent
    # bundle from double-shipping the same rows (they will be excluded by the
    # status IN ('ok','pending') filter above). A failed bundle leaves no
    # orphan claims because the INSERT and _gen_log row share one transaction.
    bundle_run_id: str | None = None
    if pending_rows:
        bundle_run_id = str(uuid.uuid4())
        run_id = bundle_run_id
        pk_list = [pk] if isinstance(pk, str) else list(pk)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO gen._gen_log "
                "(run_id, name, model, status, started_at, n_rows, n_done, "
                " source_table, system_prompt_content, config) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (run_id, job_id, decoder_name, "exported",
                 datetime.now(timezone.utc), len(pending_rows), 0,
                 source_table,
                 prompt_row["content"],   # frozen into job.toml at export —
                                          # audit what the remote will run with
                 json.dumps({"job_id": job_id, "offload": True,
                             "prompt": prompt_name,
                             "stage": "exported",
                             "model_type": model_type_name,
                             "pk": pk_list,
                             "doc_cols": doc_cols})),
            )
            # Ensure the gen table exists before claiming rows.
            # Mirrors the DDL in importer._ensure_gen_table.
            expected_output = prompt_row.get("expected_output", "t")
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
            # Insert one placeholder per shipped row_key (status='pending').
            # ON CONFLICT DO NOTHING is a safety net in case of a race; normally
            # every row_key here is fresh because we just excluded ok/pending.
            claim_sql = (
                f'INSERT INTO gen."{prompt_name}" (run_id, row_key, status) '
                "VALUES (%s, %s, 'pending') ON CONFLICT (run_id, row_key) DO NOTHING"
            )
            for row in pending_rows:
                rk = _compute_row_key(pk_list if len(pk_list) > 1 else pk_list[0], row)
                conn.execute(claim_sql, (run_id, rk))

    return _write_bundle(
        job_id=job_id,
        run_id=bundle_run_id,
        trawler_version=version,
        created_at=created_at,
        prompt=dict(prompt_row),
        decoder=dict(decoder_row),
        model_type=dict(model_type_row),
        pending_rows=pending_rows,
        source_table=source_table,
        pk=pk,
        doc_cols=doc_cols,
        limit=limit,
        out_dir=out_dir,
    )
