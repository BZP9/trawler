"""CRUD helpers for the cfg schema.

All four cfg tables (system_prompt, decoder, encoder, model_type) follow the
same upsert-on-conflict pattern — edit a row by calling the upsert again.

DSN resolution: explicit `dsn` param > TRAWLER_DSN env var > ROWINFER_DSN (back-compat).
"""
from __future__ import annotations
import re

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from trawler.dsn import resolve_dsn


_VALID_TABLES = frozenset({"system_prompt", "decoder", "encoder", "model_type"})

# cfg names end up as quoted table names (gen.<prompt> / enc.<encoder>) and in
# f-string SQL — restrict to safe identifier chars, 63-byte Postgres limit
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid cfg name {name!r}: use only letters, digits, _ or -, "
            "max 63 chars"
        )


def _conn(dsn: str | None):
    return psycopg.connect(resolve_dsn(dsn), row_factory=dict_row)


# ---- upserts ----------------------------------------------------------------

def upsert_system_prompt(
    name: str,
    content: str,
    expected_output: str,
    description: str | None = None,
    *,
    dsn: str | None = None,
) -> None:
    """Insert or update a cfg.system_prompt row.

    expected_output: 't' (TextGenRun) or 'j' (JsonGenRun).
    """
    _check_name(name)
    if expected_output not in ("t", "j"):
        raise ValueError("expected_output must be 't' or 'j'")
    with _conn(dsn) as conn:
        conn.execute(
            "INSERT INTO cfg.system_prompt (name, content, expected_output, description) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "content=EXCLUDED.content, "
            "expected_output=EXCLUDED.expected_output, "
            "description=EXCLUDED.description, "
            "updated_at=now()",
            (name, content, expected_output, description),
        )


def upsert_decoder(
    name: str,
    repo_name: str,
    format: dict | None = None,
    description: str | None = None,
    *,
    dsn: str | None = None,
) -> None:
    """Insert or update a cfg.decoder row."""
    _check_name(name)
    with _conn(dsn) as conn:
        conn.execute(
            "INSERT INTO cfg.decoder (name, repo_name, format, description) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "repo_name=EXCLUDED.repo_name, "
            "format=EXCLUDED.format, "
            "description=EXCLUDED.description, "
            "updated_at=now()",
            (name, repo_name, Jsonb(format) if format is not None else None, description),
        )


def upsert_encoder(
    name: str,
    repo_name: str,
    dim: int,
    format: dict | None = None,
    description: str | None = None,
    *,
    dsn: str | None = None,
) -> None:
    """Insert or update a cfg.encoder row."""
    _check_name(name)
    with _conn(dsn) as conn:
        conn.execute(
            "INSERT INTO cfg.encoder (name, repo_name, dim, format, description) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "repo_name=EXCLUDED.repo_name, "
            "dim=EXCLUDED.dim, "
            "format=EXCLUDED.format, "
            "description=EXCLUDED.description, "
            "updated_at=now()",
            (name, repo_name, dim, Jsonb(format) if format is not None else None, description),
        )


def upsert_model_type(
    name: str,
    protocol: str,
    base_url_env: str | None = None,
    api_key_env: str | None = None,
    description: str | None = None,
    *,
    dsn: str | None = None,
) -> None:
    """Insert or update a cfg.model_type row.

    base_url_env: env var name holding the base URL. Nullable for local
    protocols (e.g. sentence_transformers) that need no HTTP endpoint.
    """
    _check_name(name)
    with _conn(dsn) as conn:
        conn.execute(
            "INSERT INTO cfg.model_type "
            "(name, protocol, base_url_env, api_key_env, description) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "protocol=EXCLUDED.protocol, "
            "base_url_env=EXCLUDED.base_url_env, "
            "api_key_env=EXCLUDED.api_key_env, "
            "description=EXCLUDED.description, "
            "updated_at=now()",
            (name, protocol, base_url_env, api_key_env, description),
        )


# ---- reads ------------------------------------------------------------------

def list_cfg(table: str, *, dsn: str | None = None) -> list[dict]:
    """Return all rows from a cfg table, ordered by name.

    table: 'system_prompt' | 'decoder' | 'encoder' | 'model_type'
    """
    if table not in _VALID_TABLES:
        raise ValueError(f"table must be one of {sorted(_VALID_TABLES)}")
    with _conn(dsn) as conn:
        return conn.execute(
            f"SELECT * FROM cfg.{table} ORDER BY name"
        ).fetchall()


def get_cfg(table: str, name: str, *, dsn: str | None = None) -> dict | None:
    """Fetch one row by name. Returns None if not found."""
    if table not in _VALID_TABLES:
        raise ValueError(f"table must be one of {sorted(_VALID_TABLES)}")
    with _conn(dsn) as conn:
        return conn.execute(
            f"SELECT * FROM cfg.{table} WHERE name=%s", (name,)
        ).fetchone()


# ---- delete -----------------------------------------------------------------

def delete_cfg(table: str, name: str, *, dsn: str | None = None) -> bool:
    """Delete a cfg row by name. Returns True if a row was deleted."""
    if table not in _VALID_TABLES:
        raise ValueError(f"table must be one of {sorted(_VALID_TABLES)}")
    with _conn(dsn) as conn:
        result = conn.execute(
            f"DELETE FROM cfg.{table} WHERE name=%s", (name,)
        )
        return result.rowcount > 0
