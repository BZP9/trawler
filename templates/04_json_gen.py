"""TEMPLATE 04 — JSON-output LLM pipeline.

Same as 03 but prompt has expected_output='j'; output parsed into
json_output (jsonb). Parse failures → status=failed, error_category=ParseError.
"""
from trawler import JsonGenRun, from_db

gen = JsonGenRun()
gen.set_model("<MODEL_NAME>")
gen.set_model_type("<MODEL_TYPE>")
gen.set_system_prompt("<PROMPT_NAME>")     # expected_output='j'
gen.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
gen.set_doc_fn(["<COL_A>", "<COL_B>"])
gen.set_config(temperature=0.0, max_tokens=2000)   # 0.0 typical for extraction
gen.set_carry_cols(["<COL>"])              # query later: carry->>'<COL>'
gen.set_limit(5)                           # smoke first

if __name__ == "__main__":
    run_id = gen.run()
    print(f"run_id: {run_id}")

# Read results:
#   trawler.query.get_output("gen.<PROMPT_NAME>", run_id=run_id, status="ok")
#   SQL: SELECT row_key, json_output->>'<KEY>' FROM gen."<PROMPT_NAME>" WHERE status='ok';
