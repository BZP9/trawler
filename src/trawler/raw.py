"""Raw schema table management and multi-source data loading.

Helpers for creating raw.* tables and bulk-loading data from CSV, JSONL,
or another Postgres table. Designed for the ingestion step before a
gen/enc pipeline run.

DSN resolution: explicit `dsn` param > TRAWLER_DSN env var > ROWINFER_DSN (back-compat).
"""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
from typing import Any, Iterator, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from trawler.dsn import resolve_dsn


_DEFAULT_SCHEMA = "raw"

# bool must come before int — bool is subclass of int in Python
_PY_TO_PG: list[tuple[type, str]] = [
    (bool,  "boolean"),
    (int,   "bigint"),
    (float, "double precision"),
    (dict,  "jsonb"),
    (list,  "jsonb"),
    (str,   "text"),
]


def _conn(dsn: str | None):
    return psycopg.connect(resolve_dsn(dsn), row_factory=dict_row)


def _fqn(table: str) -> str:
    """'jobs' → 'raw."jobs"',  'gen.extract_skills' → 'gen."extract_skills"'."""
    if "." in table:
        schema, name = table.split(".", 1)
        return f'{schema}."{name.strip(chr(34))}"'
    return f'{_DEFAULT_SCHEMA}."{table}"'


def _pg_val(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return Jsonb(v)
    return v


# ---- schema inference -------------------------------------------------------

def infer_columns(rows: list[dict]) -> dict[str, str]:
    """Infer {col: pg_type} from a sample of rows.

    Columns with all-null values default to text.
    """
    if not rows:
        raise ValueError("need at least one row to infer columns")
    seen: dict[str, str] = {}
    order: list[str] = []
    for row in rows:
        for col, val in row.items():
            if col not in order:
                order.append(col)
            if col in seen or val is None:
                continue
            for py_type, pg_type in _PY_TO_PG:
                if type(val) is py_type:
                    seen[col] = pg_type
                    break
    return {col: seen.get(col, "text") for col in order}


# ---- DDL --------------------------------------------------------------------

def create_table(
    table: str,
    columns: dict[str, str],
    *,
    pk: str | list[str] | None = None,
    if_not_exists: bool = True,
    dsn: str | None = None,
) -> None:
    """CREATE TABLE raw.<table> with explicit column spec.

    columns: {"col": "sql_type", ...}  e.g. {"id": "bigint", "title": "text"}
    pk:      column name or list of names for PRIMARY KEY. None = no PK.
    """
    fqn = _fqn(table)
    col_defs = [f'"{c}" {t}' for c, t in columns.items()]
    if pk:
        pk_cols = [pk] if isinstance(pk, str) else list(pk)
        pk_str = ", ".join(f'"{c}"' for c in pk_cols)
        col_defs.append(f"PRIMARY KEY ({pk_str})")
    exist = "IF NOT EXISTS " if if_not_exists else ""
    ddl = f"CREATE TABLE {exist}{fqn} (\n  " + ",\n  ".join(col_defs) + "\n)"
    with _conn(dsn) as conn:
        conn.execute(ddl)


def drop_table(table: str, *, dsn: str | None = None) -> None:
    """DROP TABLE IF EXISTS raw.<table>."""
    with _conn(dsn) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {_fqn(table)}")


# ---- bulk insert internals --------------------------------------------------

def _ensure_table(
    conn,
    fqn: str,
    columns: dict[str, str],
    pk: str | list[str] | None,
) -> None:
    col_defs = [f'"{c}" {t}' for c, t in columns.items()]
    if pk:
        pk_cols = [pk] if isinstance(pk, str) else list(pk)
        pk_str = ", ".join(f'"{c}"' for c in pk_cols)
        col_defs.append(f"PRIMARY KEY ({pk_str})")
    ddl = "CREATE TABLE IF NOT EXISTS " + fqn + " (\n  " + ",\n  ".join(col_defs) + "\n)"
    conn.execute(ddl)


def _insert_batch(
    conn,
    fqn: str,
    batch: list[dict],
    *,
    on_conflict: Literal["error", "skip", "replace"] = "error",
    pk_cols: list[str] | None = None,
) -> int:
    if not batch:
        return 0
    if on_conflict == "replace" and not pk_cols:
        raise ValueError("on_conflict='replace' requires pk to be set")
    cols = list(batch[0].keys())
    col_sql = ", ".join(f'"{c}"' for c in cols)
    val_sql = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO {fqn} ({col_sql}) VALUES ({val_sql})"
    if on_conflict == "skip":
        sql += " ON CONFLICT DO NOTHING"
    elif on_conflict == "replace":
        pk_sql = ", ".join(f'"{c}"' for c in pk_cols)  # type: ignore[arg-type]
        update_cols = [c for c in cols if c not in pk_cols]
        if update_cols:
            update_sql = ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in update_cols)
            sql += f" ON CONFLICT ({pk_sql}) DO UPDATE SET {update_sql}"
        else:
            sql += " ON CONFLICT DO NOTHING"
    params = [[_pg_val(row.get(c)) for c in cols] for row in batch]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
        return cur.rowcount if cur.rowcount >= 0 else len(batch)


def _load_rows(
    table: str,
    row_iter: Iterator[dict],
    *,
    columns: dict[str, str] | None,
    pk: str | list[str] | None,
    batch_size: int,
    truncate: bool,
    on_conflict: Literal["error", "skip", "replace"],
    dsn: str | None,
) -> int:
    fqn = _fqn(table)
    pk_cols = ([pk] if isinstance(pk, str) else list(pk)) if pk else None
    with _conn(dsn) as conn:
        # buffer first batch for schema inference + table creation
        first_batch: list[dict] = []
        for row in row_iter:
            first_batch.append(row)
            if len(first_batch) >= batch_size:
                break

        if not first_batch:
            return 0

        cols = columns or infer_columns(first_batch)
        _ensure_table(conn, fqn, cols, pk)

        if truncate:
            conn.execute(f"TRUNCATE {fqn}")

        n = _insert_batch(conn, fqn, first_batch, on_conflict=on_conflict, pk_cols=pk_cols)

        cur_batch: list[dict] = []
        for row in row_iter:
            cur_batch.append(row)
            if len(cur_batch) >= batch_size:
                n += _insert_batch(conn, fqn, cur_batch, on_conflict=on_conflict, pk_cols=pk_cols)
                cur_batch = []
        if cur_batch:
            n += _insert_batch(conn, fqn, cur_batch, on_conflict=on_conflict, pk_cols=pk_cols)

        return n


# ---- public loaders ---------------------------------------------------------

def load_from_csv(
    table: str,
    path: str | Path,
    *,
    columns: dict[str, str] | None = None,
    pk: str | list[str] | None = None,
    batch_size: int = 500,
    truncate: bool = False,
    on_conflict: Literal["error", "skip", "replace"] = "error",
    encoding: str = "utf-8",
    dsn: str | None = None,
) -> int:
    """Load a CSV file into raw.<table>. Creates table if it doesn't exist.

    All CSV columns land as `text` (CSV has no type info). If pk points to a
    numeric column, pass explicit columns={"id": "bigint", ...} to get the
    right PG type — otherwise the PK will be text, which still works as a
    source_uid but won't match bigint FKs elsewhere.

    on_conflict: "error" raises on duplicate PK; "skip" silently ignores;
                 "replace" upserts (requires pk to be set).
    Returns rows actually written (skipped rows not counted).
    """
    def _iter() -> Iterator[dict]:
        with open(path, encoding=encoding, newline="") as f:
            yield from csv.DictReader(f)

    return _load_rows(table, _iter(), columns=columns, pk=pk,
                      batch_size=batch_size, truncate=truncate,
                      on_conflict=on_conflict, dsn=dsn)


def load_from_jsonl(
    table: str,
    path: str | Path,
    *,
    columns: dict[str, str] | None = None,
    pk: str | list[str] | None = None,
    batch_size: int = 500,
    truncate: bool = False,
    on_conflict: Literal["error", "skip", "replace"] = "error",
    encoding: str = "utf-8",
    dsn: str | None = None,
) -> int:
    """Load a JSONL file into raw.<table>. Creates table if it doesn't exist.

    Column types inferred from first batch of rows unless columns= provided.
    on_conflict: "error" raises on duplicate PK; "skip" silently ignores;
                 "replace" upserts (requires pk to be set).
    Returns rows actually written (skipped rows not counted).
    """
    def _iter() -> Iterator[dict]:
        with open(path, encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    return _load_rows(table, _iter(), columns=columns, pk=pk,
                      batch_size=batch_size, truncate=truncate,
                      on_conflict=on_conflict, dsn=dsn)


def load_from_db(
    dest_table: str,
    src_table: str,
    *,
    columns: dict[str, str] | None = None,
    pk: str | list[str] | None = None,
    batch_size: int = 1000,
    truncate: bool = False,
    on_conflict: Literal["error", "skip", "replace"] = "error",
    dsn: str | None = None,
    src_dsn: str | None = None,
) -> int:
    """Copy rows from src_table into raw.<dest_table>.

    src_table: bare name defaults to raw schema. Use 'gen.extract_skills'
               or 'enc.jd2vector' to pull from other schemas.
    src_dsn:   source Postgres DSN for cross-DB loads. Defaults to dsn (same DB).
    on_conflict: "error" raises on duplicate PK; "skip" silently ignores;
                 "replace" upserts (requires pk to be set).

    Uses a server-side cursor so large tables don't land in memory.
    Returns rows actually written (skipped rows not counted).
    """
    src_fqn = _fqn(src_table)
    resolved_src_dsn = resolve_dsn(src_dsn or dsn)

    def _iter() -> Iterator[dict]:
        with psycopg.connect(resolved_src_dsn, row_factory=dict_row) as src_conn:
            with src_conn.cursor(name="trawler_raw_src") as cur:
                cur.itersize = batch_size
                cur.execute(f"SELECT * FROM {src_fqn}")
                for row in cur:
                    yield row

    return _load_rows(dest_table, _iter(), columns=columns, pk=pk,
                      batch_size=batch_size, truncate=truncate,
                      on_conflict=on_conflict, dsn=dsn)
