# Trawler templates — copy, fill, run

Guideline tiers for any LLM (or human) working with Trawler:

1. **Skill** (`SKILL.md` / `~/.claude/skills/trawler-*`) — concepts, API tables, invariants. Read first.
2. **Template** (this folder) — a runnable skeleton per use case. Copy the matching file, replace every `<PLACEHOLDER>`, run it. Don't invent structure; if the template doesn't fit, you probably want a different template.
3. **Raw code** (`src/trawler/`) — last resort, when skill + template don't answer it.

| # | file | use case |
|---|------|----------|
| 01 | `01_cfg_setup.py` | register prompt / decoder / encoder / model_type |
| 02 | `02_raw_ingest.py` | load CSV / JSONL / another DB table into `raw.*` |
| 03 | `03_text_gen.py` | text-output LLM pipeline (`TextGenRun`) |
| 04 | `04_json_gen.py` | JSON-output LLM pipeline (`JsonGenRun`) |
| 05 | `05_custom_doc_fn.py` | callable doc_fn + chaining one gen run into another |
| 06 | `06_resume_topup.py` | resume an interrupted run / top up to N ok rows |
| 07 | `07_embedding.py` | embedding pipeline (`MinimalEncodeRun`) with batching |
| 08 | `08_concurrency_speed.py` | speed up generation: concurrency + retries + llama.cpp server |
| 09 | `09_inspect_query.py` | check run status / errors / read output rows |
| 10 | `10_bundle_remote.sh` | full offload loop: bundle export → remote run-bundle → import |

Rules that apply to every template:

- `TRAWLER_DSN` (or legacy `ROWINFER_DSN`) must be set; setters validate against `cfg.*` at call time, so typos fail before anything is written.
- Prompt name becomes the output table: `gen.<prompt_name>`. Encoder name becomes `enc.<encoder_name>`.
- `source_uid` must be the pk you loaded the raw table with (see 02).
- Run scripts with `uv run python <file>` from a repo whose env has trawler installed.
