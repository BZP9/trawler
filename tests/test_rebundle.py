"""Tests for `trawler rebundle` (find a prompt's latest job, resolve its
recipe exactly like job-config, preview or --go bundle it).

All tests are DB-free where possible (local job.toml resolution) or use the
same psycopg-mocking style as tests/test_job_config.py / test_claim_rows.py.

Coverage:
1. rebundle resolves the newest job's recipe from a local job.toml.
2. rebundle exits 1 with named missing fields on an incomplete legacy recipe
   (gen._gen_log row missing prompt/pk/doc_cols, no job.toml fallback).
3. rebundle's 0-pending message includes the substring "stage_*.py".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trawler.offload.bundle import _write_bundle

PROMPT = {"name": "testprompt", "content": "You extract JSON.",
          "expected_output": "j"}
DECODER = {"name": "m1", "repo_name": "org/m1", "format": None}
MODEL_TYPE = {"name": "remote_x", "protocol": "openai",
              "base_url_env": "TEST_X_BASE_URL", "api_key_env": None}
ROWS = [
    {"id": 1, "title": "a", "body": "alpha"},
    {"id": 2, "title": "b", "body": "beta"},
]


def _make_job_dir(jobs_root: Path, job_id: str) -> Path:
    return _write_bundle(
        job_id=job_id,
        run_id="run-uuid-0001",
        trawler_version="dev",
        created_at="2026-07-13T00:00:00+00:00",
        prompt=PROMPT,
        decoder=DECODER,
        model_type=MODEL_TYPE,
        pending_rows=list(ROWS),
        source_table="raw.jobs",
        pk="id",
        doc_cols=["title", "body"],
        limit=None,
        out_dir=jobs_root / job_id,
    )


# ---------------------------------------------------------------------------
# 1. rebundle resolves the newest job's recipe from a tmp job.toml
# ---------------------------------------------------------------------------

def test_rebundle_resolves_from_local_toml(tmp_path, monkeypatch, capsys):
    import psycopg

    import trawler.cli as cli_mod
    from trawler.offload import bundle as bundle_mod

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _make_job_dir(jobs_root, "testprompt-20260713T000000Z")

    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    # DB lookup for "latest job for prompt" must not blow up the test; make
    # it unreachable so only the local job.toml is considered.
    def _fail_connect(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(psycopg, "connect", _fail_connect)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    # The default (no --go) path also runs a dry-run bundle() preview for
    # the pending/total/claimed numbers — stub it so this test stays
    # focused on recipe resolution, not the dry-run count machinery
    # (covered separately by test_job_config.py's dry-run tests).
    def _fake_bundle(**kwargs):
        return {"pending": 2, "total": 2, "claimed": 0, "config": {}}
    monkeypatch.setattr(bundle_mod, "bundle", _fake_bundle)

    args = type("A", (), {"prompt": "testprompt", "go": False, "limit": None})()
    cli_mod._cmd_rebundle(args)

    out = capsys.readouterr().out
    assert "testprompt-20260713T000000Z" in out
    assert "m1" in out
    assert "remote_x" in out
    assert "raw.jobs" in out
    assert "trawler rebundle testprompt --go" in out
    assert "trawler bundle --prompt testprompt" in out


# ---------------------------------------------------------------------------
# 2. rebundle exits 1 with named missing fields on an incomplete legacy recipe
# ---------------------------------------------------------------------------

def test_rebundle_incomplete_recipe_exits_1_naming_fields(tmp_path, monkeypatch, capsys):
    import psycopg

    import trawler.cli as cli_mod

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    # No local job.toml for this prompt — forces the gen._gen_log fallback.

    legacy_job_id = "legacyprompt-20260701T000000Z"

    class _LegacyConn:
        """Returns a job_id on the 'find latest job' query, then a legacy
        (missing prompt/pk/doc_cols) row on the job-config resolution query."""

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())

            class R:
                def fetchone(self_r):
                    if "config->>'job_id' AS job_id" in normalized:
                        return {"job_id": legacy_job_id}
                    if "FROM gen._gen_log WHERE config->>'job_id'" in normalized:
                        return {
                            "run_id": "legacy-run-1",
                            "model": "gemma4-31b",
                            "status": "complete",
                            "n_rows": 10,
                            "n_done": 10,
                            "n_failed": 0,
                            "source_table": "raw.jobon",
                            "system_prompt_content": None,
                            "stage": "imported",
                            "remote": "studio",
                            "model_type": "remote_llamacpp",
                            "pk": None,
                            "doc_cols": None,
                            "prompt": None,
                        }
                    return None
            return R()

        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _LegacyConn())
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {"prompt": "legacyprompt", "go": False, "limit": None})()
    with pytest.raises(SystemExit) as exc_info:
        cli_mod._cmd_rebundle(args)
    assert exc_info.value.code == 1

    err = capsys.readouterr().err
    assert "missing" in err
    assert "prompt" in err
    assert "pk" in err
    assert "doc-col" in err


# ---------------------------------------------------------------------------
# 3. rebundle's 0-pending message includes the substring "stage_*.py"
# ---------------------------------------------------------------------------

def test_rebundle_zero_pending_mentions_stage_script(tmp_path, monkeypatch, capsys):
    import trawler.cli as cli_mod
    from trawler.offload import bundle as bundle_mod

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _make_job_dir(jobs_root, "testprompt-20260713T000000Z")

    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    class _ZeroPendingConn:
        """No fetchone results needed — cli_mod._find_latest_job_id_for_prompt
        connect() will be monkeypatched to fail so only local job.toml is used;
        the dry-run bundle() call below is monkeypatched separately."""

    def _fail_connect(*a, **k):
        raise OSError("connection refused")

    import psycopg
    monkeypatch.setattr(psycopg, "connect", _fail_connect)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    def _fake_bundle(**kwargs):
        assert kwargs["dry_run"] is True
        return {"pending": 0, "total": 5, "claimed": 5, "config": {}}

    monkeypatch.setattr(cli_mod, "bundle", _fake_bundle, raising=False)
    # cli_mod imports bundle locally inside _cmd_rebundle via
    # `from trawler.offload.bundle import bundle` — patch at the source.
    monkeypatch.setattr(bundle_mod, "bundle", _fake_bundle)

    args = type("A", (), {"prompt": "testprompt", "go": False, "limit": None})()
    cli_mod._cmd_rebundle(args)

    out = capsys.readouterr().out
    assert "stage_*.py" in out


# ---------------------------------------------------------------------------
# 4. a stage='cleaned' job is NEVER picked as "latest", even if it is newest
#    by started_at — regression for the ghost-job re-resolution bug found
#    live 2026-07-14 (rebundle re-resolved a job cleaned in a prior session).
# ---------------------------------------------------------------------------

def test_rebundle_skips_cleaned_job_even_if_newest(tmp_path, monkeypatch, capsys):
    import psycopg

    import trawler.cli as cli_mod
    from trawler.offload import bundle as bundle_mod

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    # No local job.toml — forces the gen._gen_log path for both queries.

    good_job_id = "testprompt-20260709T060932Z"     # older, but NOT cleaned
    cleaned_job_id = "testprompt-20260713T064150Z"   # newer, but cleaned

    class _Conn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())

            class R:
                def fetchone(self_r):
                    # "find latest" query must exclude stage='cleaned' at the
                    # SQL level — assert the filter is actually present so a
                    # regression (dropped WHERE clause) fails loudly here,
                    # not just via wrong-job-picked behavior.
                    if "config->>'job_id' AS job_id" in normalized:
                        assert "stage" in normalized and "cleaned" in normalized
                        return {"job_id": good_job_id}
                    if "FROM gen._gen_log WHERE config->>'job_id'" in normalized:
                        return {
                            "run_id": "good-run-1",
                            "model": "m1",
                            "status": "complete",
                            "n_rows": 5,
                            "n_done": 5,
                            "n_failed": 0,
                            "source_table": "raw.staged",
                            "system_prompt_content": None,
                            "stage": "imported",
                            "remote": "studio",
                            "model_type": "remote_x",
                            "pk": ["id"],
                            "doc_cols": ["title", "body"],
                            "prompt": "testprompt",
                        }
                    return None
            return R()

        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Conn())
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    def _fake_bundle(**kwargs):
        assert kwargs["dry_run"] is True
        return {"pending": 0, "total": 5, "claimed": 5, "config": {}}

    monkeypatch.setattr(bundle_mod, "bundle", _fake_bundle)

    args = type("A", (), {"prompt": "testprompt", "go": False, "limit": None})()
    cli_mod._cmd_rebundle(args)

    out = capsys.readouterr().out
    assert good_job_id in out
    assert cleaned_job_id not in out
