"""run-bundle + import: DB-free tests for the offload loop (P3/P4).

Postgres is never touched: bundles are written via _write_bundle (pure),
the chat client is monkeypatched, and importer logic is tested through
its pure _prepare_rows half.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from trawler.errors import ConfigError
from trawler.offload.bundle import _write_bundle
from trawler.offload.importer import _completion_status, _prepare_rows
from trawler.offload import runner
from trawler.offload.runner import _build_doc, _load_job, run_bundle


PROMPT = {"name": "testprompt", "content": "You extract JSON.",
          "expected_output": "j"}
DECODER = {"name": "m1", "repo_name": "org/m1", "format": None}
MODEL_TYPE = {"name": "remote_x", "protocol": "openai",
              "base_url_env": "TEST_X_BASE_URL", "api_key_env": None}
ROWS = [
    {"id": 1, "title": "a", "body": "alpha"},
    {"id": 2, "title": "b", "body": "beta"},
    {"id": 3, "title": "c", "body": "gamma"},
]


def _make_bundle(tmp_path, *, doc_cols=("title", "body"), rows=None):
    return _write_bundle(
        job_id="testprompt-20260707T000000Z",
        trawler_version="dev",
        created_at="2026-07-07T00:00:00+00:00",
        prompt=PROMPT,
        decoder=DECODER,
        model_type=MODEL_TYPE,
        pending_rows=list(rows if rows is not None else ROWS),
        source_table="raw.jobs",
        pk="id",
        doc_cols=list(doc_cols) if doc_cols else None,
        limit=None,
        out_dir=tmp_path / "job",
    )


def _results(job_dir):
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.row_factory = sqlite3.Row
    try:
        return {r["row_key"]: dict(r) for r in db.execute("SELECT * FROM results")}
    finally:
        db.close()


# ---------------------------------------------------------------- job loading

def test_load_job_roundtrip(tmp_path):
    job_dir = _make_bundle(tmp_path)
    spec = _load_job(job_dir)
    assert spec.prompt_name == "testprompt"
    assert spec.expected_output == "j"
    assert spec.decoder.repo_name == "org/m1"
    assert spec.pk == "id"
    assert spec.doc_cols == ["title", "body"]


def test_load_job_without_doc_cols_fails(tmp_path):
    job_dir = _make_bundle(tmp_path, doc_cols=None)
    with pytest.raises(ConfigError, match="doc_cols"):
        _load_job(job_dir)


def test_build_doc_joins_and_flags_missing_col():
    assert _build_doc({"a": 1, "b": "x"}, ["a", "b"]) == "1\nx"
    with pytest.raises(ConfigError, match="missing"):
        _build_doc({"a": 1}, ["a", "nope"])


# ---------------------------------------------------------------- run_bundle

def test_run_bundle_happy_path(tmp_path, monkeypatch):
    job_dir = _make_bundle(tmp_path)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    calls = []

    def fake_call(model, endpoint, system, user, params):
        calls.append(user)
        return '{"ok": true}'

    monkeypatch.setattr(runner, "call", fake_call)
    summary = run_bundle(job_dir, verbose=False)
    assert summary == {"ok": 3, "fail": 0, "skipped": 0, "total": 3,
                       "stopped_early": False}
    res = _results(job_dir)
    assert res["1"]["status"] == "ok"
    assert res["1"]["doc"] == "a\nalpha"
    assert res["1"]["attempts"] == 1
    assert calls[0] == "a\nalpha"


def test_run_bundle_resumes_skipping_ok(tmp_path, monkeypatch):
    job_dir = _make_bundle(tmp_path)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    monkeypatch.setattr(runner, "call", lambda *a, **k: '{"v": 1}')
    run_bundle(job_dir, limit=2, verbose=False)          # rows 1,2 done

    seen = []

    def second_pass(model, endpoint, system, user, params):
        seen.append(user)
        return '{"v": 2}'

    monkeypatch.setattr(runner, "call", second_pass)
    summary = run_bundle(job_dir, verbose=False)
    assert summary["skipped"] == 2 and summary["ok"] == 1
    assert seen == ["c\ngamma"]                           # only row 3 called


def test_run_bundle_parse_failure_marks_fail(tmp_path, monkeypatch):
    job_dir = _make_bundle(tmp_path, rows=ROWS[:1])
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    monkeypatch.setattr(runner, "call", lambda *a, **k: "no json here at all")
    summary = run_bundle(job_dir, verbose=False)
    assert summary["fail"] == 1
    row = _results(job_dir)["1"]
    assert row["status"] == "fail"
    assert row["error_category"] == "ParseError"
    assert row["doc"] == "a\nalpha"                       # doc survives failure


def test_run_bundle_missing_env_raises(tmp_path, monkeypatch):
    job_dir = _make_bundle(tmp_path)
    monkeypatch.delenv("TEST_X_BASE_URL", raising=False)
    with pytest.raises(ConfigError, match="TEST_X_BASE_URL"):
        run_bundle(job_dir, verbose=False)


def test_run_bundle_retry_increments_attempts(tmp_path, monkeypatch):
    job_dir = _make_bundle(tmp_path, rows=ROWS[:1])
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    monkeypatch.setattr(runner, "call", lambda *a, **k: "not json")
    run_bundle(job_dir, verbose=False)                    # fail #1
    monkeypatch.setattr(runner, "call", lambda *a, **k: '{"fine": 1}')
    run_bundle(job_dir, verbose=False)                    # retry same row
    row = _results(job_dir)["1"]
    assert row["status"] == "ok"
    assert row["attempts"] == 2


# ---------------------------------------------------------------- import prep

def test_prepare_rows_parses_json_and_maps_status():
    results = [
        {"row_key": "1", "output": '{"a": 1}', "doc": "d1", "status": "ok",
         "error": None, "error_category": None},
        {"row_key": "2", "output": None, "doc": "d2", "status": "fail",
         "error": "EndpointError: 503", "error_category": "EndpointError"},
    ]
    rows = _prepare_rows(results, "j")
    assert rows[0]["status"] == "ok"
    assert rows[0]["json_output"] == {"a": 1}
    assert rows[1]["status"] == "failed"                  # fail → failed
    assert rows[1]["error_category"] == "EndpointError"


def test_prepare_rows_demotes_unparseable_ok_to_parse_error():
    results = [{"row_key": "1", "output": "garbage", "doc": "d", "status": "ok",
                "error": None, "error_category": None}]
    rows = _prepare_rows(results, "j")
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_category"] == "ParseError"
    assert rows[0]["raw_output"] == "garbage"             # raw kept for debugging


def test_prepare_rows_text_prompt_never_parses():
    results = [{"row_key": "1", "output": "plain text", "doc": "d",
                "status": "ok", "error": None, "error_category": None}]
    rows = _prepare_rows(results, "t")
    assert rows[0]["status"] == "ok"
    assert rows[0]["json_output"] is None


# --------------------------------------------------- import completion status

def test_completion_status_partial_not_running_is_partial():
    # confirmed NOT live on the remote: only some of the bundled rows were
    # attempted → the job's main task is NOT complete AND it isn't running,
    # so import must mark it 'partial' (never 'interrupted' — that status is
    # reserved for a job confirmed PARKED in queue/interrupted/).
    assert _completion_status(2019, 487947, is_running=False) == "partial"
    assert _completion_status(0, 5, is_running=False) == "partial"


def test_completion_status_partial_still_running_is_running():
    # liveness wins over coverage: a job still live on the remote must never
    # be reported 'partial' or 'interrupted' just because coverage is
    # incomplete — n_done/n_rows already carry that; status carries process
    # state. This is the case that motivated the status-model split: a
    # partial import of a STILL-RUNNING job used to lie and say 'interrupted'.
    assert _completion_status(159, 11360, is_running=True) == "running"
    assert _completion_status(0, 5, is_running=True) == "running"


def test_completion_status_all_rows_attempted_is_complete():
    # every bundled row attempted (ok or fail) → complete, even with failures,
    # and regardless of the liveness flag (a finished job can still look
    # "live" for a few seconds until the remote process exits).
    assert _completion_status(5, 5) == "complete"
    assert _completion_status(6, 5) == "complete"        # defensive >=
    assert _completion_status(5, 5, is_running=True) == "complete"


def test_completion_status_empty_total_not_complete():
    # unknown/zero total must never masquerade as complete; defaults to
    # not-running → 'partial'.
    assert _completion_status(0, 0) == "partial"


# ------------------------------------------------------- bundle sqlite schema

def test_bundle_sqlite_has_doc_and_category_cols(tmp_path):
    job_dir = _make_bundle(tmp_path)
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(results)")}
    finally:
        db.close()
    assert {"row_key", "output", "doc", "status", "error",
            "error_category", "attempts"} <= cols


def test_bundle_toml_carries_doc_cols(tmp_path):
    import tomllib
    job_dir = _make_bundle(tmp_path)
    with open(job_dir / "job.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["source"]["doc_cols"] == ["title", "body"]


# ------------------------------------------------- bundle() transaction guard

def test_bundle_rolls_back_after_missing_gen_table(tmp_path, monkeypatch):
    """Regression: probing a missing gen table aborts the pg transaction;
    without a rollback every later query raises InFailedSqlTransaction."""
    from trawler.offload import bundle as bundle_mod

    class FakeConn:
        def __init__(self):
            self.aborted = False
            self.rolled_back = False
            self.step = 0

        def execute(self, sql, params=None):
            if self.aborted:
                raise RuntimeError("current transaction is aborted")
            if "FROM gen." in sql:                    # the ok-keys probe
                self.aborted = True
                raise RuntimeError('relation "gen.testprompt" does not exist')
            self.step += 1
            cfg_rows = [dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)]
            row = cfg_rows[self.step - 1] if self.step <= 3 else None

            class R:
                def fetchone(self, _row=row):
                    return _row
                def fetchall(self):
                    return []                          # source rows
            return R()

        def rollback(self):
            self.aborted = False
            self.rolled_back = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake = FakeConn()
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: fake)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    out = bundle_mod.bundle("testprompt", "m1", "remote_x", "raw.jobs",
                            "id", doc_cols=["title"], out=tmp_path)
    assert fake.rolled_back                            # guard fired
    assert (out / "job.toml").exists()                 # bundle still completed


# ---------------------------------------------------------------- early stop

def test_run_bundle_early_stop_serial(tmp_path, monkeypatch):
    """A dead endpoint must not grind through every pending row."""
    rows = [{"id": i, "title": "t", "body": "b"} for i in range(20)]
    job_dir = _make_bundle(tmp_path, rows=rows)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    calls = {"n": 0}

    def dead_endpoint(*a, **k):
        calls["n"] += 1
        raise RuntimeError("connection refused")

    monkeypatch.setattr(runner, "call", dead_endpoint)
    summary = run_bundle(job_dir, early_stop=5, verbose=False)
    assert summary["stopped_early"] is True
    assert summary["fail"] == 5                            # stopped at the cap
    assert calls["n"] == 5                                 # not 20


def test_run_bundle_early_stop_disabled(tmp_path, monkeypatch):
    rows = [{"id": i, "title": "t", "body": "b"} for i in range(12)]
    job_dir = _make_bundle(tmp_path, rows=rows)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    monkeypatch.setattr(runner, "call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    summary = run_bundle(job_dir, early_stop=None, verbose=False)
    assert summary["stopped_early"] is False
    assert summary["fail"] == 12                           # ground through all


def test_run_bundle_ok_resets_consecutive_counter(tmp_path, monkeypatch):
    rows = [{"id": i, "title": "t", "body": "b"} for i in range(6)]
    job_dir = _make_bundle(tmp_path, rows=rows)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    state = {"n": 0}

    def flaky(*a, **k):                                    # fail, ok, fail, ok...
        state["n"] += 1
        if state["n"] % 2:
            raise RuntimeError("blip")
        return '{"v": 1}'

    monkeypatch.setattr(runner, "call", flaky)
    summary = run_bundle(job_dir, early_stop=2, verbose=False)
    assert summary["stopped_early"] is False               # never 2 in a row
    assert summary["ok"] == 3 and summary["fail"] == 3


def test_run_bundle_concurrency_processes_all(tmp_path, monkeypatch):
    rows = [{"id": i, "title": f"t{i}", "body": "b"} for i in range(10)]
    job_dir = _make_bundle(tmp_path, rows=rows)
    monkeypatch.setenv("TEST_X_BASE_URL", "http://fake:1/v1")
    monkeypatch.setattr(runner, "call", lambda *a, **k: '{"v": 1}')
    summary = run_bundle(job_dir, concurrency=4, verbose=False)
    assert summary["ok"] == 10 and summary["fail"] == 0
    assert len(_results(job_dir)) == 10


# ------------------------------------------------ job registration lifecycle

class _CapturingConn:
    """Minimal pg-conn fake: scripted fetch results + captured executes."""

    def __init__(self, fetchone_results=(), fetchall_result=()):
        self.executed = []                      # (sql, params)
        self._fetchones = list(fetchone_results)
        self._fetchall = list(fetchall_result)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        conn = self

        class R:
            def fetchone(self):
                return conn._fetchones.pop(0) if conn._fetchones else None
            def fetchall(self):
                return conn._fetchall
        return R()

    def rollback(self): pass
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_bundle_registers_exported_job(tmp_path, monkeypatch):
    from trawler.offload import bundle as bundle_mod

    conn = _CapturingConn(
        fetchone_results=[dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)],
        fetchall_result=[{"id": 1, "title": "a", "body": "b"}],
    )
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    bundle_mod.bundle("testprompt", "m1", "remote_x", "raw.jobs",
                      "id", doc_cols=["title"], out=tmp_path)
    inserts = [(s, p) for s, p in conn.executed if "INSERT INTO gen._gen_log" in s]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert params[3] == "exported"                 # status
    assert params[-2] == PROMPT["content"]         # system prompt frozen at export
    assert '"offload": true' in params[-1]         # config jsonb
    assert '"stage": "exported"' in params[-1]


def test_import_completes_exported_row(tmp_path, monkeypatch):
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path, rows=ROWS[:1])
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")
    db.commit(); db.close()

    conn = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "exported"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    res = importer_mod.import_bundle(job_dir)
    assert res["run_id"] == "reg-uuid"             # reused the registered row
    assert res["status"] == "complete"             # all bundled rows attempted
    updates = [(s, p) for s, p in conn.executed
               if "UPDATE gen._gen_log SET status=%s" in s]
    assert len(updates) == 1
    assert updates[0][1][0] == "complete"          # status param


def test_import_partial_not_running_marks_partial(tmp_path, monkeypatch):
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path)               # 3 rows bundled → row_count=3
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")   # only 1 of 3 ran
    db.commit(); db.close()

    conn = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "exported"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    res = importer_mod.import_bundle(job_dir, is_running=False)
    assert res["status"] == "partial"              # 1/3 rows, not running → main task unfinished
    assert res["total"] == 3
    updates = [(s, p) for s, p in conn.executed
               if "UPDATE gen._gen_log SET status=%s" in s]
    assert len(updates) == 1
    params = updates[0][1]
    assert params[0] == "partial"                  # status — NOT 'complete', NOT 'interrupted'
    assert params[2] == 3                          # n_rows = job TOTAL, not imported count
    assert params[3] == 1                          # n_done = rows actually imported


def test_import_partial_still_running_marks_running(tmp_path, monkeypatch):
    """Regression guard for the bug that motivated the status-model split:
    importing a partial snapshot of a job that's STILL RUNNING on the remote
    must never report 'interrupted' (or 'partial') — it lies about the job
    being stopped. Fails on pre-split code because import_bundle had no
    is_running parameter at all and always returned 'interrupted' for any
    incomplete coverage.
    """
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path)               # 3 rows bundled → row_count=3
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")   # only 1 of 3 ran
    db.commit(); db.close()

    conn = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "exported"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    res = importer_mod.import_bundle(job_dir, is_running=True)
    assert res["status"] == "running"              # still live — liveness wins over coverage
    updates = [(s, p) for s, p in conn.executed
               if "UPDATE gen._gen_log SET status=%s" in s]
    assert updates[0][1][0] == "running"


def test_import_refuses_when_already_completed(tmp_path, monkeypatch):
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path, rows=ROWS[:1])
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")
    db.commit(); db.close()

    conn = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "complete"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    with pytest.raises(ConfigError, match="already fully imported"):
        importer_mod.import_bundle(job_dir)
