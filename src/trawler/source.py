"""Row sources for set_data_source.

Each helper returns a RowSource — an iterable of dicts that carries source
metadata (table name, upstream run_id) for run-log provenance tracking.

Subclass RowSource to add custom loaders:

    class MySource(RowSource):
        @property
        def table(self) -> str: return "custom:my_thing"
        def __iter__(self): yield from my_thing()

Examples:
    enc.set_data_source(from_db("jobs"),                        source_uid="id")
    enc.set_data_source(from_enc("bge-m3", run_id=rid),         source_uid="row_key")
    gen.set_data_source(from_gen("grade_jd"),                   source_uid="row_key")
    gen.set_data_source(from_csv("data/jobs.csv"),              source_uid="id")
    gen.set_data_source(from_enc("bge-m3", where="status='ok'"),source_uid="row_key")
"""
from __future__ import annotations
import csv
import json
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from trawler.dsn import resolve_dsn


_DEFAULT_SCHEMA = "raw"


class RowSource(ABC):
    """ABC for data sources. Subclass to add custom loaders.

    Implement `table` (log identifier) and `__iter__` (row stream).
    Override `run_id` only when sourcing from an existing run output.
    Override `count()` to enable ETA without a limit.
    """

    @property
    @abstractmethod
    def table(self) -> str:
        """Log-friendly identifier, e.g. 'enc.bge-m3', 'csv:/data/jobs.csv'."""
        ...

    @property
    def run_id(self) -> str | None:
        return None

    def count(self) -> int | None:
        return None

    @abstractmethod
    def __iter__(self) -> Iterator[dict]:
        ...


# ---- shared DB streaming ----

def _stream_db(dsn: str, fqn: str, where: str, params: list,
               batch_size: int) -> Iterator[dict]:
    where_clause = f"WHERE {where}" if where else ""
    sql = f"SELECT * FROM {fqn} {where_clause}"
    cur_name = f"trawler_src_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor(name=cur_name) as cur:
            cur.itersize = batch_size
            cur.execute(sql, params or None)
            yield from cur


# ---- concrete sources ----

class DBSource(RowSource):
    """Stream rows from any DB table.

    `where` is a raw SQL fragment — developer-owned, not sanitized.
    """

    def __init__(self, table: str, *, schema: str = _DEFAULT_SCHEMA,
                 where: str = "", dsn: str | None = None, batch_size: int = 1000):
        if "." in table:
            schema, table = table.split(".", 1)
        self._schema = schema
        self._table = table
        self._where = where
        self._dsn = dsn
        self._batch_size = batch_size

    @property
    def table(self) -> str:
        return f"{self._schema}.{self._table}"

    def count(self) -> int | None:
        dsn = resolve_dsn(self._dsn)
        fqn = f'"{self._schema}"."{self._table}"'
        where_clause = f"WHERE {self._where}" if self._where else ""
        with psycopg.connect(dsn) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {fqn} {where_clause}").fetchone()[0]

    def __iter__(self) -> Iterator[dict]:
        dsn = resolve_dsn(self._dsn)
        fqn = f'"{self._schema}"."{self._table}"'
        yield from _stream_db(dsn, fqn, self._where, [], self._batch_size)


class _RunSource(RowSource):
    """Base for enc/gen run output sources. run_id filter is parameterized."""

    _schema: str  # set by subclass

    def __init__(self, table: str, *, run_id: str | None = None,
                 where: str = "", dsn: str | None = None, batch_size: int = 1000):
        self._table = table
        self._run_id = run_id
        self._where = where
        self._dsn = dsn
        self._batch_size = batch_size

    @property
    def table(self) -> str:
        return f"{self._schema}.{self._table}"

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def _build_where(self) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        if self._run_id:
            parts.append("run_id = %s")
            params.append(self._run_id)
        if self._where:
            parts.append(f"({self._where})")
        return " AND ".join(parts), params

    def count(self) -> int | None:
        dsn = resolve_dsn(self._dsn)
        fqn = f'"{self._schema}"."{self._table}"'
        where, params = self._build_where()
        where_clause = f"WHERE {where}" if where else ""
        with psycopg.connect(dsn) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {fqn} {where_clause}", params or None
            ).fetchone()[0]

    def __iter__(self) -> Iterator[dict]:
        dsn = resolve_dsn(self._dsn)
        fqn = f'"{self._schema}"."{self._table}"'
        where, params = self._build_where()
        yield from _stream_db(dsn, fqn, where, params, self._batch_size)


class EncSource(_RunSource):
    """Stream rows from an enc output table. Optionally filter by run_id / where."""
    _schema = "enc"


class GenSource(_RunSource):
    """Stream rows from a gen output table. Optionally filter by run_id / where."""
    _schema = "gen"


class CSVSource(RowSource):
    """Stream a CSV file as dicts (csv.DictReader)."""

    def __init__(self, path: str | Path, *, encoding: str = "utf-8"):
        self._path = Path(path)
        self._encoding = encoding

    @property
    def table(self) -> str:
        return f"csv:{self._path}"

    def count(self) -> int | None:
        with open(self._path, encoding=self._encoding, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))

    def __iter__(self) -> Iterator[dict]:
        with open(self._path, encoding=self._encoding, newline="") as f:
            yield from csv.DictReader(f)


class JSONLSource(RowSource):
    """Stream a JSONL (newline-delimited JSON) file as dicts."""

    def __init__(self, path: str | Path, *, encoding: str = "utf-8"):
        self._path = Path(path)
        self._encoding = encoding

    @property
    def table(self) -> str:
        return f"jsonl:{self._path}"

    def count(self) -> int | None:
        with open(self._path, encoding=self._encoding) as f:
            return sum(1 for line in f if line.strip())

    def __iter__(self) -> Iterator[dict]:
        with open(self._path, encoding=self._encoding) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)


# ---- factory shorthands ----

def from_db(table: str, *, where: str = "", schema: str = _DEFAULT_SCHEMA,
            dsn: str | None = None, batch_size: int = 1000) -> DBSource:
    return DBSource(table, schema=schema, where=where, dsn=dsn, batch_size=batch_size)


def from_enc(table: str, *, run_id: str | None = None, where: str = "",
             dsn: str | None = None, batch_size: int = 1000) -> EncSource:
    return EncSource(table, run_id=run_id, where=where, dsn=dsn, batch_size=batch_size)


def from_gen(table: str, *, run_id: str | None = None, where: str = "",
             dsn: str | None = None, batch_size: int = 1000) -> GenSource:
    return GenSource(table, run_id=run_id, where=where, dsn=dsn, batch_size=batch_size)


def from_csv(path: str | Path, *, encoding: str = "utf-8") -> CSVSource:
    return CSVSource(path, encoding=encoding)


def from_jsonl(path: str | Path, *, encoding: str = "utf-8") -> JSONLSource:
    return JSONLSource(path, encoding=encoding)
