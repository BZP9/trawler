"""TEMPLATE 09 — after a run: status, failures, output rows.

Run with:  uv run python 09_inspect_query.py
"""
import trawler

RUN_ID = "<RUN_ID>"                        # from the run script / gen._gen_log

# --- run health -------------------------------------------------------------
print(trawler.inspect.list_runs(schema="gen", limit=5))        # recent runs
stats = trawler.inspect.run_stats(RUN_ID, schema="gen")
print(stats)   # {status, n_done, n_failed, pct_done, out_table, by_category}

# --- what failed, and why ----------------------------------------------------
for row in trawler.inspect.failed_rows(RUN_ID, limit=10):
    print(row["row_key"], row["error_category"])

# --- read output rows ---------------------------------------------------------
rows = trawler.query.get_output("gen.<PROMPT_NAME>", run_id=RUN_ID,
                                status="ok", limit=10)
for r in rows:
    print(r["row_key"], str(r.get("json_output") or r.get("raw_output"))[:80])

# --- table overview ------------------------------------------------------------
print(trawler.query.table_row_counts("gen"))
