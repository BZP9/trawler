"""Tests for `trawler status` (no args) as the one-stop overview combining
remote progress, queue health, and local pending jobs into one output.

Coverage:
1. Overview prints the three section headers, in order, and calls
   remote_status.sh + `offload.sh queue` (stubbed via monkeypatched
   subprocess.call — no ssh, no live remote).
2. `status <JOB_ID>` and `status --all` are untouched: they still call
   remote_status.sh directly, no section headers, no local-jobs query.
3. Local pending jobs section degrades gracefully (prints one line, does not
   raise) when Postgres is unreachable.
"""
from __future__ import annotations

import argparse

import pytest


def _args(job=None, remote="", all_=False):
    return argparse.Namespace(job=job, remote=remote, all=all_)


# ---------------------------------------------------------------------------
# 1. Overview: three section headers in order, underlying scripts invoked
# ---------------------------------------------------------------------------

def test_status_overview_prints_three_sections_in_order(monkeypatch, capsys):
    import trawler.cli as cli_mod

    calls = []

    def fake_call(cmd, *a, **k):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(cli_mod.subprocess, "call", fake_call)
    # local pending jobs: force the DB-unreachable path so this test needs no DB
    monkeypatch.setattr(
        cli_mod, "_print_local_pending_jobs",
        lambda all_jobs: print("local DB unreachable: stub"),
    )

    rc = cli_mod._cmd_status_overview(_args())

    out = capsys.readouterr().out
    assert rc == 0
    remote_idx = out.index("== remote (studio) ==")
    queue_idx = out.index("== queue ==")
    local_idx = out.index("== local pending jobs ==")
    assert remote_idx < queue_idx < local_idx, (
        f"section headers must appear in order remote -> queue -> local; got: {out!r}"
    )
    assert "local DB unreachable: stub" in out

    # remote_status.sh was invoked for the REMOTE section
    assert any("remote_status.sh" in c[0] for c in calls), calls
    # offload.sh queue was invoked for the QUEUE section (reuses existing
    # queue-printing logic instead of duplicating it in Python)
    assert any(c[0].endswith("offload.sh") and "queue" in c for c in calls), calls


# ---------------------------------------------------------------------------
# 2. `status <JOB_ID>` / `status --all` keep the old focused behavior
# ---------------------------------------------------------------------------

def test_status_with_job_id_unchanged_no_overview(monkeypatch, capsys):
    import trawler.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod.subprocess, "call", lambda cmd, *a, **k: calls.append(cmd) or 0)

    def _fail_overview(args):
        raise AssertionError("overview must NOT run when a job id is given")
    monkeypatch.setattr(cli_mod, "_cmd_status_overview", _fail_overview)

    with pytest.raises(SystemExit) as exc:
        cli_mod._cmd_status(_args(job="myjob-20260101T000000Z"))
    assert exc.value.code == 0
    assert len(calls) == 1
    assert calls[0][0].endswith("remote_status.sh")
    assert "myjob-20260101T000000Z" in calls[0]
    out = capsys.readouterr().out
    assert "== remote (studio) ==" not in out
    assert "== queue ==" not in out


def test_status_all_unchanged_no_overview(monkeypatch):
    import trawler.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod.subprocess, "call", lambda cmd, *a, **k: calls.append(cmd) or 0)

    def _fail_overview(args):
        raise AssertionError("overview must NOT run when --all is given")
    monkeypatch.setattr(cli_mod, "_cmd_status_overview", _fail_overview)

    with pytest.raises(SystemExit):
        cli_mod._cmd_status(_args(all_=True))
    assert len(calls) == 1
    assert "--all" in calls[0]


# ---------------------------------------------------------------------------
# 3. Local pending jobs degrades gracefully when Postgres is unreachable
# ---------------------------------------------------------------------------

def test_local_pending_jobs_db_unreachable_does_not_raise(monkeypatch, capsys):
    import trawler.cli as cli_mod

    monkeypatch.setenv("TRAWLER_DSN", "postgresql://x@localhost:1/x")

    # No mocking of psycopg — a real connection attempt to a dead port fails
    # fast with a connection-refused error; the function must catch it.
    cli_mod._print_local_pending_jobs(all_jobs=False)

    out = capsys.readouterr().out
    assert out.startswith("local DB unreachable:")
