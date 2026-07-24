# Trawler

Tests: `uv run pytest -q`. Batch jobs run for hours — check `trawler status` before touching run infra.

| Doing | Read first |
|---|---|
| Any feature work | `AGENTS.md` |
| Pipeline/script for a known use case | `templates/` (skill → template → raw code, in that order) |
| First-time setup: new machine or new remote GPU box | `INIT.md` |
| End-to-end runs (offload loop / Mac Studio) | `MANUAL.md` — proven commands + env facts; don't re-derive |
| "improve" / "audit" / "evolve" | `EVOLVE.md`, then `uv run python scripts/audit.py` |

## Feature loop (every change)

1. Code + test
2. Update `SKILL.md` (project root) if any skill's API/trigger/scope changed — **never edit `~/.claude/skills/` directly**
3. `uv run python3 scripts/sync_skills.py`
4. Commit skill + code together; push
