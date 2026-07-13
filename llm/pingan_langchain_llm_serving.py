from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from urllib.parse import urlencode
from utils import get_logger
from tqdm import tqdm
from pathlib import Path
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
import base64
import re
import hmac
import binascii
import requests
import json
import json_repair
import time
import asyncio
import aiohttp
import yaml
from langchain_core.messages import AIMessage
from concurrent.futures import ThreadPoolExecutor, as_completed


# 生成日志记录器
logger = get_logger()

class Pingan_LangchainLLMServing(ChatOpenAI):
    config_path: Path | None = Field(default=None, alias="model_config_path")
    app_key: Optional[str] = Field(default=None, description="大模型平台应用appKey")
    app_secret: Optional[str] = Field(default=None, description="大模型平台应用appsecret")
    rsa_private_key: Optional[str] = Field(default=None, description="网关eagw上获取的凭据密钥")
    openapi_credential: Optional[str] = Field(default=None, description="openai_credential")
    scene_id: Optional[int] = Field(default=None, description="场景ID")
    request_id: Optional[str] = Field(default=None, description="请求ID")
    model_name: Optional[str] = Field(default=None, description="模型名称")

    _max_workers: int = 4
    _max_retries: int = 3


    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        config = self._load_config() if self.config_path else {}

        extra_headers = config.get("extraHeaders", {})
        # 大模型平台应用appKey
        self.app_key = extra_headers.pop("appKey", "")
        # 大模型平台应用appsecret
        self.app_secret = extra_headers.pop("appSecret", "")
        # 网关eagw上获取的凭据密钥
        self.rsa_private_key = extra_headers.pop("rsaPrivateKey", "")
        self.openapi_credential = extra_headers.pop("openApiCredential", "")
        self.scene_id = int(extra_headers.pop("sceneId", ""))
        self.request_id = extra_headers.pop("requestId", "")
        self.model_name = extra_headers.pop("modelName", "")

        logger.info(f"app_key = {self.app_key}, app_secret = {self.app_secret}, scene_id = {self.scene_id}, "
                    f"request_id = {self.request_id}, model_name = {self.model_name}")
        
        self._validate_setup()

    def get_gpt_sign(self, app_key: str, app_secret: str, requestTime: int) -> str:
        """生成GPT接口调用签名"""
        params = {
            "openApiRequestTime": str(requestTime),
            "appKey": app_key,
            "appSecret": app_secret
        }
        query_string = urlencode(params).lower()
        hmac_obj = hmac.new(app_secret.encode("utf-8"), query_string.encode("utf-8"), 'sha1')
        signature = base64.b64encode(hmac_obj.digest()).decode("utf-8")
        return signature

    def get_sign(self, rsaPrivateKey, requestTime):
        '''
        根据凭证密钥生成签名
        '''
        binary_key = binascii.a2b_hex(rsaPrivateKey)
        pkcs8_private_key = RSA.import_key(binary_key)
        h = SHA256.new(requestTime.encode("utf-8"))
        signer = PKCS1_v1_5.new(pkcs8_private_key)
        signature = signer.sign(h).hex().uppper()
        return signature

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

    def _get_request_payload(self, input_, *, stop = None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        requestTime = str(int(time.time() * 1000))
        get_signature = self.get_gpt_sign(self.app_key, self.app_secret, requestTime)
        eagw_sign = self.get_sign(self.rsa_private_key, requestTime)

        if "extra_headers" not in payload:
            payload["extra_headers"] = {
                "Content-Type": "application/json",
                "openApiCode": "API035059",
                "openApiCredential": self.openapi_credential,
                "openApiRequestTime": requestTime,
                "openApiSignature": eagw_sign,
                "gpt_app_key": self.app_key,
                "gpt_signature": get_signature
            }
        
        if "extra_body" not in payload or ("extra_body" in payload and "scene_id" not in payload.get("extra_body", {})):
            if "extra_body" not in payload:
                payload["extra_body"] = {}
            
            payload["extra_body"]["scene_id"] = self.scene_id
            payload["extra_body"]["request_id"] = self.request_id
        return payload

    # ── Validation ──────────────────────────────────────────────────────

    def _validate_setup(self) -> None:
        """Validate the model and API configuration by sending a simple prompt."""
        try:
            self.invoke([{"role": "user", "content": "Hi"}], max_tokens=1)
            logger.info("LLM setup validation successful")
        except Exception as e:
            logger.error("LLM setup validation failed: %s", e)
            raise ValueError(f"Failed to validate LLM setup: {e}") from e

    # ── Serving interface ───────────────────────────────────────────────

    def start_serving(self) -> None:
        """Start the serving endpoint (no-op for API-based LLM serving)."""
        logger.info("OpenAI_LangchainLLMServing: no local service to start.")

    def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up OpenAI_LangchainLLMServing resources")
        self._max_workers = 0
        self._max_retries = 0

    def switch_model(self, model_name: str, **kwargs: Any) -> None:
        """
        Switch the model.

        Note: ChatOpenAI configuration is immutable after init. This method
        updates reference info. For a truly different model configuration,
        create a new instance.

        Args:
            model_name: New model name.
            **kwargs: Additional model configuration parameters.
        """
        logger.info(
            "Switch model requested to: %s. Create a new instance with "
            "model=%s for API-level changes.", model_name, model_name
        )

    def get_supported_models(self) -> List[str]:
        """Return a list of supported model names."""
        model = getattr(self, "model_name", None) or getattr(self, "model", "")
        return [model] if model else []

    # ── Response formatting ────────────────────────────────────────────

    @staticmethod
    def format_response(response: AIMessage) -> str:
        """
        Format an AIMessage response to a string.

        Extracts content and any reasoning_content from the response.

        Args:
            response: The AIMessage from ChatOpenAI.

        Returns:
            Formatted string with <think>/<answer> tags if reasoning is present,
            otherwise just the content.
        """
        content = response.content or ""

        # Check for explicit reasoning_content
        reasoning_content = getattr(response, "reasoning_content", None)

        if not reasoning_content:
            # Try to extract reasoning from <think> tags embedded in content
            match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
            if match:
                reasoning_content = match.group(1).strip()
                content = re.sub(
                    r"<think>.*?</think>", "", content, flags=re.DOTALL
                ).strip()

        if reasoning_content:
            return f"<think>{reasoning_content}</think>\n<answer>{content}</answer>"
        return content

    # ── Single generation (sync) ─────────────────────────────────────────

    def _generate_single(
        self,
        user_input: str,
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Generate a response for a single input with retry logic.

        Args:
            user_input: The user input text.
            system_prompt: The system prompt.
            tools: Optional list of tool definitions.

        Returns:
            Generated response string.

        Raises:
            Exception: If generation fails after all retries.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                invoke_kwargs = {"tools": tools} if tools else {}
                response = self.invoke(messages, **invoke_kwargs)
                return self.format_response(response)
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    if _is_retryable(str(e)):
                        wait = min(2**attempt, 10)
                        logger.warning(
                            "Retryable error (attempt %d/%d), waiting %ds: %s",
                            attempt + 1, self._max_retries, wait, e,
                        )
                        time.sleep(wait)
                        continue

                logger.error(
                    "Error generating response (attempt %d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                break

        raise last_error  # type: ignore[misc]

    # ── Batch generation (sync) ─────────────────────────────────────────

    def generate_from_input(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
        tools: Optional[List[Dict[str, Any]]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Generate responses for a list of inputs using concurrent processing.

        Args:
            user_inputs: List of user input strings.
            system_prompt: System prompt for all generations.
            tools: Optional list of tool definitions.
            json_schema: Optional JSON schema (unused; use with_structured_output
                         for structured output).

        Returns:
            List of generated response strings.
        """
        if not user_inputs:
            return []

        if isinstance(user_inputs, str):
            user_inputs = [user_inputs]

        # Single input — no thread pool overhead
        if len(user_inputs) == 1:
            try:
                return [self._generate_single(user_inputs[0], system_prompt, tools)]
            except Exception as e:
                err = f"Error: {e}"
                logger.error("Failed to generate response: %s", e)
                return [err]

        responses: List[Optional[str]] = [None] * len(user_inputs)

        def _task(idx: int, inp: str) -> Tuple[int, str]:
            try:
                return idx, self._generate_single(inp, system_prompt, tools)
            except Exception as e:
                err = f"Error: {e}"
                logger.error("Failed to generate response for input %d: %s", idx, e)
                return idx, err

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            fut_map = {pool.submit(_task, idx, inp): idx for idx, inp in enumerate(user_inputs)}
            for fut in tqdm(
                as_completed(fut_map),
                total=len(fut_map),
                desc=f"Generating (concurrency: {self._max_workers})",
            ):
                idx, result = fut.result()
                responses[idx] = result

        return responses  # type: ignore[return-value]

    # ── Single generation (async) ───────────────────────────────────────

    async def _generate_single_async(
        self,
        user_input: str,
        system_prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Generate a response for a single input asynchronously with retry logic.

        Args:
            user_input: The user input text.
            system_prompt: The system prompt.
            tools: Optional list of tool definitions.

        Returns:
            Generated response string.

        Raises:
            Exception: If generation fails after all retries.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                invoke_kwargs = {"tools": tools} if tools else {}
                response = await self.ainvoke(messages, **invoke_kwargs)
                return self.format_response(response)
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    if _is_retryable(str(e)):
                        wait = min(2**attempt, 10)
                        logger.warning(
                            "Retryable error (attempt %d/%d), waiting %ds: %s",
                            attempt + 1, self._max_retries, wait, e,
                        )
                        await asyncio.sleep(wait)
                        continue

                logger.error(
                    "Error generating response (attempt %d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                break

        raise last_error  # type: ignore[misc]

    # ── Batch generation (async) ────────────────────────────────────────

    async def generate_from_input_async(
        self,
        user_inputs: List[str],
        system_prompt: str = "You are a helpful assistant",
        tools: Optional[List[Dict[str, Any]]] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Asynchronously generate responses for a list of inputs.

        Args:
            user_inputs: List of user input strings.
            system_prompt: System prompt for all generations.
            tools: Optional list of tool definitions.
            json_schema: Optional JSON schema (unused; use with_structured_output
                         for structured output).

        Returns:
            List of generated response strings.
        """
        if not user_inputs:
            return []

        if isinstance(user_inputs, str):
            user_inputs = [user_inputs]

        if len(user_inputs) == 1:
            try:
                return [await self._generate_single_async(user_inputs[0], system_prompt, tools)]
            except Exception as e:
                err = f"Error: {e}"
                logger.error("Failed to generate response: %s", e)
                return [err]

        semaphore = asyncio.Semaphore(self._max_workers)

        async def _task(idx: int, inp: str) -> Tuple[int, str]:
            async with semaphore:
                try:
                    return idx, await self._generate_single_async(inp, system_prompt, tools)
                except Exception as e:
                    err = f"Error: {e}"
                    logger.error(
                        "Failed to generate response for input %d: %s", idx, e
                    )
                    return idx, err

        tasks = [_task(idx, inp) for idx, inp in enumerate(user_inputs)]

        try:
            from tqdm.asyncio import tqdm_asyncio

            results = await tqdm_asyncio.gather(
                *tasks,
                total=len(tasks),
                desc=f"Generating (concurrency: {self._max_workers})",
            )
        except ImportError:
            logger.warning("tqdm.asyncio not available — running without progress bar.")
            results = await asyncio.gather(*tasks)

        responses: List[Optional[str]] = [None] * len(user_inputs)
        for idx, result in results:
            responses[idx] = result

        return responses  # type: ignore[return-value]


def _is_retryable(error_str: str) -> bool:
    """Check whether an error string indicates a transient failure."""
    markers = ["rate limit", "timeout", "connection", "503", "502", "429"]
    return any(m in error_str for m in markers)