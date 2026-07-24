"""psql-as-live-status-view regression tests (P2/P3/P4 of
output/psql-status-sync/STATE.md): `clean` stamps status='cleaned' on the
_gen_log row, `enqueue`/`push` refuse a RUNNING job without --force, and
`status`/`jobs`/`queue`/`pull` write fresh n_done/n_failed/stage back to the
row after every successful remote poll. All DB-free: psycopg.connect is
monkeypatched with a capturing fake, no Postgres touched.
"""
from __future__ import annotations

import argparse
import os


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


# --------------------------------------------------------------- item 2: clean

def test_clean_stamps_status_cleaned(tmp_path, monkeypatch):
    """`clean <job-id> --yes` must set status='cleaned' on the _gen_log row,
    not just config->>'stage'. Fails on pre-change code because the UPDATE
    only touched the jsonb config column, never the status column itself —
    so a raw `SELECT status FROM gen._gen_log` still showed 'complete' for a
    dir that no longer exists on disk.
    """
    import psycopg
    from trawler import cli

    job_id = "testprompt-20260707T000000Z"
    job_dir = tmp_path / "output" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.toml").write_text("[job]\nrun_id = \"r1\"\n")

    monkeypatch.setattr(cli, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")

    conn = _CapturingConn(
        fetchone_results=[{"status": "complete", "remote": "studio"}],
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)

    args = argparse.Namespace(job=job_id, imported=False, yes=True,
                               remote=False, force=False)
    cli._cmd_clean(args)

    updates = [(s, p) for s, p in conn.executed
               if "UPDATE gen._gen_log SET status = 'cleaned'" in s]
    assert len(updates) == 1, f"no status='cleaned' UPDATE found in {conn.executed}"
    assert updates[0][1] == (job_id,)
    assert not job_dir.exists()          # dir still actually deleted


def test_local_pending_jobs_filter_excludes_status_cleaned(monkeypatch):
    """`trawler jobs` (default view) must exclude status='cleaned' rows via
    the SQL WHERE clause, not just the legacy stage jsonb check. Regression
    guard: asserts the query string itself carries the new predicate so a
    future edit can't silently drop it.
    """
    import psycopg
    from trawler import cli

    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    conn = _CapturingConn(fetchall_result=[])
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)

    cli._print_local_pending_jobs(all_jobs=False)

    selects = [s for s, _ in conn.executed if "FROM gen._gen_log" in s]
    assert len(selects) == 1
    assert "status != 'cleaned'" in selects[0]


def test_local_pending_jobs_filter_includes_new_pending_statuses(monkeypatch):
    """`trawler jobs` must treat 'running' and 'partial' (the new statuses
    from the status-model split) as pending, alongside 'exported'/
    'interrupted' — otherwise a job actively running on the remote would
    silently vanish from the pending view the moment item 4's sync marks it
    'running'.
    """
    import psycopg
    from trawler import cli

    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    conn = _CapturingConn(fetchall_result=[])
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)

    cli._print_local_pending_jobs(all_jobs=False)

    selects = [s for s, _ in conn.executed if "FROM gen._gen_log" in s]
    assert len(selects) == 1
    assert "status IN ('exported', 'running', 'partial', 'interrupted')" in selects[0]


# ----------------------------------------------------- item 3: enqueue/push guard

def test_enqueue_refuses_running_job_without_force(monkeypatch, capsys):
    """`trawler enqueue <job-id>` against a job that's RUNNING on the remote
    (queue/active task or live run-bundle process) must refuse and never
    reach scripts/offload.sh — this is the exact accident from STATE.md
    (duplicate queue entry, job both active and waiting). Fails on
    pre-change code because _cmd_offload_passthrough had no guard at all —
    it always shelled straight to offload.sh.
    """
    from trawler import cli

    monkeypatch.setattr(cli, "_remote_job_running", lambda job_id, remote: True)
    called = []
    monkeypatch.setattr(cli, "_offload", lambda verb, argv: called.append((verb, argv)) or 0)

    args = argparse.Namespace(verb="enqueue", remote="", args=["dims2jd-x"])
    try:
        cli._cmd_offload_passthrough(args)
    except SystemExit as e:
        assert e.code == 1
    else:
        raise AssertionError("expected SystemExit(1)")

    assert called == [], "offload.sh must never be invoked when the guard refuses"
    err = capsys.readouterr().err
    assert "RUNNING" in err and "--force" in err


def test_enqueue_force_bypasses_guard(monkeypatch):
    """--force must skip the running-check and still reach offload.sh, with
    --force itself stripped from the forwarded args (offload.sh has no such
    flag).
    """
    from trawler import cli

    monkeypatch.setattr(cli, "_remote_job_running",
                         lambda *a, **k: (_ for _ in ()).throw(
                             AssertionError("guard should be skipped with --force")))
    called = []
    monkeypatch.setattr(cli, "_offload", lambda verb, argv: called.append((verb, argv)) or 0)

    args = argparse.Namespace(verb="enqueue", remote="", args=["dims2jd-x", "--force"])
    try:
        cli._cmd_offload_passthrough(args)
    except SystemExit as e:
        assert e.code == 0
    assert called == [("enqueue", ["dims2jd-x"])]


def test_enqueue_proceeds_when_not_running(monkeypatch):
    """A job that's NOT running on the remote must enqueue normally (no
    behavior change for the common case).
    """
    from trawler import cli

    monkeypatch.setattr(cli, "_remote_job_running", lambda job_id, remote: False)
    called = []
    monkeypatch.setattr(cli, "_offload", lambda verb, argv: called.append((verb, argv)) or 0)

    args = argparse.Namespace(verb="enqueue", remote="", args=["some-other-job"])
    try:
        cli._cmd_offload_passthrough(args)
    except SystemExit as e:
        assert e.code == 0
    assert called == [("enqueue", ["some-other-job"])]


def test_push_also_guarded(monkeypatch, capsys):
    """`push` is guarded the same as `enqueue` — pushing job dir contents
    (excluding job.sqlite) over a running job is harmless today, but a
    future push of job.sqlite itself onto a running job would be the same
    class of accident as the duplicate-queue incident.
    """
    from trawler import cli

    monkeypatch.setattr(cli, "_remote_job_running", lambda job_id, remote: True)
    called = []
    monkeypatch.setattr(cli, "_offload", lambda verb, argv: called.append((verb, argv)) or 0)

    args = argparse.Namespace(verb="push", remote="", args=["dims2jd-x"])
    try:
        cli._cmd_offload_passthrough(args)
    except SystemExit as e:
        assert e.code == 1
    assert called == []


def test_queue_verb_not_guarded(monkeypatch):
    """Read-only verbs (queue, status, pull, ...) must never be blocked by
    the running-check — only enqueue/push mutate remote job state.
    """
    from trawler import cli

    monkeypatch.setattr(cli, "_remote_job_running",
                         lambda *a, **k: (_ for _ in ()).throw(
                             AssertionError("queue must never call the running-check")))
    called = []
    monkeypatch.setattr(cli, "_offload", lambda verb, argv: called.append((verb, argv)) or 0)

    args = argparse.Namespace(verb="queue", remote="", args=[])
    try:
        cli._cmd_offload_passthrough(args)
    except SystemExit as e:
        assert e.code == 0
    assert called == [("queue", [])]


def test_bundle_default_out_is_repo_root_anchored(tmp_path, monkeypatch):
    """Regression: bundle's default --out was cwd-relative "output/jobs", so
    running via the shell alias from another repo wrote the job dir where
    push/enqueue/import (which resolve _repo_root()) could never find it —
    the job was enqueued anyway and parked as stuck on the remote."""
    from trawler import cli
    from trawler.offload import bundle as bundle_mod

    captured = {}

    def fake_bundle(**kw):
        captured.update(kw)
        return tmp_path / "output" / "jobs" / "x"

    monkeypatch.setattr(cli, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(bundle_mod, "bundle", fake_bundle)
    monkeypatch.chdir(tmp_path / "..")  # anywhere that is NOT the repo root

    args = argparse.Namespace(prompt="p", decoder="d", model_type="mt",
                              source="raw.t", pk=["id"], doc_col=["body"],
                              limit=None, out=None, dry_run=False)
    cli._cmd_bundle(args)

    assert captured["out"] == os.path.join(str(tmp_path), "output", "jobs")


def test_status_overview_syncs_every_non_terminal_job_not_just_newest(monkeypatch):
    """Regression: bare `trawler status` only synced the single newest job dir
    on the remote (remote_status.sh with no job-id argument picks one via
    `ls -td | head -1`), leaving other non-terminal jobs (e.g. one interrupted
    earlier and buried behind a newer active job) stuck showing stale status
    in gen._gen_log until someone happened to run `status <that-job-id>`
    directly. `_sync_all_remote_jobs` must poll every non-terminal job."""
    from trawler import cli

    conn = _CapturingConn(fetchall_result=[
        {"name": "dims2jd-20260714T020028Z", "remote": "studio"},
        {"name": "jd2jd-20260717T073605Z", "remote": "studio"},
    ])

    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("TRAWLER_DSN", "postgresql://fake")
    monkeypatch.setattr(cli, "_repo_root", lambda: "/repo")

    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                         lambda cmd, **k: calls.append(cmd))

    cli._sync_all_remote_jobs(None)

    assert len(calls) == 2, calls
    job_ids = {c[-1] for c in calls}
    assert job_ids == {"dims2jd-20260714T020028Z", "jd2jd-20260717T073605Z"}
    for c in calls:
        assert c[0] == "/repo/scripts/remote_status.sh"
        assert "-r" in c and "studio" in c
