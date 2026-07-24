"""Output table querying and schema discovery helpers.

Read from gen/enc output tables and inspect what tables exist without writing
raw SQL. Companion to inspect.py (which covers log tables).

DSN resolution: explicit `dsn` param > TRAWLER_DSN env var > ROWINFER_DSN (back-compat).
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from trawler.dsn import resolve_dsn


def _conn(dsn: str | None):
    return psycopg.connect(resolve_dsn(dsn), row_factory=dict_row)


def _normalize_table(out_table: str, default_schema: str = "gen") -> str:
    """'extract_skills' → 'gen."extract_skills"', 'enc.foo' → 'enc."foo"'."""
    if "." not in out_table:
        return f'{default_schema}."{out_table}"'
    schema, name = out_table.split(".", 1)
    name = name.strip('"')
    return f'{schema}."{name}"'


# ---- output table reads -----------------------------------------------------

def get_output(
    out_table: str,
    *,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    dsn: str | None = None,
) -> list[dict]:
    """Read rows from a gen/enc output table.

    out_table: 'gen.extract_skills', 'enc.jd2vector', or bare 'extract_skills'
               (defaults to gen schema).
    Filter by run_id and/or status ('ok' | 'failed'). Most recent first.
    """
    table = _normalize_table(out_table)
    wheres: list[str] = []
    params: list = []
    if run_id is not None:
        wheres.append("run_id=%s")
        params.append(run_id)
    if status is not None:
        wheres.append("status=%s")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    params.append(limit)
    with _conn(dsn) as conn:
        return conn.execute(
            f"SELECT * FROM {table} {where_sql} ORDER BY created_at DESC LIMIT %s",
            params,
        ).fetchall()


# ---- schema discovery -------------------------------------------------------

def list_tables(schema: str, *, dsn: str | None = None) -> list[str]:
    """Return table names in a schema, sorted alphabetically.

    schema: 'gen' | 'enc' | 'cfg' | 'raw'
    """
    with _conn(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' "
            "ORDER BY table_name",
            (schema,),
        ).fetchall()
    return [r["table_name"] for r in rows]


def table_row_counts(schema: str, *, dsn: str | None = None) -> dict[str, int]:
    """Return {table_name: row_count} for every table in a schema.

    Useful for a quick size check before querying. Runs one COUNT(*) per table.
    """
    tables = list_tables(schema, dsn=dsn)
    if not tables:
        return {}
    counts: dict[str, int] = {}
    with _conn(dsn) as conn:
        for t in tables:
            row = conn.execute(
                f'SELECT COUNT(*) AS n FROM {schema}."{t}"'
            ).fetchone()
            counts[t] = row["n"]  # type: ignore[index]
    return counts
