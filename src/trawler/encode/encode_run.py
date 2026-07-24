from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Iterable

from psycopg.types.json import Jsonb

from trawler.errors import ConfigError
from trawler.model.clients import embed, embed_batch
from trawler.model.types import EncoderConfig
from trawler.run.base import BaseRun, _make_doc_fn


def _vector_literal(vec: Iterable[float]) -> str:
    return "[" + ",".join(format(float(x), ".7g") for x in vec) + "]"


class MinimalEncodeRun(BaseRun):
    """Batch embedding scaffold. Setters validate early.

    Output table: enc.<encoder.name> with cols
      vec    vector(dim)   pgvector
      doc    text          the text that was embedded
      carry  jsonb         extra cols from data_source (opt-in via set_carry_cols)
      error_category text
    """

    LOG_TABLE: ClassVar[str] = "enc._enc_log"
    OUT_SCHEMA: ClassVar[str] = "enc"

    def __init__(self, dsn: str | None = None):
        super().__init__(dsn)
        self.doc_fn: Callable | None = None
        self._doc_cols: list[str] = []
        self.carry_cols: list[str] = []
        self._carry_cols_resolved: list[str] = []

    # ============================================================
    # Setters
    # ============================================================
    def set_encode_name(self, name: str) -> None:
        if not name:
            raise ValueError("encode_name required")
        self.run_name = name

    def set_model(self, name: str) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name, repo_name, dim, format, description "
                "FROM cfg.encoder WHERE name=%s",
                (name,),
            ).fetchone()
        if row is None:
            raise ConfigError(f"cfg.encoder {name!r} not found")
        self.model = EncoderConfig(**row)

    def set_model_type(self, name: str) -> None:
        self._endpoint = self._resolve_endpoint(name)
        self.model_type = name

    def set_doc_fn(self, fn: str | list[str] | Callable) -> None:
        """Set doc producer. str: single col. list[str]: cols joined by newline.
        Callable: fn(row) -> str."""
        self.doc_fn, self._doc_cols = _make_doc_fn(fn)

    def set_config(self, **params) -> None:
        self.config = dict(params)

    def set_carry_cols(self, cols: list[str]) -> None:
        self.carry_cols = list(cols)

    # ============================================================
    # Pre-run check
    # ============================================================
    def pre_run_check(self) -> None:
        required = {
            "model": self.model,
            "model_type": self.model_type,
            "data_source": self.data_source,
            "source_uid": self.source_uid,
            "doc_fn": self.doc_fn,
            "endpoint": self._endpoint,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ConfigError(f"setup incomplete; call set_* for: {missing}")
        if not self.run_name:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.run_name = f"{self.model.name}-{ts}"
        self._carry_cols_resolved = self._resolve_carry_cols()

    def post_run_check(self) -> None:
        pass

    def _resolve_carry_cols(self) -> list[str]:
        uid_set = {self.source_uid} if isinstance(self.source_uid, str) else set(self.source_uid)  # type: ignore[arg-type]
        skip = uid_set | set(self._doc_cols)
        return [c for c in self.carry_cols if c not in skip]

    # ============================================================
    # Table layout + log
    # ============================================================
    def _out_table_name(self) -> str:
        return self.model.name

    def _out_table_cols(self) -> dict[str, str]:
        return {
            "vec": f"vector({self.model.dim})",
            "doc": "text",
            "carry": "jsonb",
        }

    def _build_snapshot(self) -> dict:
        snap = super()._build_snapshot()
        snap["doc_cols"] = list(self._doc_cols)
        snap["carry_cols"] = list(self._carry_cols_resolved)
        snap["dim"] = self.model.dim
        return snap

    def _extra_log_cols(self) -> dict[str, Any]:
        return {**super()._extra_log_cols(), "dim": self.model.dim}

    # ============================================================
    # Per row
    # ============================================================
    def _row_get(self, row, key: str, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    def _fmt_payload(self, payload: Any) -> str:
        doc = str(payload.get("doc", "")) if isinstance(payload, dict) else str(payload)
        doc = doc.replace("\n", " ")
        return f'"{doc[:120]}…"' if len(doc) > 120 else f'"{doc}"'

    def _fmt_raw(self, raw: Any) -> str:
        if raw is None:
            return ""
        return f"vec[{len(raw)}]"

    def pre_step(self, row) -> dict:
        assert self.doc_fn is not None
        doc = self.doc_fn(row)
        if doc is None or (isinstance(doc, str) and not doc.strip()):
            raise ConfigError(
                f"doc_fn returned empty/None on row {self._row_key(row)!r}"
            )
        return {"doc": str(doc)}

    def step(self, payload: dict) -> list[float]:
        assert self._endpoint is not None
        return embed(self.model, self._endpoint, payload["doc"], self.config)

    def step_batch(self, payloads: list) -> list:
        assert self._endpoint is not None
        docs = [p["doc"] for p in payloads]
        return embed_batch(self.model, self._endpoint, docs, self.config)

    def _extras_floor(self, row, raw) -> dict[str, Any]:
        floor: dict[str, Any] = {}
        if self.doc_fn is not None:
            doc = self.doc_fn(row)
            if doc is not None:
                floor["doc"] = str(doc)
        floor["carry"] = Jsonb({c: self._row_get(row, c) for c in self._carry_cols_resolved})
        return floor

    def post_step(self, row, out) -> dict[str, Any]:
        vec = out
        if not vec:
            raise ConfigError("empty vec from step")
        expected = self.model.dim
        if len(vec) != expected:
            raise ConfigError(
                f"dim mismatch: got {len(vec)}, expected {expected} "
                f"(cfg.encoder.dim for {self.model.name!r})"
            )
        return {"vec": _vector_literal(vec)}
