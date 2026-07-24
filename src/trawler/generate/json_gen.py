from __future__ import annotations
import json
import re
from typing import Any, ClassVar

from psycopg.types.json import Jsonb

from trawler.errors import ParseError
from trawler.generate.gen import MinimalGenRun


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _coerce_json(out: Any) -> Any:
    """Best-effort coerce step output → JSON value.

    Handles:
      - dict/list passthrough (clients that already parsed json_mode)
      - bytes → decode
      - str → strip fences, try parse, else extract first balanced block
    Raises ParseError on failure.
    """
    if isinstance(out, (dict, list)):
        return out
    if isinstance(out, bytes):
        out = out.decode()
    if not isinstance(out, str):
        return out
    s = out.strip()
    if not s:
        raise ParseError("empty output")
    m = _FENCE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    block = _first_json_block(s)
    if block is None:
        raise ParseError(
            f"no JSON value found in output (first 200 chars): {s[:200]!r}"
        )
    try:
        return json.loads(block)
    except json.JSONDecodeError as e:
        raise ParseError(
            f"extracted JSON block didn't parse: {e}; "
            f"block first 200 chars: {block[:200]!r}"
        ) from None


def _first_json_block(s: str) -> str | None:
    """Return the first balanced { … } or [ … ] substring, or None.
    Naive — doesn't fully respect strings. Good enough for LLM output.
    """
    starts = {"{": "}", "[": "]"}
    for i, ch in enumerate(s):
        if ch in starts:
            close = starts[ch]
            depth = 0
            for j in range(i, len(s)):
                if s[j] == ch:
                    depth += 1
                elif s[j] == close:
                    depth -= 1
                    if depth == 0:
                        return s[i:j + 1]
            return None
    return None


class JsonGenRun(MinimalGenRun):
    """Gen for `expected_output='j'`. Extracts JSON from LLM output → jsonb.

    Output table cols (in addition to MinimalGenRun's raw_output + carry cols):
      json_output (jsonb)  — parsed object/array
      error_category text  — set by backbone on failure (e.g. 'ParseError', 'BudgetError')
    """

    EXPECTED_OUTPUT: ClassVar[str | None] = "j"

    def _out_table_cols(self) -> dict[str, str]:
        cols = super()._out_table_cols()
        cols["json_output"] = "jsonb"
        return cols

    def _fmt_post(self, extras: dict) -> str:
        j = extras.get("json_output")
        if j is None:
            return ""
        obj = j.obj if hasattr(j, "obj") else j
        if isinstance(obj, dict):
            return f"keys={list(obj.keys())}"
        if isinstance(obj, list):
            return f"list[{len(obj)}]"
        return str(obj)[:60]

    def post_step(self, row, out) -> dict[str, Any]:
        parsed = _coerce_json(out)
        return {"json_output": Jsonb(parsed)}
