"""LLM client for the ACA ecosystem.

Uses the OpenAI-compatible SDK pointed at the configured provider (default: DeepSeek).
Handles chat completion with automatic prefix caching. Tracks cache hit metrics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from core.config import Config

logger = logging.getLogger(__name__)


@dataclass
class CacheMetrics:
    """Tracks prompt caching statistics."""

    total_requests: int = 0
    total_prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.cache_hits / self.total_requests

    @property
    def token_savings_pct(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.cached_prompt_tokens / self.total_prompt_tokens


@dataclass
class LLMResponse:
    """Structured response from the LLM."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    cached_tokens: int = 0


class LLMClient:
    """Async client for LLM API via OpenAI-compatible SDK.

    Defaults to DeepSeek V4. Can be pointed at any OpenAI-compatible endpoint.
    Tracks prompt cache hit metrics for optimization.
    """

    def __init__(self, config: Config):
        self.config = config
        self._client: AsyncOpenAI | None = None
        self.metrics = CacheMetrics()
        self._last_prefix_hash: str | None = None
        self._prefix_hits: int = 0

    @property
    def client(self) -> AsyncOpenAI:
        """Lazy-init the OpenAI client pointed at the configured provider."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.llm_api_key,
                base_url=self.config.llm_base_url,
                timeout=120.0,
            )
        return self._client

    def _compute_prefix_hash(self, messages: list[dict[str, Any]]) -> str:
        """Compute a stable hash for the static prefix of messages.

        Only hashes everything except the last user message — this is the
        cacheable prefix that the provider will cache server-side.
        """
        prefix = messages[:-1] if messages else messages
        serialized = json.dumps(prefix, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: The conversation history (append-only).
            tools: Optional tool definitions for function calling.
            tool_choice: "auto", "none", or "required".
            temperature: Override default temperature.
            max_tokens: Override default max tokens.

        Returns:
            LLMResponse with content, tool calls, and usage info.
        """
        self.metrics.total_requests += 1

        prefix_hash = self._compute_prefix_hash(messages)
        expected_cache_hit = prefix_hash == self._last_prefix_hash
        if expected_cache_hit:
            self._prefix_hits += 1
        self._last_prefix_hash = prefix_hash

        api_messages = self._convert_messages(messages)

        start_time = time.monotonic()
        try:
            response = await self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=api_messages,
                tools=tools or [],
                tool_choice=tool_choice if tools else None,
                temperature=temperature if temperature is not None else self.config.llm_temperature,
                max_tokens=max_tokens or self.config.llm_max_tokens,
            )
        except Exception as e:
            logger.error("LLM API error: %s", e)
            raise

        elapsed = time.monotonic() - start_time

        choice = response.choices[0]
        usage = response.usage

        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        cached_tokens = 0
        if usage and hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)

        self.metrics.total_prompt_tokens += prompt_tokens
        self.metrics.cached_prompt_tokens += cached_tokens
        if cached_tokens > 0:
            self.metrics.cache_hits += 1
        else:
            self.metrics.cache_misses += 1

        logger.info(
            "LLM API: %dms, %d prompt tokens (%d cached), %d completion tokens",
            int(elapsed * 1000),
            prompt_tokens,
            cached_tokens,
            completion_tokens,
        )

        return LLMResponse(
            content=choice.message.content,
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (choice.message.tool_calls or [])
            ],
            finish_reason=choice.finish_reason or "stop",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
            },
            cached_tokens=cached_tokens,
        )

    def _convert_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert messages to OpenAI format, handling tool results."""
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "tool":
                converted.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                })
            elif role == "assistant" and msg.get("tool_calls"):
                converted.append({
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": msg["tool_calls"],
                })
            else:
                converted.append({
                    "role": role,
                    "content": msg.get("content", ""),
                })
        return converted

    @property
    def cache_metrics_summary(self) -> dict[str, Any]:
        """Return a summary of cache metrics for this client."""
        return {
            "requests": self.metrics.total_requests,
            "cache_hit_rate": f"{self.metrics.cache_hit_rate:.1%}",
            "token_savings": f"{self.metrics.token_savings_pct:.1%}",
            "cached_tokens": self.metrics.cached_prompt_tokens,
            "total_prompt_tokens": self.metrics.total_prompt_tokens,
        }
