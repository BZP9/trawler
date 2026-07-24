"""DB-free tests for `trawler job-config` and `trawler bundle --dry-run`.

Coverage:
1. job-config resolves from a local job.toml (happy path).
2. job-config errors clearly, naming BOTH the local path checked and the
   gen._gen_log lookup, when neither resolves.
3. bundle(dry_run=True) leaves no job dir and makes no DB writes (INSERT/claim).
4. config jsonb written by bundle() contains pk + doc_cols alongside the
   existing keys (unchanged).
"""
from __future__ import annotations

import json
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


def _make_bundle(tmp_path, out_name="job") -> Path:
    return _write_bundle(
        job_id="testprompt-20260713T000000Z",
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
        out_dir=tmp_path / out_name,
    )


# ---------------------------------------------------------------------------
# 1. job-config resolution from a local job.toml (happy path)
# ---------------------------------------------------------------------------

def test_job_config_resolves_from_local_toml(tmp_path, monkeypatch, capsys):
    import trawler.cli as cli_mod

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    job_id = "testprompt-20260713T000000Z"
    _make_bundle(jobs_root, out_name=job_id)

    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {"job": job_id})()
    cli_mod._cmd_job_config(args)

    out = capsys.readouterr().out
    assert "testprompt" in out
    assert "m1" in out
    assert "remote_x" in out
    assert "raw.jobs" in out
    assert "run-uuid-0001" in out
    # ready-to-copy re-bundle line
    assert "trawler bundle --prompt testprompt" in out
    assert "--decoder m1" in out
    assert "--model-type remote_x" in out
    assert "--source raw.jobs" in out
    assert "--pk id" in out
    assert "--doc-col title body" in out


# ---------------------------------------------------------------------------
# 2. job-config fallback error when neither local job.toml nor a gen._gen_log
#    row exists — must name BOTH places it looked.
# ---------------------------------------------------------------------------

def test_job_config_fallback_error_names_both_places(tmp_path, monkeypatch, capsys):
    import trawler.cli as cli_mod
    import psycopg

    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    job_id = "nonexistent-20260713T000000Z"

    class _EmptyConn:
        def execute(self, sql, params=None):
            class R:
                def fetchone(self):
                    return None
            return R()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _EmptyConn())
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {"job": job_id})()
    with pytest.raises(SystemExit) as exc_info:
        cli_mod._cmd_job_config(args)
    assert exc_info.value.code == 1

    err = capsys.readouterr().err
    local_path = os.path.join(str(tmp_path), "output", "jobs", job_id, "job.toml")
    assert local_path in err, f"error must name the exact local path checked; got: {err!r}"
    assert "gen._gen_log" in err, f"error must name the gen._gen_log lookup; got: {err!r}"
    assert job_id in err


# ---------------------------------------------------------------------------
# 3. bundle(dry_run=True) — zero side effects
# ---------------------------------------------------------------------------

class _DryRunConn:
    """Fake psycopg conn: fetchone returns cfg rows in order; fetchall
    distinguishes ok_keys query vs source rows query by SQL content."""

    def __init__(self, fetchone_results, source_rows):
        self._fetchones = list(fetchone_results)
        self._source_rows = source_rows
        self.executed = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        conn = self

        class R:
            def fetchone(self):
                return conn._fetchones.pop(0) if conn._fetchones else None
            def fetchall(self):
                if "FROM gen." in normalized and "row_key" in normalized:
                    return []  # no ok/pending rows yet
                return conn._source_rows
        return R()

    def rollback(self): pass
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_dry_run_no_job_dir_no_db_writes(tmp_path, monkeypatch):
    from trawler.offload import bundle as bundle_mod

    conn = _DryRunConn(
        fetchone_results=[dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)],
        source_rows=[dict(r) for r in ROWS],
    )
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    out_dir = tmp_path / "jobs"
    result = bundle_mod.bundle(
        "testprompt", "m1", "remote_x", "raw.jobs", "id",
        doc_cols=["title", "body"], out=out_dir, dry_run=True,
    )

    # No job dir created anywhere under out_dir.
    assert not out_dir.exists() or list(out_dir.iterdir()) == []

    # No INSERT / claim rows executed against gen schema.
    inserts = [(s, p) for s, p in conn.executed if s.startswith("INSERT")]
    assert inserts == [], f"dry-run must not INSERT anything; got: {inserts}"

    # Returns counts + resolved config, not a Path.
    assert result["pending"] == 2
    assert result["total"] == 2
    assert result["claimed"] == 0
    assert result["config"]["prompt"] == "testprompt"
    assert result["config"]["pk"] == ["id"]
    assert result["config"]["doc_cols"] == ["title", "body"]


# ---------------------------------------------------------------------------
# 4. config jsonb written by bundle() contains pk + doc_cols
# ---------------------------------------------------------------------------

class _RealBundleConn:
    """Fake conn used for the non-dry-run path: captures the _gen_log INSERT."""

    def __init__(self, fetchone_results, source_rows):
        self._fetchones = list(fetchone_results)
        self._source_rows = source_rows
        self.executed = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        conn = self

        class R:
            def fetchone(self):
                return conn._fetchones.pop(0) if conn._fetchones else None
            def fetchall(self):
                if "FROM gen." in normalized and "row_key" in normalized:
                    return []
                return conn._source_rows
        return R()

    def rollback(self): pass
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_config_jsonb_contains_pk_and_doc_cols(tmp_path, monkeypatch):
    from trawler.offload import bundle as bundle_mod

    conn = _RealBundleConn(
        fetchone_results=[dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)],
        source_rows=[dict(r) for r in ROWS],
    )
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    bundle_mod.bundle(
        "testprompt", "m1", "remote_x", "raw.jobs", "id",
        doc_cols=["title", "body"], out=tmp_path,
    )

    log_inserts = [(s, p) for s, p in conn.executed if "INSERT INTO gen._gen_log" in s]
    assert len(log_inserts) == 1
    _, params = log_inserts[0]
    config_json = params[-1]
    config = json.loads(config_json)

    # Existing keys unchanged.
    assert config["job_id"].startswith("testprompt-")
    assert config["offload"] is True
    assert config["prompt"] == "testprompt"
    assert config["stage"] == "exported"
    assert config["model_type"] == "remote_x"

    # New keys additive.
    assert config["pk"] == ["id"]
    assert config["doc_cols"] == ["title", "body"]


def test_rebundle_line_suppressed_on_incomplete_recipe():
    """Legacy _gen_log rows (no prompt/pk/doc_cols in config) must NOT yield a
    copy-pasteable but broken `trawler bundle --prompt None ...` line."""
    from trawler.cli import _rebundle_line

    out = _rebundle_line(None, "gemma4-31b", "remote_llamacpp", "raw.jobon", [], [])
    assert "trawler bundle" not in out
    assert "missing" in out and "prompt" in out and "pk" in out

    ok = _rebundle_line("p", "d", "mt", "raw.t", ["id"], ["doc"])
    assert ok.startswith("trawler bundle --prompt p ")
