#!/usr/bin/env bash
# TEMPLATE 10 — export a portable offline job dir (`trawler bundle`).
#
# Snapshot cfg (prompt/decoder/model_type) + PENDING source rows (source
# minus already-ok rows in gen.<prompt>) into <out>/<job-id>/ containing
# job.toml, rows.jsonl, job.sqlite. Copy that dir to another machine and
# run without Postgres. base_url is NEVER written — remote resolves it
# from its own env (base_url_env named in job.toml).
#
# LIMITS (check before choosing this path):
#   - source must be a plain table (schema.table) — no where= filter, no run_id filter.
#     If your pipeline uses from_gen(...)/where=, materialize it first:
#     trawler.raw.load_from_db("<staging>", "gen.<upstream>", pk="row_key")
#   - custom doc_fn is NOT shipped — precompute the doc into a source column
#     and name it in --doc-col.

# [1] LOCAL — export (needs Postgres). --doc-col builds the user message
#     on the remote (cols joined by newline, like set_doc_fn(list)).
uv run trawler bundle \
  --prompt      "<PROMPT_NAME>" \
  --decoder     "<MODEL_NAME>" \
  --model-type  "<MODEL_TYPE>" \
  --source      "<SCHEMA.TABLE>" \
  --pk          "<UID_COL>" \
  --doc-col     "<DOC_COL_A>" "<DOC_COL_B>" \
  --limit       1000 \
  --out         output/jobs
# → output/jobs/<job-id>/   copy this dir to the remote (scp/rsync/USB)

# [2] REMOTE — execute (no Postgres; needs trawler installed + the env var
#     named by model_type.base_url_env pointing at its local LLM server).
#     Resumable: re-run to retry failures / continue after interrupt.
uv run trawler run-bundle "output/jobs/<JOB_ID>" \
  --concurrency 8 --retries 2 --max-tokens 4000

# [3] LOCAL — copy the dir back, merge into Postgres.
uv run trawler import "output/jobs/<JOB_ID>"
# → new run in gen._gen_log, rows in gen.<PROMPT_NAME>; re-import needs --force
