"""TEMPLATE 03 — text-output LLM pipeline.

Needs (ask the user if missing): source table + uid col, doc col(s),
model + model_type, prompt name (expected_output='t').
Output lands in gen.<PROMPT_NAME> (raw_output, doc, carry, status...).
"""
from trawler import TextGenRun, from_db

gen = TextGenRun()
gen.set_model("<MODEL_NAME>")              # cfg.decoder row
gen.set_model_type("<MODEL_TYPE>")         # cfg.model_type row, env var must be set
gen.set_system_prompt("<PROMPT_NAME>")     # cfg.system_prompt, expected_output='t'
gen.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
gen.set_doc_fn("<DOC_COL>")                # or ["colA", "colB"] joined by \n
gen.set_config(temperature=0.7, max_tokens=2000)   # timeout=600 default
# gen.set_carry_cols(["<COL>"])            # copy source cols into carry jsonb
gen.set_limit(5)                           # smoke first; raise/remove for full run

if __name__ == "__main__":
    run_id = gen.run()
    print(f"run_id: {run_id}")             # keep this — needed for resume/inspect
