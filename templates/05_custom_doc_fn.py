"""TEMPLATE 05 — callable doc_fn + chaining one gen run into another.

Use when the user message needs assembly logic (formatting, sampling,
combining cols) instead of a plain column. Pattern taken from a real
finetune data-gen script (dims2jd).
"""
from trawler import JsonGenRun, from_gen

UPSTREAM_RUN_ID = "<RUN_ID_OF_UPSTREAM_GEN_RUN>"


def make_doc(r: dict) -> str:
    """r is the full source row (dict). Return the user message string.

    Keep it PURE per-row if possible. If you need cross-row state (lookup
    maps, style exemplars), load it ONCE at module level — but note that
    makes the script non-portable for `trawler bundle` offload.
    """
    parsed = r["json_output"]              # from_gen rows carry json_output (jsonb → dict)
    return f"""<SECTION HEADER>
{parsed["<KEY>"]}

<ANOTHER SECTION>
{r["doc"]}"""


gen = JsonGenRun()
gen.set_model("<MODEL_NAME>")
gen.set_model_type("<MODEL_TYPE>")
gen.set_system_prompt("<PROMPT_NAME>")
# chain: feed a previous gen run's ok rows into this run.
# run_id= is parameterized (safe); where= is raw SQL you own.
gen.set_data_source(
    from_gen("<UPSTREAM_PROMPT_NAME>", run_id=UPSTREAM_RUN_ID, where="status='ok'"),
    source_uid="row_key",                  # gen/enc outputs are keyed by row_key
)
gen.set_doc_fn(make_doc)
gen.set_config(temperature=0.7, max_tokens=4000)
gen.set_limit(5)

if __name__ == "__main__":
    print(f"run_id: {gen.run()}")


# ---------------------------------------------------------------------------
# OFFLOAD VARIANT — materializing this same make_doc() into a raw.* table
# ---------------------------------------------------------------------------
# `trawler bundle` (offload) can't ship a Python doc_fn — the remote has no
# Postgres, only literal columns. So instead of set_doc_fn(make_doc) above,
# write a separate stage_<task>.py that runs make_doc() HERE (where Postgres
# is reachable) and writes the result into a plain raw table that bundle can
# then read via --source raw.<table> --doc-col doc.
#
# MANDATORY: every stage_<task>.py's docstring must state, in the first few
# lines: (1) that it's a Trawler pre-offload staging script, (2) which
# prompt/bundle it feeds, (3) why it exists (bundle can't ship a doc_fn).
# A future session — or a small model following SKILL.md's offload gate —
# must be able to tell what this script is for from its header alone,
# without reading the body. Worked example (trimmed, from a real task):
#
#   """Materialize raw.dims2jd_docs from ALL ok jd2dims rows (latest-ok union).
#
#   Offload bundles can't ship a doc_fn (Trawler MANUAL.md: custom docs must
#   be pre-materialized into a staging raw table). This rebuilds the staging
#   table that `trawler bundle --source raw.dims2jd_docs --pk row_key
#   --doc-col doc` consumes ...
#
#   Run: uv run python stage_dims2jd_docs.py   (needs TRAWLER_DSN or ROWINFER_DSN)
#   """
#
# Sketch:
#
# with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
#     rows = conn.execute("SELECT ... FROM gen.<UPSTREAM_PROMPT> WHERE status='ok'").fetchall()
#     conn.execute('CREATE TABLE IF NOT EXISTS raw.<table> (row_key text PRIMARY KEY, doc text NOT NULL)')
#     with conn.cursor() as cur:
#         cur.executemany(
#             "INSERT INTO raw.<table> (row_key, doc) VALUES (%s, %s) "
#             "ON CONFLICT (row_key) DO UPDATE SET doc = EXCLUDED.doc",
#             [(r["row_key"], make_doc(r)) for r in rows],
#         )
# Upsert, never DROP/rename the live table — see SKILL.md trawler-offload's
# Hard NOs for what goes wrong when a stage script destructively replaces it.
