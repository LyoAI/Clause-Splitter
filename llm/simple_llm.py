from langchain_openai import ChatOpenAI
import os


# ── Backwards-compatible helper ──────────────────────────────────────────
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "qwen3-max")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_llm_cache = {}

def get_llm(config: dict | None = None) -> ChatOpenAI:
    """
    Get an LLM instance.

    ``config["configurable"]["llm"]`` can inject a mock object (for testing).

    Args:
        config: Optional runtime configuration dict.

    Returns:
        A ChatOpenAI instance.
    """
    if config:
        configurable = config.get("configurable", {})
        llm = configurable.get("llm")
        if llm is not None:
            return llm

    model_name = DEFAULT_MODEL
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    # Cache by (model, key) so a changed key produces a fresh instance
    cache_key = (model_name, api_key)
    if cache_key not in _llm_cache:
        _llm_cache[cache_key] = ChatOpenAI(
            model=model_name,
            base_url=DASHSCOPE_BASE_URL,
            api_key=api_key,
        )
    return _llm_cache[cache_key]