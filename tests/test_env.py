"""DB-free tests for trawler.env.load_env (.env auto-load).

Guards the "uniform config place" contract: Python entry points load .env so a
manual `set -a; source .env` is no longer needed, and an already-exported env
var always wins over the file. All tests fail if trawler.env is absent.
"""
from __future__ import annotations

import os

from trawler.env import load_env


def _write(tmp_path, text: str) -> str:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_loads_values_into_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAWLER_TEST_A", raising=False)
    env_file = _write(tmp_path, "TRAWLER_TEST_A=from_file\n")

    applied = load_env(env_file)

    assert applied == {"TRAWLER_TEST_A": "from_file"}
    assert os.environ["TRAWLER_TEST_A"] == "from_file"


def test_exported_env_wins_over_file(tmp_path, monkeypatch):
    # This is the load-order guarantee: a value already in the environment
    # (exported / CI) is never clobbered by .env.
    monkeypatch.setenv("TRAWLER_TEST_B", "from_env")
    env_file = _write(tmp_path, "TRAWLER_TEST_B=from_file\n")

    applied = load_env(env_file)

    assert "TRAWLER_TEST_B" not in applied      # not newly set
    assert os.environ["TRAWLER_TEST_B"] == "from_env"


def test_skips_comments_blanks_and_malformed(tmp_path, monkeypatch):
    for k in ("TRAWLER_TEST_C", "TRAWLER_TEST_D"):
        monkeypatch.delenv(k, raising=False)
    env_file = _write(
        tmp_path,
        "# a comment\n"
        "\n"
        "   \n"
        "   # indented comment\n"
        "NOEQUALSIGN\n"
        "TRAWLER_TEST_C=has=equals=in=value\n"   # only first '=' splits
        "  TRAWLER_TEST_D = spaced \n",           # key trimmed, value kept as-is
    )

    load_env(env_file)

    assert os.environ["TRAWLER_TEST_C"] == "has=equals=in=value"
    assert os.environ["TRAWLER_TEST_D"] == " spaced "
    assert "NOEQUALSIGN" not in os.environ


def test_missing_file_is_noop(tmp_path):
    applied = load_env(str(tmp_path / "does-not-exist.env"))
    assert applied == {}


def test_env_file_override_var(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAWLER_TEST_E", raising=False)
    env_file = _write(tmp_path, "TRAWLER_TEST_E=via_override\n")
    monkeypatch.setenv("TRAWLER_ENV_FILE", env_file)

    # no explicit path → resolves TRAWLER_ENV_FILE; force past the once-guard
    applied = load_env(force=True)

    assert os.environ["TRAWLER_TEST_E"] == "via_override"
    assert applied.get("TRAWLER_TEST_E") == "via_override"
