"""TEMPLATE 02 — load data into raw.* before a pipeline run.

ALWAYS pass pk= — it becomes source_uid in the pipeline. Omitting pk creates
a keyless table silently (no warning) and breaks resume/identity.
"""
import trawler

# --- JSONL (types inferred: int→bigint, dict/list→jsonb, str→text) --------
n = trawler.raw.load_from_jsonl(
    "<TABLE>", "<path/to/data.jsonl>",
    pk="<UID_COL>",              # composite: pk=["colA", "colB"]
    on_conflict="skip",          # "error" (default) | "skip" | "replace" (upsert)
)

# --- CSV (everything lands as text unless columns= given) ------------------
# n = trawler.raw.load_from_csv(
#     "<TABLE>", "<path/to/data.csv>",
#     columns={"<UID_COL>": "bigint", "<COL>": "text"},   # explicit types for CSV
#     pk="<UID_COL>",
# )

# --- copy from another table / another DB ----------------------------------
# n = trawler.raw.load_from_db("<DEST>", "gen.<SRC_TABLE>", pk="row_key")
# n = trawler.raw.load_from_db("<DEST>", "staging.jobs",
#                              src_dsn="postgresql://other-host/db",
#                              pk="id", truncate=True)     # truncate = full refresh

print(f"{n} rows written to raw.<TABLE>")
# Later, in the pipeline:  gen.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
