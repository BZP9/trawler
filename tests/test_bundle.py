"""DB-free tests for trawler bundle.

Tests use _compute_pending and _write_bundle directly — no Postgres connection.
All tests must fail if the bundle module or base._compute_row_key are absent.
"""
from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from trawler.offload.bundle import _compute_pending, _write_bundle
from trawler.run.base import _compute_row_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_prompt() -> dict:
    return {"name": "grade_jd", "content": "You grade job descriptions.", "expected_output": "j"}


def _sample_decoder() -> dict:
    return {"name": "gemma3", "repo_name": "google/gemma-3-4b-it", "format": None}


def _sample_model_type() -> dict:
    return {"name": "ollama-local", "protocol": "ollama", "base_url_env": None, "api_key_env": None}


def _make_bundle(
    tmp_path: Path,
    *,
    prompt=None,
    decoder=None,
    model_type=None,
    pending_rows=None,
    source_table="raw.jobs",
    pk="id",
    limit=None,
) -> Path:
    return _write_bundle(
        job_id="grade_jd-20260706T120000Z",
        trawler_version="0.0.1",
        created_at="2026-07-06T12:00:00+00:00",
        prompt=prompt or _sample_prompt(),
        decoder=decoder or _sample_decoder(),
        model_type=model_type or _sample_model_type(),
        pending_rows=pending_rows if pending_rows is not None else [{"id": 1, "title": "eng"}],
        source_table=source_table,
        pk=pk,
        limit=limit,
        out_dir=tmp_path / "job",
    )


# ---------------------------------------------------------------------------
# _compute_row_key encoding (must match base.py BaseRun._row_key)
# ---------------------------------------------------------------------------

def test_compute_row_key_single_str():
    assert _compute_row_key("id", {"id": 42, "title": "foo"}) == "42"


def test_compute_row_key_single_non_str_pk():
    """Integer pk values are stringified."""
    assert _compute_row_key("id", {"id": 7}) == "7"


def test_compute_row_key_composite():
    key = _compute_row_key(["resume_id", "sort"], {"resume_id": "r1", "sort": 2})
    assert key == json.dumps(["r1", "2"])


def test_compute_row_key_composite_collision_safe():
    k1 = _compute_row_key(["x", "y"], {"x": "a:b", "y": "c"})
    k2 = _compute_row_key(["x", "y"], {"x": "a", "y": "b:c"})
    assert k1 != k2


# ---------------------------------------------------------------------------
# _compute_pending — pure logic
# ---------------------------------------------------------------------------

def test_pending_excludes_ok_rows():
    source = [{"id": 1}, {"id": 2}, {"id": 3}]
    ok_keys = {"1", "3"}
    pending, n_dups = _compute_pending(source, ok_keys, "id", limit=None)
    assert [r["id"] for r in pending] == [2]
    assert n_dups == 0


def test_pending_all_rows_when_no_ok():
    source = [{"id": i} for i in range(1, 6)]
    pending, n_dups = _compute_pending(source, set(), "id", limit=None)
    assert len(pending) == 5
    assert n_dups == 0


def test_pending_all_excluded_when_all_ok():
    source = [{"id": 1}, {"id": 2}]
    ok_keys = {"1", "2"}
    pending, n_dups = _compute_pending(source, ok_keys, "id", limit=None)
    assert pending == []
    assert n_dups == 0


def test_pending_respects_limit():
    source = [{"id": i} for i in range(1, 11)]
    pending, n_dups = _compute_pending(source, set(), "id", limit=3)
    assert len(pending) == 3
    assert [r["id"] for r in pending] == [1, 2, 3]
    assert n_dups == 0


def test_pending_limit_after_exclusion():
    """Limit applies to post-exclusion count, not pre-exclusion."""
    source = [{"id": i} for i in range(1, 11)]
    # ids 1–4 already ok; remaining 5–10 → limit=3 → ids 5,6,7
    ok_keys = {str(i) for i in range(1, 5)}
    pending, n_dups = _compute_pending(source, ok_keys, "id", limit=3)
    assert [r["id"] for r in pending] == [5, 6, 7]
    assert n_dups == 0


def test_pending_composite_pk():
    source = [
        {"rid": "r1", "sort": 1, "body": "x"},
        {"rid": "r1", "sort": 2, "body": "y"},
        {"rid": "r2", "sort": 1, "body": "z"},
    ]
    ok_key = json.dumps(["r1", "1"])
    pending, n_dups = _compute_pending(source, {ok_key}, ["rid", "sort"], limit=None)
    assert len(pending) == 2
    assert pending[0] == {"rid": "r1", "sort": 2, "body": "y"}
    assert n_dups == 0


# ---------------------------------------------------------------------------
# _write_bundle — job.toml
# ---------------------------------------------------------------------------

def test_job_toml_job_section(tmp_path):
    out = _make_bundle(tmp_path)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["job"]["id"] == "grade_jd-20260706T120000Z"
    assert toml["job"]["trawler_version"] == "0.0.1"
    assert "created_at" in toml["job"]


def test_job_toml_prompt_section(tmp_path):
    out = _make_bundle(tmp_path)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["prompt"]["name"] == "grade_jd"
    assert toml["prompt"]["content"] == "You grade job descriptions."
    assert toml["prompt"]["expected_output"] == "j"


def test_job_toml_decoder_section_no_format(tmp_path):
    out = _make_bundle(tmp_path, decoder={"name": "gemma3", "repo_name": "google/gemma-3", "format": None})
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["decoder"]["name"] == "gemma3"
    assert toml["decoder"]["repo_name"] == "google/gemma-3"
    assert "format" not in toml["decoder"]


def test_job_toml_decoder_section_with_format(tmp_path):
    fmt = {"type": "json_schema", "strict": True}
    decoder = {"name": "qwen", "repo_name": "Qwen/Qwen3-8B", "format": fmt}
    out = _make_bundle(tmp_path, decoder=decoder)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["decoder"]["format"] == fmt


def test_job_toml_model_type_section(tmp_path):
    mt = {"name": "lmstudio", "protocol": "openai", "base_url_env": "LM_STUDIO_URL", "api_key_env": None}
    out = _make_bundle(tmp_path, model_type=mt)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["model_type"]["protocol"] == "openai"
    assert toml["model_type"]["base_url_env"] == "LM_STUDIO_URL"
    # api_key_env is None → should be absent from toml
    assert "api_key_env" not in toml["model_type"]


def test_job_toml_base_url_never_written(tmp_path):
    """base_url must NOT appear anywhere in job.toml."""
    out = _make_bundle(tmp_path)
    raw = (out / "job.toml").read_text()
    assert "base_url" not in raw or "base_url_env" in raw
    # More precise: the literal key 'base_url' (without _env suffix) is absent
    assert "\nbase_url " not in raw


def test_job_toml_source_section(tmp_path):
    rows = [{"id": 1}, {"id": 2}]
    out = _make_bundle(tmp_path, pending_rows=rows, source_table="raw.jobs", pk="id")
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["source"]["table"] == "raw.jobs"
    assert toml["source"]["pk"] == ["id"]
    assert toml["source"]["row_count"] == 2


def test_job_toml_composite_pk_in_source(tmp_path):
    rows = [{"rid": "r1", "sort": 1}]
    out = _make_bundle(tmp_path, pending_rows=rows, pk=["rid", "sort"])
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["source"]["pk"] == ["rid", "sort"]


def test_job_toml_run_section_with_limit(tmp_path):
    out = _make_bundle(tmp_path, limit=50)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["run"]["limit"] == 50


def test_job_toml_no_run_section_without_limit(tmp_path):
    out = _make_bundle(tmp_path, limit=None)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert "run" not in toml


def test_job_toml_prompt_content_with_newlines(tmp_path):
    """Newlines in prompt content must survive the TOML round-trip."""
    p = dict(_sample_prompt())
    p["content"] = "Line one.\nLine two.\nLine three."
    out = _make_bundle(tmp_path, prompt=p)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["prompt"]["content"] == p["content"]


def test_job_toml_prompt_content_with_quotes(tmp_path):
    """Double-quotes in prompt content must survive the TOML round-trip."""
    p = dict(_sample_prompt())
    p["content"] = 'Say "hello" and then stop.'
    out = _make_bundle(tmp_path, prompt=p)
    toml = tomllib.loads((out / "job.toml").read_text())
    assert toml["prompt"]["content"] == p["content"]


# ---------------------------------------------------------------------------
# _write_bundle — rows.jsonl
# ---------------------------------------------------------------------------

def test_rows_jsonl_count(tmp_path):
    rows = [{"id": i, "title": f"job-{i}"} for i in range(5)]
    out = _make_bundle(tmp_path, pending_rows=rows)
    lines = (out / "rows.jsonl").read_text().splitlines()
    assert len(lines) == 5


def test_rows_jsonl_round_trips_single_pk(tmp_path):
    rows = [{"id": 42, "title": "engineer"}]
    out = _make_bundle(tmp_path, pending_rows=rows, pk="id")
    loaded = [json.loads(l) for l in (out / "rows.jsonl").read_text().splitlines()]
    assert loaded[0]["id"] == 42
    # row_key from the loaded row must match what _compute_row_key would produce
    assert _compute_row_key("id", loaded[0]) == "42"


def test_rows_jsonl_round_trips_composite_pk(tmp_path):
    rows = [{"rid": "r1", "sort": 2, "body": "text"}]
    out = _make_bundle(tmp_path, pending_rows=rows, pk=["rid", "sort"])
    loaded = [json.loads(l) for l in (out / "rows.jsonl").read_text().splitlines()]
    rk = _compute_row_key(["rid", "sort"], loaded[0])
    assert rk == json.dumps(["r1", "2"])


def test_rows_jsonl_empty_when_all_ok(tmp_path):
    out = _make_bundle(tmp_path, pending_rows=[])
    lines = (out / "rows.jsonl").read_text().splitlines()
    assert lines == []


# ---------------------------------------------------------------------------
# _write_bundle — job.sqlite schema
# ---------------------------------------------------------------------------

def test_sqlite_schema_job_meta(tmp_path):
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        # Check table exists with correct columns
        cols = {row[1] for row in db.execute("PRAGMA table_info(job_meta)")}
        assert cols == {"key", "value"}


def test_sqlite_schema_results(tmp_path):
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        col_info = {row[1]: row for row in db.execute("PRAGMA table_info(results)")}
        assert "row_key" in col_info
        assert "output" in col_info
        assert "status" in col_info
        assert "error" in col_info
        assert "attempts" in col_info
        assert "updated_at" in col_info
        # row_key is PRIMARY KEY (pk = 1)
        assert col_info["row_key"][5] == 1


def test_sqlite_results_status_check_constraint(tmp_path):
    """results.status CHECK constraint rejects values other than 'ok'/'fail'."""
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO results (row_key, status) VALUES ('k1', 'pending')"
            )


def test_sqlite_results_accepts_ok_and_fail(tmp_path):
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        db.execute("INSERT INTO results (row_key, status) VALUES ('k1', 'ok')")
        db.execute("INSERT INTO results (row_key, status) VALUES ('k2', 'fail')")
        count = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    assert count == 2


def test_sqlite_job_meta_has_job_id(tmp_path):
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        row = db.execute("SELECT value FROM job_meta WHERE key='job_id'").fetchone()
    assert row is not None
    assert row[0] == "grade_jd-20260706T120000Z"


def test_sqlite_results_initially_empty(tmp_path):
    out = _make_bundle(tmp_path)
    with sqlite3.connect(str(out / "job.sqlite")) as db:
        count = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# _check_source_table validation
# ---------------------------------------------------------------------------

def test_invalid_source_table_raises():
    from trawler.offload.bundle import _check_source_table
    from trawler.errors import ConfigError
    with pytest.raises(ConfigError):
        _check_source_table("drop table; --")


def test_valid_source_tables_pass():
    from trawler.offload.bundle import _check_source_table
    _check_source_table("raw.jobs")
    _check_source_table("public.resumes")
    _check_source_table("jobs")
