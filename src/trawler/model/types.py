from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class DecoderConfig:
    name: str
    repo_name: str
    format: dict | None = None
    description: str | None = None


@dataclass(frozen=True)
class EncoderConfig:
    name: str
    repo_name: str
    dim: int
    format: dict | None = None
    description: str | None = None


@dataclass(frozen=True)
class ResolvedEndpoint:
    model_type: str
    protocol: str
    base_url: str
    api_key: str | None = None
