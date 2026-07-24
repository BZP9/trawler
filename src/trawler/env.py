"""Auto-load Trawler's ``.env`` into ``os.environ`` for Python entry points.

Shell scripts under ``scripts/`` already source ``.env`` (see
``scripts/remote_env.sh``); Python runs did not, so ``uv run trawler ...`` and
pipeline scripts required a manual ``set -a; source .env; set +a`` first. This
module closes that gap so ``.env`` is the single, uniform config place.

Semantics mirror ``remote_env.sh`` exactly: parse ``KEY=VALUE`` lines, skip
blanks and ``#`` comments, and **never clobber a variable already present in
the environment** (an exported/CI value always wins over the file). Idempotent.
"""
from __future__ import annotations
import os

_LOADED = False


def _repo_root() -> str:
    return os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_env(path: str | None = None, *, force: bool = False) -> dict[str, str]:
    """Load ``.env`` into ``os.environ`` without overriding existing vars.

    Resolution of the file:
      1. explicit ``path`` argument, else
      2. ``TRAWLER_ENV_FILE`` env var, else
      3. ``<repo-root>/.env``.

    Runs at most once per process unless ``force=True`` (or a distinct ``path``
    is passed). Returns the mapping that was *newly* set (empty if the file is
    missing or every key was already present). Pre-existing env vars win.
    """
    global _LOADED
    if _LOADED and not force and path is None:
        return {}

    env_file = path or os.environ.get("TRAWLER_ENV_FILE") \
        or os.path.join(_repo_root(), ".env")

    applied: dict[str, str] = {}
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n").rstrip("\r")
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                if os.environ.get(key):          # already set wins
                    continue
                os.environ[key] = value
                applied[key] = value

    if path is None:
        _LOADED = True
    return applied
