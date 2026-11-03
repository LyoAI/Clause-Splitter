"""LLM facade + factory, configured by per-model YAML.

``LLM(model_name, provider_type)`` builds the provider section from
``config/llm/<model_name>.yaml``. Callers depend only on the facade, never a
provider directly. Provider calls use keyword arguments because signatures
differ between providers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from utils import get_logger
from .provider import LLMProvider, LLMResponse, OpenAICompatProvider

logger = get_logger()

#: ``${VAR}`` placeholder — secrets never go in the yaml; they resolve from env.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with environment values.

    Lets ``config/llm/*.yaml`` stay secret-free: ``api_key: ${DASHSCOPE_API_KEY}``
    resolves at load time instead of committing a credential.
    """
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class LLM:
    """Provider-agnostic LLM facade the agent invokes.

    Attributes:
        provider: The wrapped concrete provider.
        model: The model id sent to the API (the provider section's
            ``model_name``, falling back to the config file's name).
    """

    def __init__(
        self, model_name: str, provider_type: str, *, config_dir: Path, timeout_s: float
    ) -> None:
        cfg = _load_model_config(model_name, config_dir)
        section = cfg.get(provider_type) or {}
        if not section:
            raise ValueError(
                f"no '{provider_type}' section in {_config_path(model_name, config_dir)}; "
                f"have: {', '.join(cfg) or 'none'}"
            )
        self.model = section.get("model_name") or model_name
        self.provider = _build_provider(provider_type, section, self.model, timeout_s)

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Synchronous chat completion; ``model`` overridable via keyword."""
        return self.provider.invoke(
            messages, tools=tools, model=kwargs.pop("model", self.model), **kwargs
        )

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Asynchronous chat completion; ``model`` overridable via keyword."""
        return await self.provider.ainvoke(
            messages, tools=tools, model=kwargs.pop("model", self.model), **kwargs
        )

    def get_default_model(self) -> str:
        return self.model


def get_llm(
    model_name: str, provider_type: str, *, config_dir: Path, timeout_s: float
) -> LLM:
    """Build the LLM facade for ``model_name`` from ``<config_dir>/<model_name>.yaml``."""
    return LLM(model_name, provider_type, config_dir=config_dir, timeout_s=timeout_s)


def _config_path(model_name: str, config_dir: Path) -> Path:
    # RUN_ENV ("stg"/"prd"...) routes to <model>-<RUN_ENV>.yaml; empty = <model>.yaml.
    run_env = os.getenv("RUN_ENV", "").strip().lower()
    stem = f"{model_name}-{run_env}" if run_env else model_name
    return config_dir / f"{stem}.yaml"


def _load_model_config(model_name: str, config_dir: Path) -> dict:
    """Load ``<config_dir>/<model_name>.yaml``; raises with the available models."""
    path = _config_path(model_name, config_dir)
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in config_dir.glob("*.yaml")))
        raise FileNotFoundError(
            f"no LLM config for {model_name!r} at {path} (have: {available or 'none'})"
        )
    with path.open(encoding="utf-8") as f:
        return _expand_env(yaml.safe_load(f) or {})


def _build_provider(
    provider_type: str, section: dict, default_model: str, timeout_s: float
) -> LLMProvider:
    if provider_type == "pingan":
        return _pingan_provider(section, default_model, timeout_s)
    # openai (or any other type a yaml defines): OpenAI-compatible.
    return OpenAICompatProvider(
        api_key=section.get("api_key", ""),
        api_base=section.get("api_base", ""),
        default_model=default_model,
        timeout_s=timeout_s,
    )


def _pingan_provider(section: dict, default_model: str, timeout_s: float) -> LLMProvider:
    #: PingAn intranet gateway creds read from the yaml's ``pingan`` section.
    _PINGAN_CRED_KEYS = (
        "appKey",
        "appSecret",
        "rsaPrivateKey",
        "openApiCredential",
        "sceneId",
        "requestId",
        "modelName",
    )
    # Guard fires before the lazy import, so a missing sceneId raises a clear
    # error instead of a cryptic ``int('')`` ValueError from the provider.
    if not section.get("sceneId"):
        raise RuntimeError(
            "pingan provider requires 'sceneId' in the config's pingan section"
        )
    from .provider import PingAnLLMProvider
    return PingAnLLMProvider(
        api_key=section.get("api_key", ""),
        api_base=section.get("api_base", "http://localhost:8000/v1"),
        default_model=default_model,
        extra_headers={key: section[key] for key in _PINGAN_CRED_KEYS if section.get(key)},
        timeout_s=timeout_s,
    )
