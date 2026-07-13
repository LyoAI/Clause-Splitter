import os
import time
import re
import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Literal, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import models
from tqdm import tqdm
from llm.providers import LLM, LLMResponse
from .base import LLMServingABC
from utils import get_logger


class PingAnLLMServing(LLMServingABC):
    """
    PingAnLLM-based serving class that provides unified interface for multiple LLM providers.
    """
    def start_serving(self) -> None:
        self.logger.info("PingAnLLMServing: no local service to start.")
        return
    
    def __init__(
            self,
            config_path: Optional[Union[str, Path]] = None,
            validate_on_init: bool = True,
            max_workers: int = 10,
            max_retries: int = 5,
            api_version: Optional[str] = None,
            temperature: float = 0.7,
            max_completion_tokens: Optional[int] = 4096,
            max_context_len: Optional[int] = 32000,
            top_p: float = 1.0,
            enable_thinking: bool = False,
            timeout: int = 60,
            stream: Optional[bool] = False,
            **kwargs: Any
        ):
        """
        Initialize PingAnLLM serving instance.
        
        Args:
            validate_on_init: Whether to validate the model and API configuration on initialization
            max_workers: Number of concurrent workers for batch processing
            max_retries: Number of LLM inference retry chances for each input
            api_version: API version for providers that support it
            temperature: Sampling temperature
            max_completion_tokens: Maximum tokens to generate in the completion
            max_context_len: Maximum tokens in the context (prompt + response)
            top_p: Top-p sampling parameter
            timeout: Request timeout in seconds
            **kwargs: Additional parameters passed to PingAnLLM.completion()
            
        Note:
            All configuration parameters are immutable after initialization.
            If you need different settings, create a new instance.
        """
        self.config_path = Path(config_path) if isinstance(config_path, str) else config_path
        self.api_version = api_version
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self.max_context_len = max_context_len
        self.max_retries = max_retries
        self.top_p = top_p
        self.max_workers = max_workers
        self.enable_thinking = enable_thinking
        self.timeout = timeout
        self.stream = stream
        self.kwargs = kwargs
        self.logger = get_logger()
        
        # initialize llm server
        self.llm = LLM(provider_type="pingan", config_path=self.config_path)
        self.model_name = self.llm.model_name

        if validate_on_init:
            self._validate_setup()
        
        self.logger.info(f"Successfully initialized with model: {self.model_name}")
    
    def switch_model(self, 
                     model_name: str,
                     key_name_of_api_key: Optional[str] = None,
                     api_url: Optional[str] = None,
                     api_version: Optional[str] = None,
                     **kwargs: Any):
        """
        Switch to a different model with potentially different API configuration.
        
        Args:
            model_name: Model name to switch to
            key_name_of_api_key: New environment variable name for API key (optional)
            api_url: New API base URL (optional)
            api_version: New API version (optional)
            **kwargs: Additional parameters for the new model
        """
        # Update model
        self.model_name = model_name
        pass

    
    def format_response(self, response: Union[LLMResponse, Any]) -> str:
        """
        Format PingAnLLM response to include reasoning content in a structured format.
        
        This method handles the standardized PingAnLLM response format and extracts
        both the main content and any reasoning_content if available.
        
        Args:
            response: The response object from PingAnLLM
            
        Returns:
            Formatted string with think/answer tags if reasoning is present,
            otherwise just the content
        """
        try:
            if isinstance(response, LLMResponse):
                # Extract content and reasoning_content from the response
                content = response.content
                reasoning_content = response.reasoning_content
            else:
                content = response.choices[0].message.content
                reasoning_content = response.choices[0].message.reasoning_content
            
            if not reasoning_content:
                # Try to extract reasoning content from the content if not explicitly provided
                reasoning_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if reasoning_match:
                    reasoning_content = reasoning_match.group(1).strip()
                    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                else:
                    reasoning_content = ""
                
                # Format the response based on whether reasoning content exists
            if reasoning_content:
                return f"<think>{reasoning_content}</think>\n<answer>{content}</answer>"
            else:
                return content
        except Exception as e:
            self.logger.error(f"Error formatting response: {e}")
            return getattr(response, 'content', str(response))
        
    
    def _validate_setup(self):
        """Validate the model and API configuration."""
        try:
            self.llm.invoke(
                messages=[{"role": "user", "content": "Hi"}],
                max_completion_tokens=1
            )
            self.logger.success("PingAnLLM setup validation successful")
        except Exception as e:
            self.logger.error(f"PingAnLLM setup validation failed: {e}")
            raise ValueError(f"Failed to validate PingAnLLM setup: {e}")
    
    def _generate_single(self, user_input: str, system_prompt: str, tools: Optional[list[dict[str, Any]]] = None) -> str:
        """Generate response for a single input with retry logic.
        
        Args:
            user_input: User input text
            system_prompt: System prompt
            tools: List of tools available for use
        Returns:
            Generated response string
            
        Raises:
            Exception: If generation fails after all retries
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]
        
        # Prepare completion parameters
        completion_params = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "enable_thinking": self.enable_thinking,
            "max_completion_tokens": self.max_completion_tokens,
            "max_context_len": self.max_context_len,
            "timeout": self.timeout,
            "stream": self.stream,
            **self.kwargs
        }

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self.llm.invoke(**completion_params)
                return self.format_response(response)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    # Check if error is retryable
                    error_str = str(e).lower()
                    if any(retryable in error_str for retryable in 
                           ["rate limit", "timeout", "connection", "503", "502", "429"]):
                        wait_time = min(2 ** attempt, 10)  # Exponential backoff with max 10s
                        self.logger.warning(f"Retryable error, waiting {wait_time}s: {e}")
                        time.sleep(wait_time)
                        continue
                    
                # Non-retryable error or last attempt
                self.logger.error(f"Error generating response (attempt {attempt + 1}/{self.max_retries}): {e}")
                break
        
        # Raise the last error instead of returning error string
        raise last_error
    
    def generate_from_input(
        self, 
        user_inputs: List[str], 
        system_prompt: str = "You are a helpful assistant",
        tools: Optional[list[dict[str, Any]]] = None,
        json_schema: dict = None,
        *args, **kwargs
    ) -> List[str]:
        """
        Generate responses for a list of inputs using concurrent processing.
        
        Args:
            user_inputs: List of user input strings
            system_prompt: System prompt to use for all generations
            
        Returns:
            List of generated responses
        """
        if not user_inputs:
            return []
        
        if isinstance(user_inputs, str):
            user_inputs = [user_inputs]

        # Single input case
        if len(user_inputs) == 1:
            try:
                return [self._generate_single(user_inputs[0], system_prompt, tools)]
            except Exception as e:
                # For consistency with batch processing, return error message in list
                error_msg = f"Error: {str(e)}"
                self.logger.error(f"Failed to generate response: {e}")
                return [error_msg]
        
        # Batch processing with threading
        responses = [None] * len(user_inputs)
        
        def generate_with_index(idx: int, user_input: str) -> Tuple[int, str]:
            try:
                response = self._generate_single(user_input, system_prompt, tools)
                return idx, response
            except Exception as e:
                # For batch processing, return error message to maintain list structure
                error_msg = f"Error: {str(e)}"
                self.logger.error(f"Failed to generate response for input {idx}: {e}")
                return idx, error_msg
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(generate_with_index, idx, user_input)
                for idx, user_input in enumerate(user_inputs)
            ]
            
            for future in tqdm(as_completed(futures), total=len(futures), desc=f"Generating with Max Concurrency: {self.max_workers}"):
                idx, response = future.result()
                responses[idx] = response
        
        return responses

    async def _generate_single_async(
            self, 
            user_input: str, 
            system_prompt: str, 
            tools: Optional[list[dict[str, Any]]] = None
        ) -> str:
        """Generate response for a single input with retry logic.
        
        Args:
            user_input: User input text
            system_prompt: System prompt
            tools: List of tools available for use
        Returns:
            Generated response string
            
        Raises:
            Exception: If generation fails after all retries
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]
        
        # Prepare completion parameters
        completion_params = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "enable_thinking": self.enable_thinking,
            "max_completion_tokens": self.max_completion_tokens,
            "max_context_len": self.max_context_len,
            "timeout": self.timeout,
            "stream": self.stream,
            **self.kwargs
        }

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = await self.llm.ainvoke(**completion_params)
                return self.format_response(response)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    # Check if error is retryable
                    error_str = str(e).lower()
                    if any(retryable in error_str for retryable in 
                           ["rate limit", "timeout", "connection", "503", "502", "429"]):
                        wait_time = min(2 ** attempt, 10)  # Exponential backoff with max 10s
                        self.logger.warning(f"Retryable error, waiting {wait_time}s: {e}")
                        time.sleep(wait_time)
                        continue
                    
                # Non-retryable error or last attempt
                self.logger.error(f"Error generating response (attempt {attempt + 1}/{self.max_retries}): {e}")
                break
        
        # Raise the last error instead of returning error string
        raise last_error
    
    async def generate_from_input_async(
        self, 
        user_inputs: List[str], 
        system_prompt: str = "You are a helpful assistant",
        tools: Optional[list[dict[str, Any]]] = None,
        json_schema: dict = None,
        *args, **kwargs
    ) -> List[str]:
        """
        Asynchronously generate responses for a list of inputs using concurrent processing.
        
        Args:
            user_inputs: List of user input strings
            system_prompt: System prompt to use for all generations
            tools: List of tools available for use
            json_schema: JSON schema for response validation
        Returns:
            List of generated response strings
        """
        if not user_inputs:
            return []

        if isinstance(user_inputs, str):
            user_inputs = [user_inputs]
        
        # single input case
        if len(user_inputs) == 1:
            try:
                return [await self._generate_single_async(user_inputs[0], system_prompt, tools)]
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                self.logger.error(f"Failed to generate response: {e}")
                return [error_msg]
        
        semaphore = asyncio.Semaphore(self.max_workers)

        async def run_task(idx: int, user_input: str) -> Tuple[int, str]:
            async with semaphore:
                try:
                    response = await self._generate_single_async(user_input, system_prompt, tools)
                    return idx, response
                except Exception as e:
                    error_msg = f"Error: {str(e)}"
                    self.logger.error(f"Failed to generate response for input {idx}: {e}")
                    return idx, error_msg

        tasks = [
            run_task(idx, user_input) 
            for idx, user_input in enumerate(user_inputs)
        ]

        try:
            from tqdm.asyncio import tqdm_asyncio
            results = await tqdm_asyncio.gather(*tasks, total=len(tasks), desc=f"Generating with Max Concurrency: {self.max_workers}")
        except ImportError:
            self.logger.warning("tqdm.asyncio not available, proceeding without progress bar.")
            results = await asyncio.gather(*tasks)
        
        responses = [None] * len(user_inputs)
        for idx, response in results:
            responses[idx] = response
        
        return responses

    def get_supported_models(self) -> List[str]:
        pass
    

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.logger.info("Cleaning up PingAnLLMServing resources")
        # PingAnLLM doesn't require explicit cleanup since we don't use global state
        # Instance variables will be garbage collected when the instance is destroyed
        # Clear any references to ensure proper cleanup
        self.kwargs = None

if __name__ == "__main__":
    input = 'hi'
    serving = PingAnLLMServing(config_path="path/to/config.json")
    response = serving.generate_from_input(input)
    