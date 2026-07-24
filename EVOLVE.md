# EVOLVE.md — Self-Improvement Playbook for Agents

Repeatable procedure for an LLM agent to find, prioritize, and ship
improvements to Trawler without hand-holding. Read AGENTS.md first —
it owns the per-feature checklists; this file owns the *finding* of work.

## When to run a pass

- The user says "improve", "audit", "evolve", or "what's wrong with this repo"
- After landing any sizable feature (new pipeline type, new protocol)
- Before a release / version bump

## Step 1 — mechanical audit (always first)

```sh
uv run python scripts/audit.py
```

| check | contract it enforces | fix |
|-------|----------------------|-----|
| tests pass | no regressions | fix code or test, never delete the test |
| no `raise RuntimeError` | AGENTS.md: library errors are `RowInferError` subclasses (or `ValueError` for tool-arg validation) | pick the right category from `errors.py` |
| error classes exported + documented | every `RowInferError` subclass in `__all__`, SKILL.md, run/README.md | add to all three |
| subpackages have `__init__.py` | `importlib.resources` + packaging | add the file |
| installed skills match SKILL.md (warn) | skill drift after edits | `uv run python scripts/sync_skills.py` |
| dependencies import | pyproject ↔ lockfile ↔ env agreement | `uv sync --extra local` |
| no untracked source files (warn) | AGENTS.md: `git add` by file — easy to miss new files | add or gitignore deliberately |

A FAIL is always worth fixing before anything else — these are contracts the
codebase has already committed to.

When you find a new *mechanically checkable* contract (while fixing a bug or
reading AGENTS.md), add a check function to `scripts/audit.py` in the same
pass. That is how this loop compounds: every manual finding should leave
behind either a regression test or an audit check.

## Step 2 — manual smell hunt

Ranked heuristics. Each was a real finding in a past pass (see log at bottom)
— search for the *pattern*, not the specific instance, since fixed instances
can regress or reappear in new code.

| rank | smell | how to detect | past example |
|------|-------|---------------|--------------|
| 1 | data written wrong / lost | read `_write_row` / floor-merge paths; check every exception path still persists the floor | `_extras_floor` merged before extras (pre-existing, keep intact) |
| 2 | transient failure kills work | grep for error categories documented as "retryable" and confirm something actually retries; check HTTP code → category mapping for misfiled transients | `EndpointError` documented retryable but nothing retried; 429 filed as `ProtocolError` |
| 3 | boundary overshoot | any `limit` / `batch_size` / `concurrency` interaction: trace worst case by hand (limit=4, batch=32; limit=5, workers=4) | concurrent mode overshot limit by up to `concurrency-1` rows |
| 4 | double-paying side effects | any "dry run" / "check" that repeats a paid call later | preflight called the LLM on row 1, then row 1 ran again |
| 5 | resource churn in hot loops | anything opened per row: connections, sessions, model loads | 2+ psycopg connects per row → pool |
| 6 | contract drift vs AGENTS.md | reread AGENTS.md checklists against reality; automate what you find (→ audit.py) | gen setters raised `RuntimeError` despite the ConfigError rule |
| 7 | library etiquette | `print` in library code, hardcoded timeouts, no way to silence | prints → `trawler` logger; timeout → `set_config(timeout=...)` |
| 8 | untested hard paths | coverage of concurrency/batch/resume/interrupt code; anything with threads or partial failure | batch + concurrent modes shipped with zero tests |
| 9 | doc drift | every behavior change: check the doc touch map below | error tables said "4xx" after 429 moved |

## Step 3 — prioritize and confirm

Rank by user impact: **stored-data correctness > run reliability > cost >
performance > developer experience > style.**

- Small fixes inside the current contract (rank 1–5, mechanical drift): just do them.
- Scope changes (new dependency, new public API, behavior a user might rely on): list them with a recommendation and let the user pick before coding.

## Step 4 — ship

Follow the feature loop in CLAUDE.md / AGENTS.md. Additionally, for an
evolution pass:

1. Every behavior fix gets a **DB-free regression test** (stub the DB layer
   like `tests/test_run_lifecycle.py::FakeRun` does — no Postgres in tests).
2. Every new mechanical contract gets an **audit.py check**.
3. Touch the right docs:

| change type | docs to update |
|-------------|----------------|
| new setter / run behavior | SKILL.md (dec + enc setter tables), run/README.md, generate/ or encode/README.md |
| error category / mapping | SKILL.md backbone table, run/README.md, model/README.md, root README.md |
| new protocol | model/README.md, table/README.md seed rows, SKILL.md dec/enc protocol tables |
| cfg table / column | table/README.md, SKILL.md trawler-cfg block |
| new skill or trigger change | SKILL.md + `uv run python scripts/sync_skills.py` |

4. Append an entry to the evolution log below — findings, fixes, commit —
   so the next pass (or next agent) doesn't rediscover the same ground.

## Known backlog (unclaimed work)

- **typing cleanup**: pyright reports dict_row / f-string-SQL noise across
  the db-touching modules; needs a decision (psycopg.sql composition vs
  targeted ignores), not piecemeal suppression.
- **offload doc_fn**: bundle can only ship column-based docs (`--doc-col`);
  custom doc_fn logic requires precomputing into a staging table. Consider
  a `--doc-sql` or serialized doc template if this recurs.

## Evolution log (append-only)

### 2026-07-02 — pass 1 (commit 4611e9f + follow-up)

Audit of the whole library after batch/concurrency features landed.
Fixed: no-retry on retryable errors (`set_retries`), 429 miscategorized,
per-row connection churn (pool + throttled progress), limit overshoot in
concurrent and batch modes, preflight double-call, prints → logging
(`set_verbose`), gen setters RuntimeError → ConfigError, cfg name
validation, configurable HTTP timeout, resume n_rows/n_done refresh,
inspect/init RuntimeError → ValueError/ConfigError. Added DB-free test
suite (lifecycle/clients/cfg names) and `scripts/audit.py`.
Deferred (user decision pending): CI, anthropic protocol.

### 2026-07-07 — pass 2 (offload loop follow-up)

Pass after run-bundle/import landed. Fixed: run-bundle had no early stop
(dead endpoint would grind a timeout per pending row) — added
`early_stop` (default 10 consecutive fails, `--early-stop`, 0/None
disables); runner print() → `trawler` logger (etiquette parity with
BaseRun); preflight wrapped RowInferError into RuntimeError erasing the
error category (backlog #4) — now re-raised as-is. Cleared backlog: CI
(`.github/workflows/ci.yml`: pytest + audit), anthropic protocol client
(`anthropic_call` in clients.py, x-api-key + anthropic-version headers,
refusal/max_tokens/empty branches, seed row + live cfg row). New tests:
early-stop x3, runner concurrency, preflight category regression,
anthropic_call x5. 150 tests green.
