"""Regression tests for trawler.dsn.resolve_dsn.

Verifies:
  1. TRAWLER_DSN is preferred over ROWINFER_DSN when both are set.
  2. ROWINFER_DSN alone works (back-compat fallback).
  3. Neither set → ConfigError.
  4. Explicit dsn= arg overrides env vars.

No real DB required — resolve_dsn() only reads env vars and returns a string.
"""
import pytest

from trawler.dsn import resolve_dsn
from trawler.errors import ConfigError


def test_trawler_dsn_preferred(monkeypatch):
    """TRAWLER_DSN wins when both env vars are set."""
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://trawler-host/db")
    monkeypatch.setenv("ROWINFER_DSN", "postgresql://legacy-host/db")
    assert resolve_dsn(None) == "postgresql://trawler-host/db"


def test_rowinfer_dsn_fallback(monkeypatch):
    """ROWINFER_DSN alone works (back-compat)."""
    monkeypatch.delenv("TRAWLER_DSN", raising=False)
    monkeypatch.setenv("ROWINFER_DSN", "postgresql://legacy-host/db")
    assert resolve_dsn(None) == "postgresql://legacy-host/db"


def test_neither_set_raises(monkeypatch):
    """ConfigError when neither env var is set and no explicit dsn given."""
    monkeypatch.delenv("TRAWLER_DSN", raising=False)
    monkeypatch.delenv("ROWINFER_DSN", raising=False)
    with pytest.raises(ConfigError):
        resolve_dsn(None)


def test_explicit_dsn_overrides_env(monkeypatch):
    """Explicit dsn= arg takes precedence over any env var."""
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://env-host/db")
    monkeypatch.setenv("ROWINFER_DSN", "postgresql://legacy-host/db")
    assert resolve_dsn("postgresql://explicit-host/db") == "postgresql://explicit-host/db"
