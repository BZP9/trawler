"""TEMPLATE 07 — embedding pipeline. Output: enc.<ENC_NAME> with vec + doc.

Encoder dim is validated per row (len(vec)==dim) — preflight catches
mismatches before anything is written.
"""
from trawler import MinimalEncodeRun, from_db

enc = MinimalEncodeRun()
enc.set_model("<ENC_NAME>")                # cfg.encoder row (name, repo_name, dim)
# transport options:
#   local_sentence_transformer  (in-process, GPU, no HTTP)
#   remote_lms / <openai-compatible>  (base_url env must end /v1)
#   remote_ollama / local_ollama
enc.set_model_type("<MODEL_TYPE>")
enc.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
enc.set_doc_fn(["<COL_A>", "<COL_B>"])
enc.set_config(normalize=True)
enc.set_batch_size(32)                     # N docs per call — big speedup;
                                           # one failed batch-call fails all rows in it
# enc.set_retries(2)                       # retry EndpointError (5xx/429/timeout)
enc.set_limit(5)                           # smoke first

if __name__ == "__main__":
    print(f"run_id: {enc.run()}")

# Search later (pgvector):
#   SELECT row_key, doc FROM enc."<ENC_NAME>" WHERE status='ok'
#   ORDER BY vec <=> %s::vector LIMIT 10;
