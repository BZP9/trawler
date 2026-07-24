"""TEMPLATE 08 — speed up a generation run: concurrency + retries,
paired with a llama.cpp server doing continuous batching.

Trawler side: set_concurrency(n) fires n LLM calls in parallel (threads).
Server side: the endpoint must actually serve parallel requests, or the
threads just queue. llama.cpp's llama-server does continuous batching
across parallel slots.

--- one-time on the model host (e.g. Mac Studio) ---------------------------
  brew install llama.cpp        # or build from source
  llama-server -m <path/to/model.gguf> \
      --host 0.0.0.0 --port 8080 \
      -np 8 -c 65536            # 8 parallel slots; ctx is SPLIT across slots
                                # (65536/8 = 8192 per request) — size accordingly
  # (flags current as of 2026-07; verify with `llama-server --help`)

--- one-time on this machine ----------------------------------------------
  export LLAMACPP_REMOTE_BASE_URL="http://<HOST_IP>:8080/v1"   # → ~/.zshrc
  # cfg row (once):  trawler.cfg.upsert_model_type(
  #     "remote_llamacpp", "openai", base_url_env="LLAMACPP_REMOTE_BASE_URL")
"""
from trawler import JsonGenRun, from_db

gen = JsonGenRun()
gen.set_model("<MODEL_NAME>")
gen.set_model_type("remote_llamacpp")     # openai protocol, llama.cpp server
gen.set_system_prompt("<PROMPT_NAME>")
gen.set_data_source(from_db("<TABLE>"), source_uid="<UID_COL>")
gen.set_doc_fn("<DOC_COL>")
gen.set_config(temperature=0.7, max_tokens=2000)

gen.set_concurrency(8)     # match the server's -np; more just queues
gen.set_retries(2)         # EndpointError (5xx/429/timeout) retried w/ backoff
# with concurrency, expect wall/row ≈ recent-latency / workers in the [eta] lines

if __name__ == "__main__":
    print(f"run_id: {gen.run()}")
