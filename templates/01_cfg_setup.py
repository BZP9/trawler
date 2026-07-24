"""TEMPLATE 01 — register cfg rows (do this BEFORE writing a pipeline).

Upserts are idempotent (ON CONFLICT DO UPDATE) — safe to re-run.
Names must match [A-Za-z0-9_-]{1,63}; prompt/encoder names become table names.
"""
import trawler.cfg as cfg

# --- system prompt: expected_output 't' (text) or 'j' (JSON) ------------
cfg.upsert_system_prompt(
    "<PROMPT_NAME>",                       # → output table gen.<PROMPT_NAME>
    content="<FULL SYSTEM PROMPT TEXT>",
    expected_output="j",                   # 'j' for JsonGenRun, 't' for TextGenRun
    description="<what this prompt does>",
)

# --- decoder (generation model) ------------------------------------------
cfg.upsert_decoder("<MODEL_NAME>", repo_name="<org/model-repo>")

# --- encoder (embedding model) — only for enc pipelines -------------------
# cfg.upsert_encoder("<ENC_NAME>", repo_name="<org/model>", dim=1024)

# --- model_type (transport) — usually already seeded; add only for new hosts
# protocol: 'openai' (LM Studio / llama.cpp / OpenAI) | 'ollama' | 'sentence_transformers'
# base_url_env: NAME of the env var holding the URL — never the URL itself.
cfg.upsert_model_type(
    "<MODEL_TYPE_NAME>",                   # e.g. remote_llamacpp
    "openai",
    base_url_env="<BASE_URL_ENV_VAR>",     # e.g. LLAMACPP_REMOTE_BASE_URL (must end /v1 for openai)
    description="<which machine/server this points at>",
)

print(cfg.list_cfg("model_type"))
