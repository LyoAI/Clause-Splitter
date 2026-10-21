"""LLM facade + factory, configured by per-model YAML.

``LLM(model_name, provider_type)`` reads ``config/llm/<model_name>.yaml`` and
builds the provider under the ``provider_type`` key (``openai:`` / ``pingan:``).
The agent talks only to ``LLM`` — never a provider directly — so the transport
is swappable behind the shared ``LLMResponse`` interface. ``get_llm()`` is the
thin factory equivalent of the constructor.

Every provider call uses keyword arguments. Provider signatures legitimately
differ — PingAn inserts ``max_context_len`` between the ABC's
``max_completion_tokens`` and ``temperature`` — so positional binding would
mis-route values; keywords make the facade immune to that.

Config values may reference the environment as ``${VAR}``, so the YAML files
themselves carry no credentials.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from chillrag.config import LLM_CONFIG_DIR
from chillrag.utils import get_logger
from .provider import LLMProvider, LLMResponse, OpenAICompatProvider

logger = get_logger()


class LLM:
    """Provider-agnostic LLM facade the agent invokes.

    Attributes:
        provider: The wrapped concrete provider.
        model: The model id sent to the API (the provider section's
            ``model_name``, falling back to the config file's name).
    """

    def __init__(self, model_name: str, provider_type: str = "openai") -> None:
        cfg = _load_model_config(model_name)
        section = cfg.get(provider_type) or {}
        if not section:
            logger.error(
                "no %r section in %s (have: %s)",
                provider_type, _config_path(model_name), ", ".join(cfg) or "none",
            )
            raise ValueError(
                f"no '{provider_type}' section in {_config_path(model_name)}; "
                f"have: {', '.join(cfg) or 'none'}"
            )
        self.model = section.get("model_name") or model_name
        self.provider = _build_provider(provider_type, section, self.model)
        logger.info("llm ready: %s via %s (model=%s)", model_name, provider_type, self.model)

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

    async def astream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        """Async-generate streamed completion chunks; ``model`` overridable."""
        async for chunk in self.provider.astream(
            messages, tools=tools, model=kwargs.pop("model", self.model), **kwargs
        ):
            yield chunk

    def get_default_model(self) -> str:
        return self.model


def get_llm(model_name: str, provider_type: str = "openai") -> LLM:
    """Build the LLM facade for ``model_name`` from ``config/llm/<model_name>.yaml``."""
    return LLM(model_name, provider_type)


def _config_path(model_name: str) -> Path:
    return LLM_CONFIG_DIR / f"{model_name}.yaml"


#: ``${VAR}`` / ``$VAR`` references inside a config value.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _load_model_config(model_name: str) -> dict:
    """Load ``config/llm/<model_name>.yaml``; raises with the available models.

    String values are env-expanded (``${VAR}``), so a config file can be
    committed while the credential it names stays in the environment. An
    unresolved reference raises rather than silently yielding an empty key,
    which would otherwise surface as a confusing auth error at call time.
    """
    path = _config_path(model_name)
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in LLM_CONFIG_DIR.glob("*.yaml")))
        logger.error("no LLM config for %r at %s (have: %s)", model_name, path, available or "none")
        raise FileNotFoundError(
            f"no LLM config for {model_name!r} at {path} (have: {available or 'none'})"
        )
    with path.open(encoding="utf-8") as f:
        return _expand_env(yaml.safe_load(f) or {}, path)


def _expand_env(value: Any, path: Path) -> Any:
    """Recursively expand ``${VAR}`` in every string of a loaded config."""
    if isinstance(value, dict):
        return {k: _expand_env(v, path) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, path) for v in value]
    if not isinstance(value, str) or "$" not in value:
        return value

    def _sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            logger.error("%s references $%s, which is not set", path, name)
            raise RuntimeError(f"{path}: ${{{name}}} is not set in the environment")
        return os.environ[name]

    return _ENV_REF.sub(_sub, value)


def _build_provider(
    provider_type: str, section: dict, default_model: str
) -> LLMProvider:
    if provider_type == "pingan":
        return _pingan_provider(section, default_model)
    # openai (or any other type a yaml defines): OpenAI-compatible.
    return OpenAICompatProvider(
        api_key=section.get("api_key", ""),
        api_base=section.get("api_base", ""),
        default_model=default_model,
    )


def _pingan_provider(section: dict, default_model: str) -> LLMProvider:
    #: PingAn intranet gateway creds read from the yaml's ``pingan`` section.
    _PINGAN_CRED_KEYS = (
        "appKey",
        "appSecret",
        "rsaPrivateKey",
        "openApiCredential",
        "sceneId",
        "requestId",
        "model_name",
    )
    # Guard fires before the lazy import, so a missing sceneId raises a clear
    # error instead of a cryptic ``int('')`` ValueError from the provider.
    if not section.get("sceneId"):
        logger.error("pingan section has no sceneId")
        raise RuntimeError(
            "pingan provider requires 'sceneId' in the config's pingan section"
        )
    from .provider import PingAnLLMProvider
    return PingAnLLMProvider(
        api_key=section.get("api_key", ""),
        api_base=section.get("api_base", "http://localhost:8000/v1"),
        default_model=default_model,
        extra_headers={key: section[key] for key in _PINGAN_CRED_KEYS if section.get(key)},
    )
