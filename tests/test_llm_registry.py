"""LLM facade — provider selection from ``<config_dir>/<model>.yaml``."""

from __future__ import annotations

import pytest

from llm import get_llm
from llm.provider import OpenAICompatProvider
from llm.provider.pingan import PingAnLLMProvider

OPENAI = """
openai:
  api_key: test
  api_base: http://llm.local/v1
"""

PINGAN_NO_SCENE = """
pingan:
  api_key: test
  api_base: http://llm.local/v1
  appKey: k
"""

PINGAN_OK = PINGAN_NO_SCENE + "  sceneId: 1\n"


def _cfg(tmp_path, name: str, body: str):
    (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_openai_section_builds_compat_provider(tmp_path):
    cfg = _cfg(tmp_path, "m1", OPENAI)
    llm = get_llm("m1", "openai", config_dir=cfg, timeout_s=5)
    assert isinstance(llm.provider, OpenAICompatProvider)


def test_pingan_section_builds_signed_provider(tmp_path):
    cfg = _cfg(tmp_path, "m2", PINGAN_OK)
    llm = get_llm("m2", "pingan", config_dir=cfg, timeout_s=5)
    assert isinstance(llm.provider, PingAnLLMProvider)


def test_pingan_requires_scene_id(tmp_path):
    cfg = _cfg(tmp_path, "m3", PINGAN_NO_SCENE)
    with pytest.raises(RuntimeError, match="sceneId"):
        get_llm("m3", "pingan", config_dir=cfg, timeout_s=5)


def test_missing_model_file_lists_available_models(tmp_path):
    cfg = _cfg(tmp_path, "present", OPENAI)
    with pytest.raises(FileNotFoundError, match="present"):
        get_llm("absent", "openai", config_dir=cfg, timeout_s=5)


def test_missing_provider_section_is_a_clear_error(tmp_path):
    cfg = _cfg(tmp_path, "m4", OPENAI)
    with pytest.raises(ValueError, match="pingan"):
        get_llm("m4", "pingan", config_dir=cfg, timeout_s=5)


def test_run_env_routes_to_suffixed_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_ENV", "stg")
    cfg = _cfg(tmp_path, "m1-stg", OPENAI)
    llm = get_llm("m1", "openai", config_dir=cfg, timeout_s=5)
    assert isinstance(llm.provider, OpenAICompatProvider)
