from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

from psycopg.types.json import Jsonb

from trawler.errors import ConfigError
from trawler.model.clients import call
from trawler.model.types import DecoderConfig
from trawler.run.base import BaseRun, _make_doc_fn


class MinimalGenRun(BaseRun):
    """Generate run scaffold. Set up via set_* funcs (each validates early)."""

    LOG_TABLE: ClassVar[str] = "gen._gen_log"
    OUT_SCHEMA: ClassVar[str] = "gen"
    EXPECTED_OUTPUT: ClassVar[str | None] = None   # subclass sets "t" / "j"

    def __init__(self, dsn: str | None = None):
        super().__init__(dsn)
        self.system_prompt_name: str | None = None
        self._prompt_row: dict | None = None
        self.doc_fn: Callable | None = None
        self._doc_cols: list[str] = []
        self.carry_cols: list[str] = []
        self._carry_cols_resolved: list[str] = []

    # ============================================================
    # Setters
    # ============================================================
    def set_gen_name(self, gen_name: str) -> None:
        if not gen_name:
            raise ValueError("gen_name required")
        self.run_name = gen_name

    def set_model(self, name: str) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name, repo_name, format, description "
                "FROM cfg.decoder WHERE name=%s",
                (name,),
            ).fetchone()
        if row is None:
            raise ConfigError(f"cfg.decoder {name!r} not found")
        self.model = DecoderConfig(**row)

    def set_model_type(self, name: str) -> None:
        self._endpoint = self._resolve_endpoint(name)
        self.model_type = name

    def set_system_prompt(self, name: str) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name, content, expected_output, description "
                "FROM cfg.system_prompt WHERE name=%s",
                (name,),
            ).fetchone()
        if row is None:
            raise ConfigError(f"cfg.system_prompt {name!r} not found")
        want = self.EXPECTED_OUTPUT
        if want is not None and row["expected_output"] != want:
            raise ConfigError(
                f"{type(self).__name__} requires expected_output={want!r}, "
                f"got {row['expected_output']!r} (system_prompt={name!r})"
            )
        self.system_prompt_name = name
        self._prompt_row = row

    def set_doc_fn(self, fn: str | list[str] | Callable) -> None:
        """Set user-message producer. str: single col. list[str]: cols joined by newline.
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
            "system_prompt": self.system_prompt_name,
            "data_source": self.data_source,
            "source_uid": self.source_uid,
            "doc_fn": self.doc_fn,
            "endpoint": self._endpoint,
            "prompt_row": self._prompt_row,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ConfigError(
                f"setup incomplete; call set_* for: {missing}"
            )
        if not self.run_name:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.run_name = f"{self.system_prompt_name}-{ts}"
        self._carry_cols_resolved = self._resolve_carry_cols()

    def post_run_check(self) -> None:
        pass

    # ============================================================
    # Table / snapshot
    # ============================================================
    def _resolve_carry_cols(self) -> list[str]:
        uid_set = {self.source_uid} if isinstance(self.source_uid, str) else set(self.source_uid)  # type: ignore[arg-type]
        skip = uid_set | set(self._doc_cols)
        return [c for c in self.carry_cols if c not in skip]

    def _out_table_name(self) -> str:
        assert self._prompt_row is not None
        return self._prompt_row["name"]

    def _out_table_cols(self) -> dict[str, str]:
        return {
            "raw_output": "text",
            "doc": "text",
            "carry": "jsonb",
        }

    def _build_snapshot(self) -> dict:
        assert self._prompt_row is not None
        snap = super()._build_snapshot()
        snap["system_prompt"] = dict(self._prompt_row)
        snap["doc_cols"] = list(self._doc_cols)
        snap["carry_cols"] = list(self._carry_cols_resolved)
        return snap

    def _extra_log_cols(self) -> dict[str, Any]:
        assert self._prompt_row is not None
        return {**super()._extra_log_cols(), "system_prompt_content": self._prompt_row["content"]}

    # ============================================================
    # Row helpers
    # ============================================================
    def _row_get(self, row, key: str, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    def _fmt_payload(self, payload: Any) -> str:
        msg = str(payload.get("user", "")) if isinstance(payload, dict) else str(payload)
        msg = msg.replace("\n", " ")
        return f'"{msg[:120]}…"' if len(msg) > 120 else f'"{msg}"'

    def pre_step(self, row) -> dict:
        assert self._prompt_row is not None
        assert self.doc_fn is not None
        return {
            "system": self._prompt_row["content"],
            "user": self.doc_fn(row),
        }

    def step(self, payload: dict) -> Any:
        assert self._endpoint is not None
        return call(
            self.model,
            self._endpoint,
            payload["system"],
            payload["user"],
            self.config,
        )

    # ============================================================
    # Floor extras
    # ============================================================
    def _extras_floor(self, row, raw) -> dict[str, Any]:
        floor: dict[str, Any] = {}
        if raw is not None:
            floor["raw_output"] = raw if isinstance(raw, str) else str(raw)
        if self.doc_fn is not None:
            try:
                floor["doc"] = str(self.doc_fn(row))
            except Exception:
                pass
        floor["carry"] = Jsonb({c: self._row_get(row, c) for c in self._carry_cols_resolved})
        return floor

    def post_step(self, row, out) -> dict:
        raise NotImplementedError("subclass MinimalGenRun and implement post_step")
