"""TEMPLATE 06 — resume an interrupted run / top up to N ok rows.

Use after Ctrl-C, LLM host down, or partial failures. Same script as the
original run + set_resume(run_id). Already-ok rows are skipped; failed and
missing rows are (re)processed under the SAME run_id and output table.
"""
from trawler import JsonGenRun, from_db

PREV_RUN_ID = "<RUN_ID_PRINTED_BY_THE_ORIGINAL_RUN>"

gen = JsonGenRun()                          # must match the original class
gen.set_model("<MODEL_NAME>")               # same setup as original run
gen.set_model_type("<MODEL_TYPE>")          # (backend MAY change, e.g. faster host)
gen.set_system_prompt("<PROMPT_NAME>")
gen.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
gen.set_doc_fn("<DOC_COL>")
gen.set_config(temperature=0.0, max_tokens=2000)

gen.set_resume(PREV_RUN_ID)                 # skip ok, retry failed/missing

# limit semantics on resume — pick ONE:
gen.set_limit(1000, total=True)   # top up UNTIL table has 1000 ok rows total
# gen.set_limit(200)              # process 200 MORE rows this pass

if __name__ == "__main__":
    print(f"run_id: {gen.run()}")           # == PREV_RUN_ID
