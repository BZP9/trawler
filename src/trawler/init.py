"""First-time DB setup for Trawler.

Given a Postgres server URL, this script:
  1. CREATE DATABASE  (defaults to `trawler` if URL has no dbname)
  2. Run table/sql/init.sql  → schemas + log + cfg tables
  3. Seed cfg.model_type  → common transport profiles

Idempotent. Accepts URL or JDBC URL (`jdbc:` prefix stripped).

Usage:
    trawler-init                                  # uses $TRAWLER_DSN
    trawler-init --dsn postgresql://localhost:5432
    trawler-init --dsn jdbc:postgresql://localhost:5432 --dbname trawler
    python -m trawler.init
"""
from __future__ import annotations
import argparse
import sys
from importlib.resources import files
from urllib.parse import urlsplit, urlunsplit

import psycopg

from trawler.dsn import resolve_dsn
from trawler.errors import ConfigError


_DEFAULT_DBNAME = "trawler"

# (name, protocol, base_url_env, api_key_env, description)
_SEED_MODEL_TYPES: list[tuple[str, str, str | None, str | None, str]] = [
    ("local_ollama",     "ollama",                "OLLAMA_LOCAL_BASE_URL",  None,             "Ollama on localhost"),
    ("remote_ollama",    "ollama",                "OLLAMA_REMOTE_BASE_URL", None,             "Ollama on remote host"),
    ("local_lms",        "openai",                "LMS_LOCAL_BASE_URL",     None,             "LM Studio on localhost (openai-compatible, base_url must end /v1)"),
    ("remote_lms",       "openai",                "LMS_REMOTE_BASE_URL",    None,             "LM Studio on remote host (base_url must end /v1)"),
    ("openai",           "openai",                "OPENAI_BASE_URL",        "OPENAI_API_KEY", "OpenAI API"),
    ("anthropic",        "anthropic",             "ANTHROPIC_BASE_URL",     "ANTHROPIC_API_KEY", "Anthropic Messages API (base_url is bare host, no /v1)"),
    ("local_sentence_transformer", "sentence_transformers", None,             None,             "sentence-transformers loaded in-process (no HTTP)"),
]


def _normalize(dsn: str) -> str:
    if dsn.startswith("jdbc:"):
        dsn = dsn[len("jdbc:"):]
    return dsn


def _split_dsn(dsn: str, dbname: str | None) -> tuple[str, str, str]:
    """Returns (admin_dsn, target_dsn, dbname). Admin connects to template1."""
    p = urlsplit(_normalize(dsn))
    if not p.scheme:
        raise ConfigError(f"invalid DSN (no scheme): {dsn!r}")
    db = dbname or (p.path.lstrip("/") or _DEFAULT_DBNAME)
    admin = urlunsplit((p.scheme, p.netloc, "/template1", p.query, p.fragment))
    target = urlunsplit((p.scheme, p.netloc, f"/{db}", p.query, p.fragment))
    return admin, target, db


def _ensure_db(admin_dsn: str, dbname: str) -> bool:
    """CREATE DATABASE if missing. Returns True if created."""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (dbname,)
        ).fetchone()
        if exists:
            return False
        conn.execute(f'CREATE DATABASE "{dbname}"')
        return True


def init_db(dsn: str | None = None, *, dbname: str | None = None,
            seed: bool = True) -> str:
    """Set up DB end-to-end. Returns the resolved target DSN."""
    dsn = resolve_dsn(dsn)
    admin_dsn, target_dsn, db = _split_dsn(dsn, dbname)

    created = _ensure_db(admin_dsn, db)
    print(f"[trawler] db {db!r} {'created' if created else 'already exists'}")

    sql = files("trawler.table.sql").joinpath("init.sql").read_text()
    with psycopg.connect(target_dsn) as conn:
        conn.execute(sql)
        if seed:
            for row in _SEED_MODEL_TYPES:
                conn.execute(
                    "INSERT INTO cfg.model_type "
                    "(name, protocol, base_url_env, api_key_env, description) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    row,
                )

    print("[trawler] schemas + cfg tables ready.")
    if seed:
        print("[trawler] cfg.model_type seed rows (existing skipped):")
        for name, proto, env, key_env, _ in _SEED_MODEL_TYPES:
            extra = f" + {key_env}" if key_env else ""
            env_str = env or "<local>"
            print(f"  - {name:<28} protocol={proto:<22} env={env_str}{extra}")

    print()
    print("Set this in your shell rc:")
    print(f"  export TRAWLER_DSN='{target_dsn}'")
    print()
    print("Then for any model_type you'll use:")
    print("  export OLLAMA_LOCAL_BASE_URL=http://localhost:11434")
    print("  export LMS_LOCAL_BASE_URL=http://localhost:1234/v1")
    print("  ...")
    return target_dsn


def main() -> int:
    from trawler.env import load_env
    load_env()                       # .env → os.environ (exported vars win)
    p = argparse.ArgumentParser(
        prog="trawler-init",
        description="Create Trawler DB + schemas + seed cfg.model_type.",
    )
    p.add_argument("--dsn", help="Postgres URL (jdbc: prefix accepted). Default: $TRAWLER_DSN")
    p.add_argument("--dbname", help=f"Override dbname (default: from URL or {_DEFAULT_DBNAME!r})")
    p.add_argument("--no-seed", action="store_true", help="Skip cfg.model_type seed")
    args = p.parse_args()
    try:
        init_db(dsn=args.dsn, dbname=args.dbname, seed=not args.no_seed)
    except Exception as e:
        print(f"[trawler] init failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
