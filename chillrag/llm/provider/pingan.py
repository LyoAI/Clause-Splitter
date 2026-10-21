from __future__ import annotations
from typing import Any, Dict, List, Optional
from openai import OpenAI, AsyncOpenAI
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from urllib.parse import urlencode
from .base import LLMProvider, LLMResponse, StreamChunk, ToolCallRequest
from chillrag.config import LLM_TIMEOUT_S
from chillrag.utils import get_logger
import base64
import hmac
import binascii
import json
import json_repair
import time


logger = get_logger()

class PingAnLLMProvider(LLMProvider):
    """LLM provider for the Ping An model-platform gateway."""

    def __init__(self, api_key: str = "token-abc", api_base: str = "http://localhost:8000/v1", default_model: str = "default",
                 extra_headers: dict[str, any] | None = None):
        super().__init__(api_key, api_base)
        self.extra_headers = extra_headers or {}
        # Platform app key / secret, plus the hex RSA credential issued by the
        # eagw gateway — all passed via extra_headers.
        self.app_key = self.extra_headers.pop("appKey", "")
        self.app_secret = self.extra_headers.pop("appSecret", "")
        self.rsa_private_key = self.extra_headers.pop("rsaPrivateKey", "")
        self.openapi_credential = self.extra_headers.pop("openApiCredential", "")
        self.scene_id = int(self.extra_headers.pop("sceneId", ""))
        self.request_id = self.extra_headers.pop("requestId", "")
        self.model_name = self.extra_headers.pop("model_name", "")

        # Credentials are deliberately not logged, only what identifies the call.
        logger.info(
            "pingan provider: scene_id=%s, request_id=%s, model=%s, base=%s",
            self.scene_id, self.request_id, self.model_name, api_base,
        )

        self.default_model = default_model
        self.client = OpenAI(api_key=api_key, base_url=api_base,
                             timeout=LLM_TIMEOUT_S, max_retries=1)
        self.async_client = AsyncOpenAI(api_key=api_key, base_url=api_base,
                                        timeout=LLM_TIMEOUT_S, max_retries=1)
        self.requestTime = str(int(time.time() * 1000))

    def get_gpt_sign(self, app_key: str, app_secret: str, requestTime: int) -> str:
        """Base64 HMAC-SHA1 signature for the GPT API call."""
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
        """Uppercase-hex RSA signature (SHA256, PKCS1_v1_5) over the request time."""
        binary_key = binascii.a2b_hex(rsaPrivateKey)
        pkcs8_private_key = RSA.import_key(binary_key)
        h = SHA256.new(requestTime.encode("utf-8"))
        signer = PKCS1_v1_5.new(pkcs8_private_key)
        signature = signer.sign(h).hex().upper()
        return signature

    def _parse(self, response: Any) -> LLMResponse:
        # The gateway may attach a top-level resultCode; the OpenAI SDK object
        # doesn't, so read it defensively rather than crashing on every response.
        result_code = getattr(response, "resultCode", "0")
        if result_code != "0":
            logger.error("gateway returned resultCode=%s: %s", result_code, response)

        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None
        tool_calls = [
            ToolCallRequest(id=tool_call.id, name=tool_call.function.name,
                            arguments=json_repair.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str) else tool_call.function.arguments)
            for tool_call in (message.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content = message.content if message else "",
            tool_calls = tool_calls,
            finish_reason = choice.finish_reason or "stop",
            usage = {
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens
            } if u else {},
            reasoning_content = getattr(message, "reasoning_content", None) or None
        )
    
    def invoke(
        self, 
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        model: Optional[str] = None,
        max_completion_tokens: Optional[int] = 4096,
        max_context_len: int = 200000,
        temperature: Optional[float] = 0.7,
        top_p: Optional[float] = 1.0,
        enable_thinking: bool = False,
        timeout: Optional[float] = None,  # None = inherit the client timeout
        stream: bool = False,
    ) -> LLMResponse:
        """Call the Ping An chat endpoint (sync)."""
        request_time = str(int(time.time() * 1000))
        get_signature = self.get_gpt_sign(self.app_key, self.app_secret, request_time)
        eagw_sign = self.get_sign(self.rsa_private_key, request_time)
        model = model or self.default_model

        extra_body = {
            "request_id": self.request_id,
            "scene_id": self.scene_id,
        }
        chat_template_kwargs = {"thinking": enable_thinking} if "deepseek" in model else {"enable_thinking": enable_thinking}
        extra_body.update({"chat_template_kwargs": chat_template_kwargs})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_empty_content(messages),
            "max_completion_tokens": max(1, max_completion_tokens),
            "extra_body": extra_body,
            "extra_headers": {
                "Content-Type": "application/json",
                "openApiCode": "API035059",
                "openApiCredential": self.openapi_credential,
                "openApiRequestTime": request_time,
                "openApiSignature": eagw_sign,
                "gpt_app_key": self.app_key,
                "gpt_signature": get_signature
            },
            "temperature": temperature,
            "top_p": top_p,
            "timeout": timeout,
            "stream": stream
        }
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        self._cap_payload(kwargs, messages, max_context_len)

        try:
            response = self.client.chat.completions.create(**kwargs)
            return self._parse(response)
        except Exception as e:
            logger.error("pingan invoke failed: %s", e)
            return LLMResponse(content="Error: Failed to get response from LLM.", tool_calls=[], finish_reason="error", usage={})
        
    async def ainvoke(
        self, 
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        model: Optional[str] = None,
        max_completion_tokens: Optional[int] = 4096,
        max_context_len: int = 200000,
        temperature: Optional[float] = 0.7,
        top_p: Optional[float] = 1.0,
        enable_thinking: bool = False,
        timeout: Optional[float] = None,  # None = inherit the client timeout
        stream: bool = False,
    ) -> LLMResponse:
        """Call the Ping An chat endpoint (async)."""
        request_time = str(int(time.time() * 1000))
        get_signature = self.get_gpt_sign(self.app_key, self.app_secret, request_time)
        eagw_sign = self.get_sign(self.rsa_private_key, request_time)
        model = model or self.default_model

        extra_body = {
            "request_id": self.request_id,
            "scene_id": self.scene_id,
        }
        chat_template_kwargs = {"thinking": enable_thinking} if "deepseek" in model else {"enable_thinking": enable_thinking}
        extra_body.update({"chat_template_kwargs": chat_template_kwargs})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_empty_content(messages),
            "max_completion_tokens": max(1, max_completion_tokens),
            "extra_body": extra_body,
            "extra_headers": {
                "Content-Type": "application/json",
                "openApiCode": "API035059",
                "openApiCredential": self.openapi_credential,
                "openApiRequestTime": request_time,
                "openApiSignature": eagw_sign,
                "gpt_app_key": self.app_key,
                "gpt_signature": get_signature
            },
            "temperature": temperature,
            "top_p": top_p,
            "timeout": timeout,
            "stream": stream
        }
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        self._cap_payload(kwargs, messages, max_context_len)

        try:
            response = await self.async_client.chat.completions.create(**kwargs)
            return self._parse(response)
        except Exception as e:
            logger.error("pingan ainvoke failed: %s", e)
            return LLMResponse(content="Error: Failed to get response from LLM.", tool_calls=[], finish_reason="error", usage={})

    #: The gateway rejects any request whose serialized payload exceeds this.
    _MAX_PAYLOAD_BYTES = 2_000_000

    def _cap_payload(self, kwargs: dict, messages: list, max_context_len: int) -> None:
        """Truncate the last user message so the payload fits the gateway's cap.

        Head and tail are kept — the question tends to sit at one end. The size
        is re-checked afterwards because a large tool schema can exceed the cap
        on its own, and truncating a message would not help.
        """
        size = len(json.dumps(kwargs, ensure_ascii=False).encode("utf-8"))
        if size <= self._MAX_PAYLOAD_BYTES:
            return
        logger.warning(
            "request payload %d B exceeds the %d B cap; truncating the last user message",
            size, self._MAX_PAYLOAD_BYTES,
        )
        half = max_context_len // 2
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message["content"]
                message["content"] = content[:half] + " ... " + content[-half:]
                break
        kwargs.update(messages=self._sanitize_empty_content(messages))
        new_size = len(json.dumps(kwargs, ensure_ascii=False).encode("utf-8"))
        if new_size > self._MAX_PAYLOAD_BYTES:
            logger.error(
                "request payload still %d B after truncation; reduce the messages or tools",
                new_size,
            )
            raise ValueError("Request size exceeds the limit of 2M length.")
    
    def get_default_model(self) -> str:
        """Return the default model id."""
        return self.default_model

    async def astream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        model: Optional[str] = None,
        max_completion_tokens: Optional[int] = 4096,
        temperature: Optional[float] = 0.7,
    ):
        """Fallback stream: one chunk from ``ainvoke`` (no token-level streaming)."""
        resp = await self.ainvoke(
            messages, tools=tools, model=model,
            max_completion_tokens=max_completion_tokens, temperature=temperature,
        )
        yield StreamChunk(
            content=resp.content or "",
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )
