"""Unit tests for trawler.inspect helpers (no DB required)."""
import pytest

from trawler.inspect import _derive_out_table, _log_table, _VALID_STATUSES


def test_derive_out_table_live_run():
    row = {"config": {"system_prompt": {"name": "extract_skills"}}}
    assert _derive_out_table(row, "gen") == 'gen."extract_skills"'


def test_derive_out_table_offload_with_prompt_key():
    # bundle() now writes config["prompt"] alongside the offload markers
    row = {
        "name": "extract_skills-20260713T081500Z",
        "config": {"job_id": "extract_skills-20260713T081500Z",
                   "offload": True, "prompt": "extract_skills",
                   "stage": "exported", "model_type": "remote_ollama"},
    }
    assert _derive_out_table(row, "gen") == 'gen."extract_skills"'


def test_derive_out_table_offload_legacy_no_prompt_key():
    # pre-fix bundles: recover the prompt by stripping the job-id timestamp
    row = {
        "name": "extract_skills-20260713T081500Z",
        "config": {"job_id": "extract_skills-20260713T081500Z",
                   "offload": True, "stage": "exported",
                   "model_type": "remote_ollama"},
    }
    assert _derive_out_table(row, "gen") == 'gen."extract_skills"'


def test_derive_out_table_unknown_config_raises():
    with pytest.raises(ValueError, match="cannot derive out table"):
        _derive_out_table({"run_id": "x", "config": {}}, "gen")


def test_derive_out_table_enc_uses_model():
    row = {"model": "bge-m3"}
    assert _derive_out_table(row, "enc") == 'enc."bge-m3"'


def test_exported_is_valid_status():
    assert "exported" in _VALID_STATUSES


def test_log_table_rejects_unknown_schema():
    with pytest.raises(ValueError):
        _log_table("nope")
