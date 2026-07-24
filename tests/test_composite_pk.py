"""Smoke tests for composite source_uid (multi-col PK) support.

All tests are DB-free — they exercise _row_key, set_data_source validation,
and _resolve_carry_cols without opening a Postgres connection.
"""
import json
import pytest

from trawler.errors import ConfigError
from trawler.run.base import BaseRun, _prepend


# ---------------------------------------------------------------------------
# Minimal concrete BaseRun for testing _row_key without a real DB
# ---------------------------------------------------------------------------

class _StubRun(BaseRun):
    LOG_TABLE = "gen._gen_log"
    OUT_SCHEMA = "gen"

    def __init__(self):
        # skip super().__init__ to avoid requiring ROWINFER_DSN
        self.source_uid = None

    def _out_table_name(self): return "stub"
    def _out_table_cols(self): return {}
    def pre_run_check(self): pass
    def post_run_check(self): pass
    def pre_step(self, row): return row
    def step(self, payload): return None
    def post_step(self, row, out): return {}


# ---------------------------------------------------------------------------
# _row_key — single col (backward compat)
# ---------------------------------------------------------------------------

def test_row_key_single_str():
    run = _StubRun()
    run.source_uid = "id"
    assert run._row_key({"id": 42, "title": "foo"}) == "42"


def test_row_key_single_str_attr():
    class Obj:
        id = "abc"
    run = _StubRun()
    run.source_uid = "id"
    assert run._row_key(Obj()) == "abc"


# ---------------------------------------------------------------------------
# _row_key — composite col
# ---------------------------------------------------------------------------

def test_row_key_composite_dict():
    run = _StubRun()
    run.source_uid = ["resume_id", "sort"]
    key = run._row_key({"resume_id": "123", "sort": 2, "body": "text"})
    assert key == json.dumps(["123", "2"])


def test_row_key_composite_attr():
    class Obj:
        resume_id = "r1"
        sort = 5
    run = _StubRun()
    run.source_uid = ["resume_id", "sort"]
    assert run._row_key(Obj()) == json.dumps(["r1", "5"])


def test_row_key_composite_collision_safe():
    """'["a:b","c"]' != '["a","b:c"]' — json.dumps is unambiguous."""
    run = _StubRun()
    run.source_uid = ["x", "y"]
    k1 = run._row_key({"x": "a:b", "y": "c"})
    k2 = run._row_key({"x": "a", "y": "b:c"})
    assert k1 != k2


# ---------------------------------------------------------------------------
# set_data_source — gen
# ---------------------------------------------------------------------------

def _make_gen():
    from trawler.generate.gen import MinimalGenRun
    obj = MinimalGenRun.__new__(MinimalGenRun)
    obj.data_source = None
    obj.source_uid = None
    obj.carry_cols = []
    obj._doc_cols = []
    obj._carry_cols_resolved = []
    return obj


def test_gen_set_data_source_single():
    gen = _make_gen()
    rows = [{"id": 1, "title": "foo"}, {"id": 2, "title": "bar"}]
    gen.set_data_source(iter(rows), source_uid="id")
    assert gen.source_uid == "id"


def test_gen_set_data_source_composite():
    gen = _make_gen()
    rows = [{"resume_id": "r1", "sort": 1, "body": "x"}]
    gen.set_data_source(iter(rows), source_uid=["resume_id", "sort"])
    assert gen.source_uid == ["resume_id", "sort"]


def test_gen_set_data_source_composite_missing_col():
    gen = _make_gen()
    rows = [{"resume_id": "r1", "body": "x"}]  # missing "sort"
    with pytest.raises(ConfigError, match="sort"):
        gen.set_data_source(iter(rows), source_uid=["resume_id", "sort"])


def test_gen_set_data_source_empty_raises():
    gen = _make_gen()
    with pytest.raises(ConfigError, match="empty"):
        gen.set_data_source(iter([]), source_uid=["resume_id", "sort"])


# ---------------------------------------------------------------------------
# set_data_source — encode_run
# ---------------------------------------------------------------------------

def _make_enc():
    from trawler.encode.encode_run import MinimalEncodeRun
    obj = MinimalEncodeRun.__new__(MinimalEncodeRun)
    obj.data_source = None
    obj.source_uid = None
    obj.carry_cols = []
    obj._doc_cols = []
    obj._carry_cols_resolved = []
    return obj


def test_enc_set_data_source_composite():
    enc = _make_enc()
    rows = [{"resume_id": "r1", "sort": 1, "body": "x"}]
    enc.set_data_source(iter(rows), source_uid=["resume_id", "sort"])
    assert enc.source_uid == ["resume_id", "sort"]


def test_enc_set_data_source_composite_missing_col():
    enc = _make_enc()
    rows = [{"resume_id": "r1"}]
    with pytest.raises(ConfigError, match="sort"):
        enc.set_data_source(iter(rows), source_uid=["resume_id", "sort"])


# ---------------------------------------------------------------------------
# _resolve_carry_cols — gen
# ---------------------------------------------------------------------------

def test_gen_carry_cols_explicit_excludes_uid():
    gen = _make_gen()
    rows = [{"resume_id": "r1", "sort": 1, "title": "foo"}]
    gen.set_data_source(iter(rows), source_uid=["resume_id", "sort"])
    gen.carry_cols = ["resume_id", "sort", "title"]
    cols = gen._resolve_carry_cols()
    assert cols == ["title"]


def test_gen_carry_cols_default_empty():
    gen = _make_gen()
    rows = [{"resume_id": "r1", "sort": 1, "title": "foo", "body": "bar"}]
    gen.set_data_source(iter(rows), source_uid=["resume_id", "sort"])
    # default carry_cols=[] → nothing carried
    cols = gen._resolve_carry_cols()
    assert cols == []


# ---------------------------------------------------------------------------
# _resolve_carry_cols — encode_run
# ---------------------------------------------------------------------------

def test_enc_carry_cols_excludes_composite_uid_and_doc_cols():
    enc = _make_enc()
    rows = [{"resume_id": "r1", "sort": 1, "body": "x", "meta": "y"}]
    enc.set_data_source(iter(rows), source_uid=["resume_id", "sort"])
    enc._doc_cols = ["body"]
    enc.carry_cols = ["resume_id", "sort", "body", "meta"]
    cols = enc._resolve_carry_cols()
    assert "resume_id" not in cols
    assert "sort" not in cols
    assert "body" not in cols
    assert "meta" in cols
