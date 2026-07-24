"""DB-free tests for the BaseRun lifecycle: serial / batch / concurrent modes,
limit, early-stop, resume, retry, preflight reuse, progress finalize counts.

FakeRun stubs every DB touchpoint (_write_row, _register_run, _finalize, …)
so run() executes end-to-end in memory.
"""
from __future__ import annotations
from types import SimpleNamespace

import pytest

from trawler.errors import EndpointError
from trawler.run.base import BaseRun


class FakeRun(BaseRun):
    LOG_TABLE = "gen._gen_log"
    OUT_SCHEMA = "gen"

    def __init__(self, rows, fail_ids=(), step_fn=None, pre_step_fail_ids=()):
        super().__init__(dsn="postgresql://unused")
        self.set_verbose(False)
        self.preflight = False
        self.early_stop_after = None
        self._rows = list(rows)
        self._fail_ids = set(fail_ids)
        self._step_fn = step_fn
        self._pre_step_fail_ids = set(pre_step_fail_ids)
        self.model = SimpleNamespace(name="fake", repo_name="fake")
        self.set_data_source(iter(self._rows), source_uid="id")
        # recorded calls
        self.writes: list[tuple] = []          # (row_key, status)
        self.finalized: list[tuple] = []       # (status, n_done, n_failed)
        self.bumps: list[tuple] = []
        self.step_calls: int = 0
        self.step_batch_calls: list[int] = []  # payload count per call
        self.ok_keys_for_resume: set[str] = set()

    # ---- abstract table hooks ----
    def _out_table_name(self): return "fake"
    def _out_table_cols(self): return {}

    # ---- stubbed DB layer ----
    def _open_pool(self): pass
    def _close_pool(self): pass
    def _ensure_out_table(self): pass
    def _ensure_column(self, col, sql_type="jsonb"): pass
    def _register_run(self): self.run_id = "test-run"
    def _resume_register(self): pass
    def _load_ok_keys(self): return set(self.ok_keys_for_resume)
    def _build_snapshot(self): return {}

    def _bump_progress(self, n_done, n_failed):
        self.bumps.append((n_done, n_failed))

    def _finalize(self, status, error=None, *, n_done=None, n_failed=None):
        self.finalized.append((status, n_done, n_failed))

    def _write_row(self, row_key, status, error, extras):
        self.writes.append((row_key, status))

    # ---- pipeline ----
    def pre_run_check(self): pass
    def post_run_check(self): pass

    def pre_step(self, row):
        if row["id"] in self._pre_step_fail_ids:
            raise ValueError(f"pre_step fail {row['id']}")
        return row

    def step(self, payload):
        self.step_calls += 1
        if self._step_fn is not None:
            return self._step_fn(payload)
        if payload["id"] in self._fail_ids:
            raise ValueError(f"step fail {payload['id']}")
        return f"out-{payload['id']}"

    def step_batch(self, payloads):
        self.step_batch_calls.append(len(payloads))
        return [self.step(p) for p in payloads]

    def post_step(self, row, out):
        return {"result": out}


def _rows(n):
    return [{"id": i} for i in range(1, n + 1)]


def _writes_by_status(run, status):
    return [k for k, s in run.writes if s == status]


# ---------------------------------------------------------------------------
# serial
# ---------------------------------------------------------------------------

def test_serial_all_ok():
    run = FakeRun(_rows(5))
    run.run()
    assert len(_writes_by_status(run, "ok")) == 5
    assert run.finalized == [("complete", 5, 0)]


def test_serial_failures_counted():
    run = FakeRun(_rows(5), fail_ids={2, 4})
    run.run()
    assert sorted(_writes_by_status(run, "failed")) == ["2", "4"]
    assert run.finalized == [("complete", 5, 2)]


def test_serial_limit():
    run = FakeRun(_rows(10))
    run.set_limit(3)
    run.run()
    assert len(run.writes) == 3
    assert run.finalized == [("complete", 3, 0)]


def test_early_stop():
    run = FakeRun(_rows(10), fail_ids=set(range(1, 11)))
    run.set_early_stop(3)
    run.run()
    assert len(run.writes) == 3
    status, n_done, n_failed = run.finalized[0]
    assert status == "early_stopped"
    assert (n_done, n_failed) == (3, 3)


def test_step_exception_does_not_break_run():
    run = FakeRun(_rows(3), fail_ids={1})
    run.run()
    assert run.finalized[0][0] == "complete"
    assert len(run.writes) == 3


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

def test_retry_endpoint_error_then_ok():
    attempts = {"n": 0}

    def flaky(payload):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise EndpointError("boom")
        return "ok"

    run = FakeRun(_rows(1), step_fn=flaky)
    run.set_retries(2, backoff=0)
    run.run()
    assert attempts["n"] == 3
    assert run.writes == [("1", "ok")]


def test_retry_exhausted_fails_row():
    def always_down(payload):
        raise EndpointError("down")

    run = FakeRun(_rows(1), step_fn=always_down)
    run.set_retries(2, backoff=0)
    run.run()
    assert run.step_calls == 3  # 1 + 2 retries
    assert run.writes == [("1", "failed")]


def test_no_retry_on_other_errors():
    run = FakeRun(_rows(1), fail_ids={1})
    run.set_retries(3, backoff=0)
    run.run()
    assert run.step_calls == 1
    assert run.writes == [("1", "failed")]


def test_set_retries_validates():
    run = FakeRun(_rows(1))
    with pytest.raises(ValueError):
        run.set_retries(-1)
    with pytest.raises(ValueError):
        run.set_retries(1, backoff=-0.5)


# ---------------------------------------------------------------------------
# batch mode
# ---------------------------------------------------------------------------

def test_batch_all_ok():
    run = FakeRun(_rows(7))
    run.set_batch_size(3)
    run.run()
    assert run.step_batch_calls == [3, 3, 1]
    assert len(_writes_by_status(run, "ok")) == 7
    assert run.finalized == [("complete", 7, 0)]


def test_batch_pre_step_failure_isolated():
    run = FakeRun(_rows(4), pre_step_fail_ids={2})
    run.set_batch_size(4)
    run.run()
    assert run.step_batch_calls == [3]  # row 2 excluded from the batch call
    assert sorted(_writes_by_status(run, "failed")) == ["2"]
    assert len(_writes_by_status(run, "ok")) == 3


def test_batch_call_failure_fails_whole_batch():
    def boom(payloads):
        raise ValueError("batch died")

    run = FakeRun(_rows(3))
    run.step_batch = boom
    run.set_batch_size(3)
    run.run()
    assert len(_writes_by_status(run, "failed")) == 3
    assert run.finalized == [("complete", 3, 3)]


def test_batch_retry_on_endpoint_error():
    attempts = {"n": 0}
    orig = FakeRun.step_batch

    class RetryBatchRun(FakeRun):
        def step_batch(self, payloads):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise EndpointError("transient")
            return orig(self, payloads)

    run = RetryBatchRun(_rows(3))
    run.set_batch_size(3)
    run.set_retries(1, backoff=0)
    run.run()
    assert attempts["n"] == 2
    assert len(_writes_by_status(run, "ok")) == 3


def test_batch_respects_limit():
    run = FakeRun(_rows(50))
    run.set_batch_size(32)
    run.set_limit(4)
    run.run()
    assert len(run.writes) == 4
    assert run.finalized == [("complete", 4, 0)]


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------

def test_concurrent_all_ok():
    run = FakeRun(_rows(10))
    run.set_concurrency(3)
    run.run()
    assert len(_writes_by_status(run, "ok")) == 10
    assert run.finalized == [("complete", 10, 0)]


def test_concurrent_limit_no_overshoot():
    run = FakeRun(_rows(50))
    run.set_concurrency(4)
    run.set_limit(5)
    run.run()
    assert len(run.writes) == 5
    assert run.finalized == [("complete", 5, 0)]


def test_concurrent_early_stop():
    run = FakeRun(_rows(50), fail_ids=set(range(1, 51)))
    run.set_concurrency(2)
    run.set_early_stop(4)
    run.run()
    assert run.finalized[0][0] == "early_stopped"
    # in-flight rows may finish after the stop flag; bounded by concurrency
    assert len(run.writes) <= 4 + 2


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def test_resume_skips_ok_rows_and_counts_prior():
    run = FakeRun(_rows(5))
    run.ok_keys_for_resume = {"1", "2"}
    run.set_resume("test-run")
    run.run()
    assert sorted(k for k, _ in run.writes) == ["3", "4", "5"]
    # n_done includes the 2 prior-ok rows
    assert run.finalized == [("complete", 5, 0)]


# ---------------------------------------------------------------------------
# preflight reuse
# ---------------------------------------------------------------------------

def test_preflight_result_reused_not_recalled():
    run = FakeRun(_rows(3))
    run.preflight = True
    run.run()
    # 3 rows total: preflight call for row 1 + one call each for rows 2, 3
    assert run.step_calls == 3
    assert sorted(k for k, _ in run.writes) == ["1", "2", "3"]
    assert run.finalized == [("complete", 3, 0)]


def test_preflight_failure_aborts_before_register():
    run = FakeRun(_rows(3), step_fn=lambda p: (_ for _ in ()).throw(EndpointError("down")))
    run.preflight = True
    with pytest.raises(EndpointError, match="down"):   # category preserved
        run.run()
    assert run.writes == []
    assert run.finalized == []


# ---------------------------------------------------------------------------
# eta
# ---------------------------------------------------------------------------

class _LogCapturingRun(FakeRun):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.logged: list[str] = []

    def _log(self, msg: str = "") -> None:
        self.logged.append(msg)


def test_eta_uses_overall_wall_rate():
    run = _LogCapturingRun(_rows(20))
    run.set_limit(20)  # generator source has no len; limit supplies n_total
    run.run()
    etas = [m for m in run.logged if m.startswith("[eta]")]
    assert len(etas) == 2  # printed at n=10 and n=20
    # progress + percent from n_total; eta derived from wall-clock rate
    assert "done=10/20 (50%)" in etas[0]
    assert "wall/row=" in etas[0] and "elapsed=" in etas[0] and "eta=~" in etas[0]
    assert "done=20/20 (100%)" in etas[1]
    assert "eta=~0s" in etas[1]  # nothing remaining at the end


def test_eta_resume_with_limit_no_overshoot():
    """Resume + limit + source bigger than limit: n_total must be the rows this
    pass will actually process (limit), not limit - prior_ok. Regression for
    'done=200/99 (202%) eta=~-13280s'."""
    run = _LogCapturingRun(_rows(40))
    run.ok_keys_for_resume = {"1", "2", "3", "4"}          # 4 prior-ok rows
    run._row_source = SimpleNamespace(count=lambda: 40)    # type: ignore[assignment]
    run.set_limit(10)
    run.set_resume("test-run")
    run.run()
    etas = [m for m in run.logged if m.startswith("[eta]")]
    # done= is cumulative: 4 prior ok + 10 this pass
    assert "done=14/14 (100%)" in etas[-1]
    assert "eta=~0s" in etas[-1]
    assert not any("-" in m.split("eta=")[1] for m in etas)  # never negative


def test_resume_limit_total_tops_up():
    """total=True: limit counts prior-ok rows — resume processes only the
    remainder, so the table ends with exactly `limit` ok rows."""
    run = FakeRun(_rows(40))
    run.ok_keys_for_resume = {"1", "2", "3", "4"}
    run.set_limit(10, total=True)
    run.set_resume("test-run")
    run.run()
    assert len(run.writes) == 6                      # 10 total - 4 prior ok
    assert run.finalized == [("complete", 10, 0)]    # n_done = 4 + 6


def test_resume_limit_total_already_satisfied():
    run = FakeRun(_rows(40))
    run.ok_keys_for_resume = {str(i) for i in range(1, 11)}  # 10 already ok
    run.set_limit(10, total=True)
    run.set_resume("test-run")
    run.run()
    assert run.writes == []                          # budget already spent
    assert run.finalized == [("complete", 10, 0)]


def test_resume_limit_default_is_per_pass():
    run = FakeRun(_rows(40))
    run.ok_keys_for_resume = {"1", "2", "3", "4"}
    run.set_limit(10)                                # default: 10 more this pass
    run.set_resume("test-run")
    run.run()
    assert len(run.writes) == 10
    assert run.finalized == [("complete", 14, 0)]    # 4 prior + 10 new


def test_row_counter_cumulative_on_resume():
    """Per-row counter is cumulative: prior-ok rows count toward numerator and
    denominator, so a resume reads '#5/14', not '#1/10' or '#5/10'."""
    run = _LogCapturingRun(_rows(40))
    run.ok_keys_for_resume = {"1", "2", "3", "4"}
    run._row_source = SimpleNamespace(count=lambda: 40)   # type: ignore[assignment]
    run.set_limit(10)
    run.set_resume("test-run")
    run.run()
    ok_lines = [m for m in run.logged if "→ ok" in m]
    assert "[#5/14 " in ok_lines[0]     # first fresh row: 4 prior ok + 1
    assert "[#14/14 " in ok_lines[-1]   # ends at the target, not past it


def test_row_counter_total_mode_matches_budget():
    """total=True: denominator equals the ok-row budget, so the counter ends
    exactly at limit ('#10/10' for limit 10 with 4 prior ok)."""
    run = _LogCapturingRun(_rows(40))
    run.ok_keys_for_resume = {"1", "2", "3", "4"}
    run._row_source = SimpleNamespace(count=lambda: 40)   # type: ignore[assignment]
    run.set_limit(10, total=True)
    run.set_resume("test-run")
    run.run()
    ok_lines = [m for m in run.logged if "→ ok" in m]
    assert "[#5/10 " in ok_lines[0]
    assert "[#10/10 " in ok_lines[-1]


def test_pass_total_source_smaller_than_limit():
    run = FakeRun(_rows(5))
    run._row_source = SimpleNamespace(count=lambda: 5)  # type: ignore[assignment]
    run.set_limit(100)
    assert run._pass_total() == 5   # min(limit, available), not limit


def test_eta_unknown_total():
    run = _LogCapturingRun((r for r in _rows(10)))  # generator: no len/count
    run.limit = None
    run.run()
    etas = [m for m in run.logged if m.startswith("[eta]")]
    assert len(etas) == 1
    assert "done=10/?" in etas[0] and "eta=?" in etas[0]


# ---------------------------------------------------------------------------
# pool fallback
# ---------------------------------------------------------------------------

def test_open_pool_missing_psycopg_pool_falls_back(monkeypatch):
    """Stale envs without psycopg_pool must not crash — per-call connections."""
    import sys
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)  # forces ImportError
    run = FakeRun(_rows(1))
    BaseRun._open_pool(run)  # bypass FakeRun's stub, exercise the real method
    assert run._pool is None


def test_preflight_preserves_error_category():
    """Regression: _pre_flight used to wrap RowInferError in RuntimeError,
    erasing the category callers dispatch on."""
    import pytest
    from trawler.errors import BudgetError

    def boom(payload):
        raise BudgetError("truncated")

    run = FakeRun([{"id": 1}], step_fn=boom)
    run.preflight = True
    with pytest.raises(BudgetError):
        run.run()
