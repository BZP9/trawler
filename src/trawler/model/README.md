# model

Protocol clients and config dataclasses. No static Python model registry — definitions live in `cfg.decoder` / `cfg.encoder` (see `table/README.md` and `cfg.py`).

## Why DB-backed

Adding a model = `trawler.cfg.upsert_decoder(name, repo_name)`. No code edit, no restart. Mutable, queryable, snapshot-friendly (full row frozen into `_gen_log.config` / `_enc_log.config` at register time).

## Resolution chain

Model row and model_type are independent. Caller picks model_type per run.

```
# step 1 — model row only (in set_model setter)
SELECT * FROM cfg.decoder WHERE name='qwen3_8b'
  → DecoderConfig(name, repo_name, format, description)

# step 2 — combine with model_type (in set_model_type setter)
SELECT * FROM cfg.model_type WHERE name='local_ollama'
  + os.environ[base_url_env]  → base_url
  + os.environ.get(api_key_env) → api_key
  → ResolvedEndpoint(model_type, protocol, base_url, api_key)
```

Missing env var → raise before `run()` registers anything.

## Config dataclasses

```python
@dataclass(frozen=True)
class DecoderConfig:
    name: str
    repo_name: str
    format: dict | None
    description: str | None

@dataclass(frozen=True)
class EncoderConfig:
    name: str
    repo_name: str
    dim: int
    format: dict | None
    description: str | None

@dataclass(frozen=True)
class ResolvedEndpoint:
    model_type: str       # cfg.model_type.name
    protocol: str         # 'ollama' | 'openai' | 'anthropic' | 'sentence_transformers'
    base_url: str         # resolved from env var; empty string for local protocols
    api_key: str | None   # resolved from env var; None if not needed
```

`model_type` string is not on the model row — chosen per run. Same model can be called via `local_ollama` one run, `remote_lms` another.

## Client dispatch

### Generation

```python
def call(model: DecoderConfig, endpoint: ResolvedEndpoint,
         system: str, user: str, params: dict) -> str:
    ...  # dispatches by endpoint.protocol
```

| protocol | function | endpoint |
|----------|----------|----------|
| `ollama` | `ollama_call` | POST `{base_url}/api/chat` |
| `openai` | `openai_call` | POST `{base_url}/chat/completions` (base_url must include `/v1`) |
| `anthropic` | `anthropic_call` | POST `{base_url}/v1/messages` (base_url is bare host, e.g. https://api.anthropic.com; api_key via x-api-key header) |

### Embedding

```python
def embed(model: EncoderConfig, endpoint: ResolvedEndpoint,
          doc: str, params: dict) -> list[float]:
    ...  # dispatches by endpoint.protocol
```

| protocol | function | transport |
|----------|----------|-----------|
| `ollama` | `ollama_embed` | POST `{base_url}/api/embed` |
| `openai` | `openai_embed` | POST `{base_url}/embeddings` |
| `sentence_transformers` | `sentence_transformer_embed` | in-process; lazy import; model cached per process |

Adding a protocol = one new function + one dict entry. Adding a deployment = INSERT `cfg.model_type` row + export env var.

All HTTP clients accept a `timeout` key in `params` (seconds; default 600 via `DEFAULT_TIMEOUT`) — set per run with `set_config(timeout=...)`.

## Error mapping

| condition | exception |
|-----------|-----------|
| HTTP 5xx / 429 or timeout / connection refused | `EndpointError` (transient — retried by the backbone when `set_retries` is set) |
| other HTTP 4xx | `ProtocolError` |
| `finish_reason='length'` with content | `BudgetError` |
| empty content + reasoning_content + length | `BudgetError` |
| empty content otherwise | `ProtocolError` |

All categories defined in `trawler/errors.py`.
