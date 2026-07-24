from __future__ import annotations
from typing import Any, ClassVar

from trawler.generate.gen import MinimalGenRun


class TextGenRun(MinimalGenRun):
    """Gen for `expected_output='t'` prompts. Raw text only.

    Output table cols:
      raw_output (text)        — the LLM response verbatim
      carry_cols (jsonb each)  — copied from the source row
      error_category (text)    — set by backbone on per-row failure
    """

    EXPECTED_OUTPUT: ClassVar[str | None] = "t"

    def post_step(self, row, out) -> dict[str, Any]:
        # raw_output saved by _extras_floor; nothing extra needed here.
        return {}
