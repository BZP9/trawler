"""Name validation in cfg upserts — invalid names must fail before any DB
connection is attempted (no ROWINFER_DSN needed in these tests).
"""
import pytest

from trawler import cfg


BAD_NAMES = [
    "has space",
    'quo"te',
    "semi;colon",
    "dot.ted",          # dot breaks schema.table parsing downstream
    "",
    "x" * 64,           # over Postgres 63-byte identifier limit
    "drop table--",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_upsert_system_prompt_rejects_bad_name(name):
    with pytest.raises(ValueError, match="invalid cfg name"):
        cfg.upsert_system_prompt(name, content="x", expected_output="t")


@pytest.mark.parametrize("name", BAD_NAMES)
def test_upsert_decoder_rejects_bad_name(name):
    with pytest.raises(ValueError, match="invalid cfg name"):
        cfg.upsert_decoder(name, repo_name="r")


@pytest.mark.parametrize("name", BAD_NAMES)
def test_upsert_encoder_rejects_bad_name(name):
    with pytest.raises(ValueError, match="invalid cfg name"):
        cfg.upsert_encoder(name, repo_name="r", dim=8)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_upsert_model_type_rejects_bad_name(name):
    with pytest.raises(ValueError, match="invalid cfg name"):
        cfg.upsert_model_type(name, protocol="openai")


@pytest.mark.parametrize("name", ["ok_name", "bge-m3", "Gemma4_31B", "a"])
def test_good_names_pass_validation(name):
    # validation itself must accept these (DB call comes after; not exercised)
    cfg._check_name(name)
