"""DSN resolution helper.

Priority order: explicit dsn arg > TRAWLER_DSN env var > ROWINFER_DSN (back-compat) > ConfigError.
"""
from __future__ import annotations
import os

from trawler.errors import ConfigError


def resolve_dsn(dsn: str | None) -> str:
    """Return a resolved Postgres DSN string.

    Resolution order:
      1. Explicit ``dsn`` argument (non-empty string).
      2. ``TRAWLER_DSN`` environment variable.
      3. ``ROWINFER_DSN`` environment variable (back-compat fallback).
      4. Raises :class:`~trawler.errors.ConfigError`.
    """
    if dsn:
        return dsn
    env = os.environ.get("TRAWLER_DSN") or os.environ.get("ROWINFER_DSN")
    if env:
        return env
    raise ConfigError(
        "No DSN provided. Set TRAWLER_DSN (or legacy ROWINFER_DSN) env var, "
        "or pass dsn= explicitly."
    )
