import yaml
from pathlib import Path
from typing import Any, Dict, Optional, Type
from .base import LLMProvider, LLMResponse, ToolCallRequest
from .pingan import PingAnLLMProvider

class LLM:
    """
    Unified interface for interacting with various LLM providers.
    """
    def __init__(self, provider_type = "pingan", config_path: Path = Path("config/llm/model.yaml")):
        """
        Initialize the LLM with the specified configuration file.
        :param provider_type: The type of LLM provider to use (e.g., "pingan").
        :param config_path: Path to the YAML configuration file containing provider settings.
        """
        self.config_path = config_path
        self.provider_type = provider_type

        # Load Configuration
        self.config = self._load_config()

        # Initialize the provider based on the configuration
        init_kwargs = self._build_init_kwargs(self.config)
        self.provider: LLMProvider = self._initialize_provider(**init_kwargs)

        # store default model if specified in config
        self.model_name = init_kwargs.get("extra_headers", {}).get("modelName", "")
        self.default_model = self.config.get("default_model", self.model_name)

    def _initialize_provider(self, **kwargs) -> LLMProvider:
        """
        Initialize the LLM provider based on the specified type and configuration.
        :param kwargs: Keyword arguments to initialize the provider.
        :return: An instance of the initialized LLM provider.
        """
        if self.provider_type == "pingan":
            return PingAnLLMProvider(**kwargs)
        else:
            raise ValueError(f"Unsupported LLM provider type: {self.provider_type}")
    
    def _load_config(self) -> Dict[str, Any]:
        """
        Load the LLM provider configuration from the YAML file.
        :return: A dictionary containing the provider configuration.
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"LLM configuration file not found: {self.config_path}")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing LLM configuration file: {e}")
    
    def _build_init_kwargs(self, provider_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build the initialization keyword arguments for the LLM provider based on the configuration.
        :param provider_config: A dictionary containing the provider-specific configuration.
        :return: A dictionary of keyword arguments to initialize the LLM provider.
        """
        # common parameters
        api_key = provider_config.get("api_key", "token-abc123")
        api_base = provider_config.get("api_base", "https://localhost:8000/v1")

        # Extra headers specific to the provider
        extra_headers = provider_config.get("extra_headers", {})

        return {
            "api_key": api_key,
            "api_base": api_base,
            "extra_headers": extra_headers
        }

    def invoke(
            self, 
            messages: list[dict[str, Any]], 
            tools: Optional[list[dict[str, Any]]] = None,
            model: Optional[str] = None,
            max_completion_tokens: int = 4096,
            max_context_len: int = 2000000,
            temperature: float = 0.7,
            top_p: float = 1.0,
            enable_thinking: bool = False,
            timeout: Optional[float] = None,
            stream: bool = False
        ) -> LLMResponse:
        """
        Invoke the LLM provider with the given messages and optional model.
        :param messages: A list of message dictionaries to send to the LLM.
        :param tools: An optional list of tools to use for this invocation.
        :param model: An optional model name to use for this invocation.
        :param max_completion_tokens: The maximum number of tokens to generate in the completion.
        :param max_context_len: The maximum number of tokens in the context.
        :param temperature: The temperature for sampling.
        :param top_p: The top-p value for sampling.
        :param enable_thinking: Whether to enable thinking steps.
        :param timeout: The timeout for the request.
        :param stream: Whether to stream the response.
        :return: An LLMResponse containing the response from the provider.
        """
        return self.provider.invoke(
            messages=messages, 
            tools=tools, 
            model=model, 
            max_completion_tokens=max_completion_tokens, 
            max_context_len=max_context_len, 
            temperature=temperature, 
            top_p=top_p, 
            enable_thinking=enable_thinking, 
            timeout=timeout, 
            stream=stream
        )

    async def ainvoke(
            self, 
            messages: list[dict[str, Any]], 
            tools: Optional[list[dict[str, Any]]] = None,
            model: Optional[str] = None,
            max_completion_tokens: int = 4096,
            max_context_len: int = 2000000,
            temperature: float = 0.7,
            top_p: float = 1.0,
            enable_thinking: bool = False,
            timeout: Optional[float] = None,
            stream: bool = False
        ) -> LLMResponse:
        """
        Asynchronously invoke the LLM provider with the given messages and optional model.
        :param messages: A list of message dictionaries to send to the LLM.
        :param tools: An optional list of tools to use for this invocation.
        :param model: An optional model name to use for this invocation.
        :param max_completion_tokens: The maximum number of tokens to generate in the completion.
        :param max_context_len: The maximum number of tokens in the context.
        :param temperature: The temperature for sampling.
        :param top_p: The top-p value for sampling.
        :param enable_thinking: Whether to enable thinking steps.
        :param timeout: The timeout for the request.
        :param stream: Whether to stream the response.
        :return: An LLMResponse containing the response from the provider.
        """
        return await self.provider.ainvoke(
            messages=messages, 
            tools=tools, 
            model=model, 
            max_completion_tokens=max_completion_tokens, 
            max_context_len=max_context_len, 
            temperature=temperature, 
            top_p=top_p, 
            enable_thinking=enable_thinking, 
            timeout=timeout, 
            stream=stream
        )

    def get_default_model(self) -> str:
        """
        Get the default model for this LLM provider.
        :return: The default model name as a string.
        """
        return self.default_model
    

if __name__ == "__main__":
    from utils import get_logger
    logger = get_logger()
    llm = LLM()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
    response = llm.invoke(messages)
    logger.info(response)