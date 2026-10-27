"""LLM facade — provider selection from ``config/llm/<model>.yaml``."""

from __future__ import annotations

import pytest

from clause_splitter.llm import llm_registry
from clause_splitter.llm.llm_registry import get_llm
from clause_splitter.llm.provider import OpenAICompatProvider
from clause_splitter.llm.provider.pingan import PingAnLLMProvider

OPENAI = """
openai:
  api_key: test
  api_base: http://llm.local/v1
  model_name: model-a
"""

PINGAN = """
pingan:
  api_key: unused
  api_base: http://gateway.local/v1
  model_name: qwen3.8-27b
  appKey: ak
  appSecret: as
  rsaPrivateKey: "00"
  openApiCredential: cred
  sceneId: "42"
  requestId: rid
"""


def _use_config(tmp_path, monkeypatch, body: str) -> None:
    (tmp_path / "m.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(llm_registry, "LLM_CONFIG_DIR", tmp_path)


def test_openai_section_is_the_default(tmp_path, monkeypatch):
    _use_config(tmp_path, monkeypatch, OPENAI)
    llm = get_llm("m")
    assert isinstance(llm.provider, OpenAICompatProvider)
    assert llm.model == "model-a"


def test_pingan_section_builds_signed_provider(tmp_path, monkeypatch):
    _use_config(tmp_path, monkeypatch, PINGAN)
    llm = get_llm("m", "pingan")
    assert isinstance(llm.provider, PingAnLLMProvider)
    assert llm.model == "qwen3.8-27b"
    assert llm.provider.scene_id == 42


def test_pingan_requires_scene_id(tmp_path, monkeypatch):
    _use_config(tmp_path, monkeypatch, "pingan:\n  api_base: http://gw/v1\n")
    with pytest.raises(RuntimeError, match="sceneId"):
        get_llm("m", "pingan")


def test_runner_uses_configured_provider(monkeypatch):
    from clause_splitter import config
    from clause_splitter.jobs import JobRunner

    monkeypatch.setattr(config, "LLM_PROVIDER", "pingan")
    monkeypatch.setattr(config.get_settings(), "default_model", "m")
    seen: dict = {}
    monkeypatch.setattr(
        "clause_splitter.llm.get_llm",
        lambda name, provider_type="openai": seen.update(name=name, provider_type=provider_type),
    )
    JobRunner._default_llm()
    assert seen == {"name": "m", "provider_type": "pingan"}


def test_settings_model_comes_from_llm_model_name():
    from clause_splitter.config import LLM_MODEL_NAME, Settings

    assert Settings().default_model == LLM_MODEL_NAME
