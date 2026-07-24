"""Tests for the offload claim-rows feature (ROADMAP: offload-claim-rows).

Each test in this file MUST FAIL on pre-change code (verified via git stash).
All tests are DB-free: psycopg is monkeypatched or only pure functions are used.

Coverage per ROADMAP Tests section:
1. bundle writes N claim rows with job's run_id and status='pending'
2. second bundle for same prompt excludes rows claimed by first (pending exclusion)
3. source with duplicate row_keys → deduped pending, warning emitted, row_count = distinct
4. import upserts over placeholders (no PK violation; placeholder becomes ok/failed)
5. re-import of an interrupted job (same run_id, more results) still works
6. clean --yes releases 'pending' rows for the cleaned job; leaves ok/failed untouched
7. clean dry-run reports claim count without deleting
"""
from __future__ import annotations

import json
import os
import sqlite3
import warnings
from pathlib import Path

import pytest

from trawler.offload.bundle import _compute_pending, _write_bundle
from trawler.offload.importer import _prepare_rows

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


def _make_bundle(tmp_path, *, rows=None, pk="id") -> Path:
    return _write_bundle(
        job_id="testprompt-20260709T120000Z",
        run_id="test-run-uuid-0001",
        trawler_version="dev",
        created_at="2026-07-09T12:00:00+00:00",
        prompt=PROMPT,
        decoder=DECODER,
        model_type=MODEL_TYPE,
        pending_rows=list(rows if rows is not None else ROWS),
        source_table="raw.jobs",
        pk=pk,
        doc_cols=["title", "body"],
        limit=None,
        out_dir=tmp_path / "job",
    )


# ---------------------------------------------------------------------------
# 1. Bundle writes N claim rows with the job's run_id and status='pending'
# ---------------------------------------------------------------------------

class _CapturingConn:
    """Minimal psycopg connection fake for claim-row tests."""

    def __init__(self, fetchone_results=(), fetchall_result=()):
        self.executed = []
        self._fetchones = list(fetchone_results)
        self._fetchall = list(fetchall_result)
        self.claims: list[tuple[str, str]] = []  # (run_id, row_key) from claim INSERTs

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        # Capture claim INSERTs
        if "status) VALUES" in normalized and "'pending'" in normalized and params:
            # INSERT INTO gen."..." (run_id, row_key, status) VALUES (%s, %s, 'pending')
            self.claims.append((params[0], params[1]))
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


def test_bundle_writes_claim_rows(tmp_path, monkeypatch):
    """Bundle must INSERT N claim rows with run_id+status='pending' in the gen table."""
    from trawler.offload import bundle as bundle_mod

    conn = _CapturingConn(
        fetchone_results=[dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)],
        fetchall_result=[dict(r) for r in ROWS],
    )
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    bundle_mod.bundle("testprompt", "m1", "remote_x", "raw.jobs",
                      "id", doc_cols=["title", "body"], out=tmp_path)

    # Must have claim INSERTs for every pending row
    claim_inserts = [(s, p) for s, p in conn.executed
                     if "'pending'" in s and "INSERT INTO gen." in s]
    assert len(claim_inserts) == len(ROWS), (
        f"expected {len(ROWS)} claim INSERT(s), got {len(claim_inserts)}"
    )
    # All claims must share the same run_id
    run_ids = {p[0] for _, p in claim_inserts}
    assert len(run_ids) == 1, "all claim rows must share one run_id"
    # Status is 'pending' — embedded in the SQL not in params
    for sql, _ in claim_inserts:
        assert "'pending'" in sql


# ---------------------------------------------------------------------------
# 2. Second bundle for same prompt excludes rows claimed by first (pending exclusion)
# ---------------------------------------------------------------------------

class _SmartConn:
    """Smarter fake conn that returns different results per query."""

    def __init__(self, fetchone_results, ok_keys, source_rows):
        self._fetchones = list(fetchone_results)
        self._ok_keys = ok_keys        # returned for SELECT ... FROM gen."..."
        self._source_rows = source_rows  # returned for SELECT * FROM source
        self.executed = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        conn = self

        class R:
            def fetchone(self):
                return conn._fetchones.pop(0) if conn._fetchones else None
            def fetchall(self):
                # Distinguish the two fetchall calls by SQL content
                if 'FROM gen.' in normalized and 'row_key' in normalized:
                    return conn._ok_keys
                return conn._source_rows
        return R()

    def rollback(self): pass
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_second_bundle_excludes_pending_claims(tmp_path, monkeypatch):
    """A row_key already 'pending' in the gen table must be excluded from the bundle."""
    from trawler.offload import bundle as bundle_mod

    # Row '1' is already claimed (pending) by a first bundle
    pending_key = {"row_key": "1"}
    source = [dict(r) for r in ROWS]   # all 3 rows in source

    conn = _SmartConn(
        fetchone_results=[dict(PROMPT), dict(DECODER), dict(MODEL_TYPE)],
        ok_keys=[pending_key],     # row '1' is already ok/pending
        source_rows=source,
    )
    monkeypatch.setattr(bundle_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    bundle_mod.bundle("testprompt", "m1", "remote_x", "raw.jobs",
                      "id", doc_cols=["title", "body"], out=tmp_path)

    # The ok_keys exclusion query must include 'pending'
    ok_query = next(
        (s for s, _ in conn.executed
         if "FROM gen." in s and "status" in s and "row_key" in s),
        None,
    )
    assert ok_query is not None, "expected a SELECT row_key FROM gen.\"...\" query"
    assert "pending" in ok_query, (
        f"exclusion query must include 'pending'; got: {ok_query}"
    )

    # Only 2 claim rows should be inserted (row '1' is excluded)
    claim_inserts = [(s, p) for s, p in conn.executed
                     if "'pending'" in s and "INSERT INTO gen." in s]
    assert len(claim_inserts) == 2, (
        f"expected 2 claims (row '1' excluded), got {len(claim_inserts)}"
    )


# ---------------------------------------------------------------------------
# 3. Source with duplicate row_keys → deduped, warning emitted, row_count correct
# ---------------------------------------------------------------------------

def test_dedup_warning_and_count():
    """Duplicate row_keys in source must emit a warning and reduce row_count."""
    # Simulate a gen-table source where run_id varies but row_key repeats
    source = [
        {"id": "r1", "run_id": "uuid-a"},
        {"id": "r1", "run_id": "uuid-b"},  # duplicate row_key
        {"id": "r2", "run_id": "uuid-a"},
    ]
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        pending, n_dups = _compute_pending(source, set(), "id", limit=None)

    assert n_dups == 1, f"expected 1 dup, got {n_dups}"
    assert len(pending) == 2, f"expected 2 distinct rows, got {len(pending)}"
    assert any("duplicate" in str(x.message).lower() for x in w), (
        "expected a warning mentioning 'duplicate'"
    )
    # Warning must include the dup count
    dup_warnings = [x for x in w if "duplicate" in str(x.message).lower()]
    assert any("1" in str(x.message) for x in dup_warnings), (
        "warning must include the dup count"
    )


def test_dedup_row_count_in_job_toml(tmp_path):
    """row_count in job.toml must equal the DEDUPED count, not the raw source count."""
    import tomllib

    # Source with 3 entries but only 2 distinct row_keys
    source = [
        {"id": "r1", "title": "a"},
        {"id": "r1", "title": "dup"},  # duplicate
        {"id": "r2", "title": "b"},
    ]
    # Suppress warning (already tested above)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        pending, n_dups = _compute_pending(source, set(), "id", limit=None)

    assert n_dups == 1
    assert len(pending) == 2

    out = _write_bundle(
        job_id="test-20260709T120000Z",
        run_id="test-uuid",
        trawler_version="dev",
        created_at="2026-07-09T12:00:00+00:00",
        prompt=PROMPT,
        decoder=DECODER,
        model_type=MODEL_TYPE,
        pending_rows=pending,   # deduped
        source_table="raw.jobs",
        pk="id",
        doc_cols=["title"],
        limit=None,
        out_dir=tmp_path / "job",
    )
    with open(out / "job.toml", "rb") as f:
        toml = tomllib.load(f)
    assert toml["source"]["row_count"] == 2, (
        f"row_count must be deduped count (2), got {toml['source']['row_count']}"
    )


# ---------------------------------------------------------------------------
# 4. Import upserts over placeholders (no PK violation; placeholder → ok/failed)
# ---------------------------------------------------------------------------

def test_import_upserts_over_placeholder(tmp_path, monkeypatch):
    """Import INSERT must use ON CONFLICT (run_id, row_key) DO UPDATE to upsert
    over 'pending' placeholder rows — no PK violation."""
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path, rows=ROWS[:1])
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")
    db.commit()
    db.close()

    conn = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "exported"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    res = importer_mod.import_bundle(job_dir)

    # Must use ON CONFLICT ... DO UPDATE (not plain INSERT)
    insert_sqls = [s for s, _ in conn.executed
                   if "INSERT INTO gen." in s and "row_key" in s]
    assert len(insert_sqls) >= 1
    for sql in insert_sqls:
        assert "ON CONFLICT" in sql and "DO UPDATE" in sql, (
            f"expected ON CONFLICT DO UPDATE in insert SQL; got: {sql}"
        )
    assert res["ok"] == 1


# ---------------------------------------------------------------------------
# 5. Re-import of an interrupted job (same run_id, more results) still works
# ---------------------------------------------------------------------------

def test_reimport_interrupted_job_succeeds(tmp_path, monkeypatch):
    """Re-importing an interrupted job (status='interrupted') with more results
    must succeed and update the log row — not raise or insert a duplicate."""
    from trawler.offload import importer as importer_mod

    job_dir = _make_bundle(tmp_path, rows=ROWS)
    # First pass: only 1 of 3 rows ran → interrupted
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('1', '{\"a\":1}', 'd', 'ok', 1)")
    db.commit()
    db.close()

    conn1 = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "exported"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn1)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    res1 = importer_mod.import_bundle(job_dir)
    assert res1["status"] == "partial"     # not running, coverage incomplete

    # Second pass: all 3 rows ran → complete
    db = sqlite3.connect(str(job_dir / "job.sqlite"))
    db.execute("INSERT OR REPLACE INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('2', '{\"b\":2}', 'd', 'ok', 1)")
    db.execute("INSERT OR REPLACE INTO results(row_key, output, doc, status, attempts)"
               " VALUES ('3', '{\"c\":3}', 'd', 'ok', 1)")
    db.commit()
    db.close()

    conn2 = _CapturingConn(
        fetchone_results=[{"run_id": "reg-uuid", "status": "interrupted"}],
    )
    monkeypatch.setattr(importer_mod.psycopg, "connect", lambda *a, **k: conn2)
    res2 = importer_mod.import_bundle(job_dir)
    assert res2["status"] == "complete"
    assert res2["ok"] == 3
    # Must UPDATE (not INSERT) the existing log row
    updates = [(s, p) for s, p in conn2.executed
               if "UPDATE gen._gen_log" in s]
    assert len(updates) == 1, "re-import must UPDATE the existing log row"


# ---------------------------------------------------------------------------
# 6. clean --yes releases 'pending' rows; leaves ok/failed untouched
# ---------------------------------------------------------------------------

class _CleanConn:
    """Fake psycopg conn for clean command tests: tracks DELETEs and SELECTs."""

    def __init__(self, log_rows=(), job_row=None, pending_count=3):
        self._log_rows = list(log_rows)
        self._job_row = job_row
        self._pending_count = pending_count
        self.deleted: list[tuple] = []  # (table, run_id) from DELETE
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        conn = self

        class R:
            def fetchone(self):
                if "SELECT status" in normalized or "SELECT name" in normalized:
                    return conn._job_row
                if "COUNT(*)" in normalized:
                    return {"n": conn._pending_count}
                return None
            def fetchall(self):
                return conn._log_rows
        if "DELETE FROM gen." in normalized and "'pending'" in normalized:
            conn.deleted.append(params)
        return R()

    def rollback(self): pass
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _build_job_dir(tmp_path: Path, job_id: str, run_id: str) -> Path:
    """Write a minimal job dir with job.toml containing the given run_id."""
    import tomllib as _tl
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)
    # Write a minimal job.toml
    (job_dir / "job.toml").write_text(
        f'[job]\nid = "{job_id}"\nrun_id = "{run_id}"\n'
        f'created_at = "2026-07-09T12:00:00+00:00"\ntrawler_version = "dev"\n'
        f'\n[prompt]\nname = "testprompt"\ncontent = "x"\nexpected_output = "j"\n'
        f'\n[source]\ntable = "raw.jobs"\npk = ["id"]\nrow_count = 3\n',
        encoding="utf-8",
    )
    return job_dir


def test_clean_yes_releases_pending_claims(tmp_path, monkeypatch):
    """clean --yes must DELETE pending rows for the job's run_id."""
    import trawler.cli as cli_mod
    import psycopg
    from psycopg.rows import dict_row

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    job_dir = _build_job_dir(jobs_root, job_id, run_id)

    conn = _CleanConn(
        job_row={"status": "complete", "remote": None},
        pending_count=3,
    )

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": job_id,
        "imported": False,
        "yes": True,
        "remote": False,
        "force": False,
    })()

    cli_mod._cmd_clean(args)

    # Must have issued a DELETE targeting run_id and status='pending'
    deletes = [(s, p) for s, p in conn.executed
               if "DELETE FROM gen." in s and "'pending'" in s]
    assert len(deletes) == 1, f"expected 1 DELETE, got {len(deletes)}: {conn.executed}"
    assert deletes[0][1][0] == run_id, "DELETE must be scoped to job run_id"


def test_clean_yes_does_not_delete_ok_or_failed(tmp_path, monkeypatch):
    """clean --yes must only DELETE status='pending', never 'ok' or 'failed'."""
    import trawler.cli as cli_mod
    import psycopg

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _build_job_dir(jobs_root, job_id, run_id)

    conn = _CleanConn(
        job_row={"status": "complete", "remote": None},
        pending_count=0,
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": job_id, "imported": False, "yes": True,
        "remote": False, "force": False,
    })()
    cli_mod._cmd_clean(args)

    # DELETE SQL must be specifically scoped to 'pending'
    for sql, params in conn.executed:
        if "DELETE" in sql:
            assert "'pending'" in sql, (
                f"DELETE must only target 'pending'; got: {sql}"
            )
            # Must NOT be a blanket delete
            assert "ok" not in sql and "failed" not in sql


# ---------------------------------------------------------------------------
# 7. clean dry-run reports claim count without deleting
# ---------------------------------------------------------------------------

def test_clean_dryrun_reports_claims_without_deleting(tmp_path, monkeypatch, capsys):
    """clean without --yes must print the pending claim count but not DELETE."""
    import trawler.cli as cli_mod
    import psycopg

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _build_job_dir(jobs_root, job_id, run_id)

    conn = _CleanConn(
        job_row={"status": "complete", "remote": None},
        pending_count=3,
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": job_id, "imported": False, "yes": False,  # dry-run
        "remote": False, "force": False,
    })()
    cli_mod._cmd_clean(args)

    out = capsys.readouterr().out
    # Must report the claim count in output
    assert "3" in out and "claim" in out.lower(), (
        f"dry-run output must report pending claim count; got: {out!r}"
    )
    # Must NOT have executed any DELETE
    deletes = [(s, p) for s, p in conn.executed if "DELETE" in s]
    assert len(deletes) == 0, f"dry-run must not DELETE; got: {deletes}"


# ---------------------------------------------------------------------------
# 8. clean works when Postgres is unreachable (dirs deleted, warning, exit 0)
# ---------------------------------------------------------------------------

def test_clean_proceeds_when_db_unreachable(tmp_path, monkeypatch, capsys):
    """If Postgres is unreachable, clean must still delete dirs and warn."""
    import trawler.cli as cli_mod
    import psycopg

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    job_dir = _build_job_dir(jobs_root, job_id, run_id)
    assert job_dir.exists()

    call_count = {"n": 0}

    def _fail_connect(*a, **k):
        call_count["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _fail_connect)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": job_id, "imported": False, "yes": True,
        "remote": False, "force": True,  # --force since status unknown
    })()
    # Must not raise; must exit 0
    cli_mod._cmd_clean(args)

    out = capsys.readouterr().out
    assert "warning" in out.lower() or "unreachable" in out.lower(), (
        f"must warn about DB; got: {out!r}"
    )
    # Dir should be deleted
    assert not job_dir.exists(), "job dir must be deleted even if DB is unreachable"


# ---------------------------------------------------------------------------
# 9. job.toml records run_id (needed by claim-release lookups)
# ---------------------------------------------------------------------------

def test_write_bundle_records_run_id_in_toml(tmp_path):
    """run_id written to _write_bundle must appear in job.toml [job] section."""
    import tomllib

    out = _make_bundle(tmp_path)
    with open(out / "job.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["job"]["run_id"] == "test-run-uuid-0001"


# ---------------------------------------------------------------------------
# 10. clean stamps gen._gen_log.config->>'stage' = 'cleaned' on deletion
#     (ghost-job fix: a deleted job dir should stop haunting `trawler jobs`)
# ---------------------------------------------------------------------------

class _StampingCleanConn(_CleanConn):
    """_CleanConn plus tracking of UPDATE ... stage=cleaned statements."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.updated: list[tuple] = []  # (sql, params)

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE gen._gen_log") and "cleaned" in normalized:
            self.updated.append((normalized, params))
        return super().execute(sql, params)


def test_clean_yes_stamps_stage_cleaned(tmp_path, monkeypatch):
    """clean --yes must UPDATE the job's gen._gen_log row to stage='cleaned'
    (jsonb, back-compat with pre-2026-07-14 filters) AND status='cleaned'
    (the actual status column — psql-as-live-status-view change), matched by
    config->>'job_id'."""
    import trawler.cli as cli_mod
    import psycopg

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _build_job_dir(jobs_root, job_id, run_id)

    conn = _StampingCleanConn(
        job_row={"status": "complete", "remote": None},
        pending_count=0,
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": job_id, "imported": False, "yes": True,
        "remote": False, "force": False,
    })()
    cli_mod._cmd_clean(args)

    assert len(conn.updated) == 1, f"expected 1 stage=cleaned UPDATE, got: {conn.updated}"
    sql, params = conn.updated[0]
    assert "config = config ||" in sql
    assert "cleaned" in sql
    assert params == (job_id,)
    # status column IS now set to 'cleaned' too (2026-07-14 change) — psql
    # itself must reflect the ghost-row state, not just the jsonb config blob.
    assert "status = 'cleaned'" in sql.split("SET")[1].split("WHERE")[0]


def test_clean_imported_path_also_stamps_stage_cleaned(tmp_path, monkeypatch):
    """The --imported bulk path must stamp stage='cleaned' too, not just the
    single-job path."""
    import trawler.cli as cli_mod
    import psycopg

    job_id = "testprompt-20260709T120000Z"
    run_id = "test-run-uuid-0001"
    jobs_root = tmp_path / "output" / "jobs"
    jobs_root.mkdir(parents=True)
    _build_job_dir(jobs_root, job_id, run_id)

    conn = _StampingCleanConn(
        log_rows=[{"name": job_id, "remote": None}],
        pending_count=0,
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli_mod, "_repo_root", lambda: str(tmp_path))

    args = type("A", (), {
        "job": None, "imported": True, "yes": True,
        "remote": False, "force": False,
    })()
    cli_mod._cmd_clean(args)

    assert len(conn.updated) == 1, f"expected 1 stage=cleaned UPDATE, got: {conn.updated}"
    assert conn.updated[0][1] == (job_id,)


# ---------------------------------------------------------------------------
# 11. _print_local_pending_jobs excludes stage='cleaned' jobs by default,
#     shows them when all_jobs=True
# ---------------------------------------------------------------------------

class _JobsListConn:
    """Fake conn for _print_local_pending_jobs: captures the WHERE clause
    used and returns a fixed row set."""

    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        conn = self

        class R:
            def fetchall(self_r):
                return conn._rows
        return R()

    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_print_local_pending_jobs_excludes_cleaned_by_default(monkeypatch, capsys):
    import trawler.cli as cli_mod
    import psycopg

    conn = _JobsListConn(rows=[])
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    cli_mod._print_local_pending_jobs(all_jobs=False)

    sql, _params = conn.executed[0]
    assert "'cleaned'" in sql, (
        f"default view must filter out stage='cleaned' jobs in the SQL; got: {sql}"
    )


def test_print_local_pending_jobs_shows_cleaned_when_all_jobs(monkeypatch, capsys):
    import trawler.cli as cli_mod
    import psycopg

    conn = _JobsListConn(rows=[])
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    cli_mod._print_local_pending_jobs(all_jobs=True)

    sql, _params = conn.executed[0]
    assert "'cleaned'" not in sql, (
        f"--all view must NOT filter out stage='cleaned' jobs; got: {sql}"
    )
