"""Categorized errors. Helps tell `max_tokens` problems from server outages
from prompt bugs at a glance — each row's `error_category` col == one of
these class names.
"""
from __future__ import annotations


class RowInferError(Exception):
    """Base. Don't catch this directly; prefer subclasses."""


class ConfigError(RowInferError):
    """Setup / cfg-table / env-var problem. Setters raise this early.
    Examples: missing env var, unknown cfg.decoder name, expected_output mismatch.
    """


class EndpointError(RowInferError):
    """Network / server transport problem (retryable).
    Examples: connection refused, timeout, HTTP 5xx, DNS fail.
    """


class ProtocolError(RowInferError):
    """API contract violated (likely a config or model issue, not transport).
    Examples: HTTP 4xx with body, malformed response shape, empty content
    without obvious cause.
    """


class BudgetError(RowInferError):
    """LLM hit max_tokens before producing usable output.
    Examples: reasoning model consumed all tokens reasoning;
    finish_reason='length' with empty/truncated content.
    """


class ParseError(RowInferError):
    """Step output couldn't be parsed by post_step (e.g. expected JSON,
    got prose). Prompt/model issue, not transport.
    """
