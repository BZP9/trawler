"""DB-free tests for model clients: openai_call branches, HTTP error
categorization in _post_json (429 → EndpointError), timeout passthrough.
"""
from __future__ import annotations
import io
import json
import urllib.error

import pytest

import trawler.model.clients as clients
from trawler.errors import BudgetError, EndpointError, ProtocolError
from trawler.model.types import DecoderConfig, ResolvedEndpoint


MODEL = DecoderConfig(name="m", repo_name="repo/m", format=None, description=None)
EP = ResolvedEndpoint(model_type="t", protocol="openai",
                      base_url="http://x/v1", api_key=None)


def _openai_resp(content, finish="stop", reasoning=None, reasoning_tokens=None):
    msg = {"content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    resp: dict = {"choices": [{"message": msg, "finish_reason": finish}]}
    if reasoning_tokens is not None:
        resp["usage"] = {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}
    return resp


# ---------------------------------------------------------------------------
# openai_call branches
# ---------------------------------------------------------------------------

def test_openai_call_ok(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: _openai_resp("hello"))
    assert clients.openai_call(MODEL, EP, "sys", "usr", {}) == "hello"


def test_openai_call_truncated_raises_budget(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: _openai_resp("partial…", finish="length"))
    with pytest.raises(BudgetError, match="truncated"):
        clients.openai_call(MODEL, EP, "sys", "usr", {})


def test_openai_call_reasoning_exhausted_raises_budget(monkeypatch):
    monkeypatch.setattr(
        clients, "_post_json",
        lambda *a, **k: _openai_resp("", finish="length",
                                     reasoning="thinking…", reasoning_tokens=2000),
    )
    with pytest.raises(BudgetError, match="reasoning"):
        clients.openai_call(MODEL, EP, "sys", "usr", {})


def test_openai_call_empty_raises_protocol(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: _openai_resp("", finish="stop"))
    with pytest.raises(ProtocolError, match="empty content"):
        clients.openai_call(MODEL, EP, "sys", "usr", {})


# ---------------------------------------------------------------------------
# _post_json error categorization
# ---------------------------------------------------------------------------

def _http_error(code):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"detail"),
    )


@pytest.mark.parametrize("code,exc", [
    (500, EndpointError),
    (503, EndpointError),
    (429, EndpointError),   # rate limit is transient → retryable
    (400, ProtocolError),
    (404, ProtocolError),
])
def test_post_json_http_error_mapping(monkeypatch, code, exc):
    def fake_urlopen(req, timeout=None):
        raise _http_error(code)

    monkeypatch.setattr(clients.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(exc, match=f"HTTP {code}"):
        clients._post_json("http://x", {})


def test_post_json_url_error_is_endpoint(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(clients.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(EndpointError, match="URLError"):
        clients._post_json("http://x", {})


# ---------------------------------------------------------------------------
# timeout passthrough
# ---------------------------------------------------------------------------

class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _capture_urlopen(seen):
    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResp(json.dumps(
            {"message": {"content": "hi"}}
        ).encode())
    return fake_urlopen


def test_timeout_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(clients.urllib.request, "urlopen", _capture_urlopen(seen))
    clients.ollama_call(MODEL, EP, "sys", "usr", {})
    assert seen["timeout"] == clients.DEFAULT_TIMEOUT


def test_timeout_from_config(monkeypatch):
    seen = {}
    monkeypatch.setattr(clients.urllib.request, "urlopen", _capture_urlopen(seen))
    clients.ollama_call(MODEL, EP, "sys", "usr", {"timeout": 5})
    assert seen["timeout"] == 5


# ---------------------------------------------------------------------------
# anthropic_call branches
# ---------------------------------------------------------------------------

from trawler.errors import ConfigError

ANTHROPIC_EP = ResolvedEndpoint(model_type="anthropic", protocol="anthropic",
                                base_url="https://api.anthropic.com",
                                api_key="sk-test")


def _anthropic_resp(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop}


def test_anthropic_call_ok(monkeypatch):
    captured = {}

    def fake_post(url, body, api_key=None, timeout=None, extra_headers=None):
        captured.update(url=url, body=body, headers=extra_headers)
        return _anthropic_resp("hello")

    monkeypatch.setattr(clients, "_post_json", fake_post)
    out = clients.anthropic_call(MODEL, ANTHROPIC_EP, "sys", "usr", {})
    assert out == "hello"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "sys"
    assert captured["body"]["max_tokens"] == 16000       # required by the API


def test_anthropic_call_truncated_raises_budget(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: _anthropic_resp("partial", stop="max_tokens"))
    with pytest.raises(BudgetError, match="max_tokens"):
        clients.anthropic_call(MODEL, ANTHROPIC_EP, "sys", "usr", {})


def test_anthropic_call_refusal_raises_protocol(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: {"content": [], "stop_reason": "refusal",
                                         "stop_details": {"category": "cyber"}})
    with pytest.raises(ProtocolError, match="refusal"):
        clients.anthropic_call(MODEL, ANTHROPIC_EP, "sys", "usr", {})


def test_anthropic_call_empty_raises_protocol(monkeypatch):
    monkeypatch.setattr(clients, "_post_json",
                        lambda *a, **k: _anthropic_resp(""))
    with pytest.raises(ProtocolError, match="empty"):
        clients.anthropic_call(MODEL, ANTHROPIC_EP, "sys", "usr", {})


def test_anthropic_call_requires_api_key():
    no_key = ResolvedEndpoint(model_type="anthropic", protocol="anthropic",
                              base_url="https://api.anthropic.com", api_key=None)
    with pytest.raises(ConfigError, match="api_key_env"):
        clients.anthropic_call(MODEL, no_key, "sys", "usr", {})
