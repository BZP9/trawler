from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any

from trawler.errors import BudgetError, ConfigError, EndpointError, ProtocolError
from trawler.model.types import DecoderConfig, EncoderConfig, ResolvedEndpoint


# per-request timeout (s); override per run via set_config(timeout=...)
DEFAULT_TIMEOUT = 600


def ollama_call(model: DecoderConfig, endpoint: ResolvedEndpoint,
                system: str, user: str, params: dict) -> str:
    body: dict[str, Any] = {
        "model": model.repo_name,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    opts: dict[str, Any] = {}
    if "temperature" in params:
        opts["temperature"] = params["temperature"]
    if "max_tokens" in params:
        opts["num_predict"] = params["max_tokens"]
    if opts:
        body["options"] = opts
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/api/chat",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    return resp["message"]["content"]


def openai_call(model: DecoderConfig, endpoint: ResolvedEndpoint,
                system: str, user: str, params: dict) -> str:
    body: dict[str, Any] = {
        "model": model.repo_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    for k in ("temperature", "max_tokens", "top_p"):
        if k in params:
            body[k] = params[k]
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/chat/completions",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    choice = resp["choices"][0]
    msg = choice["message"]
    content = (msg.get("content") or "").strip()
    finish = choice.get("finish_reason")
    if content:
        if finish == "length":
            # Got something but truncated. Surface as Budget so user knows to bump.
            raise BudgetError(
                f"truncated (finish_reason='length') from {model.repo_name}; "
                f"raise max_tokens. Got {len(content)} chars."
            )
        return content
    # Empty content. Inspect for reasoning model.
    reasoning = msg.get("reasoning_content") or ""
    usage = resp.get("usage", {}).get("completion_tokens_details", {})
    rtoks = usage.get("reasoning_tokens", "?")
    if finish == "length" and reasoning:
        raise BudgetError(
            f"empty content from {model.repo_name} "
            f"(reasoning_tokens={rtoks}, reasoning_chars={len(reasoning)}, "
            f"finish_reason='length'). Reasoning model ran out of budget; "
            f"raise max_tokens. First 200 chars of reasoning: {reasoning[:200]!r}"
        )
    raise ProtocolError(
        f"empty content from {model.repo_name} "
        f"(finish_reason={finish!r}, reasoning_chars={len(reasoning)})"
    )


def anthropic_call(model: DecoderConfig, endpoint: ResolvedEndpoint,
                   system: str, user: str, params: dict) -> str:
    """Anthropic Messages API (POST {base_url}/v1/messages).

    base_url is the bare host (https://api.anthropic.com) — no /v1 suffix,
    matching the ANTHROPIC_BASE_URL convention. api_key goes in the
    x-api-key header (not Authorization: Bearer). max_tokens is required
    by the API; defaults to 16000 if not set via set_config.
    """
    if not endpoint.api_key:
        raise ConfigError(
            f"model_type {endpoint.model_type!r} (anthropic protocol) needs "
            "an api_key_env pointing at a set env var"
        )
    body: dict[str, Any] = {
        "model": model.repo_name,
        "max_tokens": params.get("max_tokens", 16000),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if "temperature" in params:
        body["temperature"] = params["temperature"]
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/v1/messages",
        body,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
        extra_headers={
            "x-api-key": endpoint.api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    stop = resp.get("stop_reason")
    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text").strip()
    if stop == "refusal":
        raise ProtocolError(
            f"refusal from {model.repo_name} "
            f"(stop_details: {resp.get('stop_details')})"
        )
    if not text:
        raise ProtocolError(
            f"empty content from {model.repo_name} (stop_reason={stop!r})"
        )
    if stop == "max_tokens":
        raise BudgetError(
            f"truncated (stop_reason='max_tokens') from {model.repo_name}; "
            f"raise max_tokens. Got {len(text)} chars."
        )
    return text


CHAT_CLIENTS = {
    "ollama": ollama_call,
    "openai": openai_call,
    "anthropic": anthropic_call,
}


def call(model: DecoderConfig, endpoint: ResolvedEndpoint,
         system: str, user: str, params: dict) -> str:
    fn = CHAT_CLIENTS.get(endpoint.protocol)
    if fn is None:
        raise ConfigError(f"unknown chat protocol {endpoint.protocol!r}")
    return fn(model, endpoint, system, user, params)


# =====================================================================
# Embedding clients — same endpoint resolve infra as chat (cfg.model_type
# + base_url_env env vars), different paths within each protocol.
# =====================================================================

def ollama_embed(model: EncoderConfig, endpoint: ResolvedEndpoint,
                 doc: str, params: dict) -> list[float]:
    body = {"model": model.repo_name, "input": doc}
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/api/embed",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    embeddings = resp.get("embeddings")
    if not embeddings:
        raise ProtocolError(f"no embeddings in ollama response from {model.repo_name}")
    return list(embeddings[0])


def openai_embed(model: EncoderConfig, endpoint: ResolvedEndpoint,
                 doc: str, params: dict) -> list[float]:
    body = {"model": model.repo_name, "input": doc}
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/embeddings",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    data = resp.get("data")
    if not data:
        raise ProtocolError(f"no data in openai response from {model.repo_name}")
    return list(data[0]["embedding"])


_SENTENCE_TRANSFORMER_CACHE: dict[str, Any] = {}


def _load_st(repo_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ConfigError(
            "sentence_transformers protocol requires the sentence-transformers package. "
            "Install: uv sync --extra local"
        ) from None
    st = _SENTENCE_TRANSFORMER_CACHE.get(repo_name)
    if st is None:
        st = SentenceTransformer(repo_name)
        _SENTENCE_TRANSFORMER_CACHE[repo_name] = st
    return st


def sentence_transformer_embed(model: EncoderConfig, endpoint: ResolvedEndpoint,
                               doc: str, params: dict) -> list[float]:
    """Local in-process embedding. No HTTP. Loads model once per process."""
    st = _load_st(model.repo_name)
    normalize = bool(params.get("normalize", True))
    vec = st.encode([doc], normalize_embeddings=normalize, show_progress_bar=False)[0]
    return vec.tolist()


EMBED_CLIENTS = {
    "ollama":                ollama_embed,
    "openai":                openai_embed,
    "sentence_transformers": sentence_transformer_embed,
}


def embed(model: EncoderConfig, endpoint: ResolvedEndpoint,
          doc: str, params: dict) -> list[float]:
    fn = EMBED_CLIENTS.get(endpoint.protocol)
    if fn is None:
        raise ConfigError(f"unknown embed protocol {endpoint.protocol!r}")
    return fn(model, endpoint, doc, params)


# =====================================================================
# Batch embedding — same protocols, array input in a single request.
# =====================================================================

def ollama_embed_batch(model: EncoderConfig, endpoint: ResolvedEndpoint,
                       docs: list[str], params: dict) -> list[list[float]]:
    body = {"model": model.repo_name, "input": docs}
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/api/embed",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    embeddings = resp.get("embeddings")
    if not embeddings:
        raise ProtocolError(f"no embeddings in ollama response from {model.repo_name}")
    return [list(e) for e in embeddings]


def openai_embed_batch(model: EncoderConfig, endpoint: ResolvedEndpoint,
                       docs: list[str], params: dict) -> list[list[float]]:
    body = {"model": model.repo_name, "input": docs}
    resp = _post_json(
        f"{endpoint.base_url.rstrip('/')}/embeddings",
        body,
        api_key=endpoint.api_key,
        timeout=params.get("timeout", DEFAULT_TIMEOUT),
    )
    data = resp.get("data")
    if not data:
        raise ProtocolError(f"no data in openai response from {model.repo_name}")
    data.sort(key=lambda x: x["index"])
    return [list(x["embedding"]) for x in data]


def sentence_transformer_embed_batch(model: EncoderConfig, endpoint: ResolvedEndpoint,
                                     docs: list[str], params: dict) -> list[list[float]]:
    st = _load_st(model.repo_name)
    normalize = bool(params.get("normalize", True))
    vecs = st.encode(docs, normalize_embeddings=normalize, show_progress_bar=False)
    return [v.tolist() for v in vecs]


EMBED_BATCH_CLIENTS = {
    "ollama":                ollama_embed_batch,
    "openai":                openai_embed_batch,
    "sentence_transformers": sentence_transformer_embed_batch,
}


def embed_batch(model: EncoderConfig, endpoint: ResolvedEndpoint,
                docs: list[str], params: dict) -> list[list[float]]:
    fn = EMBED_BATCH_CLIENTS.get(endpoint.protocol)
    if fn is None:
        raise ConfigError(f"unknown embed protocol {endpoint.protocol!r}")
    return fn(model, endpoint, docs, params)


def _post_json(url: str, body: dict, api_key: str | None = None,
               timeout: float = DEFAULT_TIMEOUT,
               extra_headers: dict[str, str] | None = None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = "<no body>"
        msg = f"HTTP {e.code} {e.reason} from {url}\n  body: {err_body}"
        # 5xx + 429 = transient / retryable; other 4xx = client/protocol issue
        if 500 <= e.code < 600 or e.code == 429:
            raise EndpointError(msg) from None
        raise ProtocolError(msg) from None
    except urllib.error.URLError as e:
        raise EndpointError(f"URLError calling {url}: {e.reason}") from None
