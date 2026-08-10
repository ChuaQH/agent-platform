"""LLM client for the ACA ecosystem.

Uses the OpenAI-compatible SDK pointed at the configured provider (default: DeepSeek).
Handles chat completion (sync + streaming) with automatic prefix caching.
Tracks cache hit metrics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncGenerator
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
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    cached_tokens: int = 0


@dataclass
class StreamEvent:
    """A single event from a streaming chat completion."""

    type: str  # "thinking", "tool_call", "done"
    content: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


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
                timeout=self.config.llm_timeout,
            )
        return self._client

    def _compute_prefix_hash(self, messages: list[dict[str, Any]]) -> str:
        """Compute a stable hash for the static prefix of messages."""
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
        reasoning: bool | None = None,
    ) -> LLMResponse:
        """Send a chat completion request (non-streaming).

        Args:
            messages: The conversation history (append-only).
            tools: Optional tool definitions for function calling.
            tool_choice: "auto", "none", or "required".
            temperature: Override default temperature.
            max_tokens: Override default max tokens.
            reasoning: Override reasoning/thinking. None uses config default.

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
            extra_body = {}
            use_reasoning = reasoning if reasoning is not None else self.config.llm_reasoning
            if use_reasoning:
                extra_body["thinking"] = {
                    "type": "enabled",
                    "effort": self.config.llm_reasoning_effort,
                }
            elif reasoning is False:
                extra_body["thinking"] = {"type": "disabled"}

            response = await self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=api_messages,
                tools=tools or [],
                tool_choice=tool_choice if tools else None,
                temperature=temperature if temperature is not None else self.config.llm_temperature,
                max_tokens=max_tokens or self.config.llm_max_tokens,
                extra_body=extra_body if extra_body else None,
            )
        except Exception as e:
            logger.error("LLM API error: %s", e)
            raise

        elapsed = time.monotonic() - start_time

        choice = response.choices[0]
        usage = response.usage

        reasoning_content = getattr(choice.message, "reasoning_content", None)

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
            reasoning_content=reasoning_content,
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

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a streaming chat completion request.

        Yields StreamEvent objects as tokens arrive:
        - type="thinking": content delta from the LLM's reasoning text
        - type="tool_call": a complete tool call was detected
        - type="done": stream finished with usage info

        Tool calls are accumulated across chunks and emitted as a single
        "tool_call" event once the full JSON arguments are received.

        Args:
            messages: The conversation history (append-only).
            tools: Optional tool definitions for function calling.
            tool_choice: "auto", "none", or "required".
            temperature: Override default temperature.
            max_tokens: Override default max tokens.

        Yields:
            StreamEvent objects.
        """
        self.metrics.total_requests += 1

        api_messages = self._convert_messages(messages)

        try:
            stream = await self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=api_messages,
                tools=tools or [],
                tool_choice=tool_choice if tools else None,
                temperature=temperature if temperature is not None else self.config.llm_temperature,
                max_tokens=max_tokens or self.config.llm_max_tokens,
                stream=True,
            )
        except Exception as e:
            logger.error("LLM streaming API error: %s", e)
            raise

        accumulated_content = ""
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0
        thinking_sent = False

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            # Track usage if present in chunk
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0

            finish_reason = chunk.choices[0].finish_reason or finish_reason

            # ── Text content ────────────────────
            if delta.content:
                accumulated_content += delta.content
                if not thinking_sent:
                    thinking_sent = True
                yield StreamEvent(
                    type="thinking",
                    content=delta.content,
                )

            # ── Tool calls ──────────────────────
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }

                    acc = accumulated_tool_calls[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["function"]["arguments"] += tc_delta.function.arguments

        # ── Emit accumulated tool calls ─────────
        for tc in sorted(accumulated_tool_calls.values(), key=lambda t: t.get("id", "")):
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                tool_args = {}

            yield StreamEvent(
                type="tool_call",
                tool_name=tool_name,
                tool_args=tool_args,
                content=tool_name,
            )

        # ── Done ────────────────────────────────
        self.metrics.total_prompt_tokens += prompt_tokens
        self.metrics.cached_prompt_tokens += cached_tokens
        if cached_tokens > 0:
            self.metrics.cache_hits += 1
        else:
            self.metrics.cache_misses += 1

        yield StreamEvent(
            type="done",
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
            },
        )

        logger.info(
            "LLM stream finished: %d thinking chars, %d tool calls, %d tokens",
            len(accumulated_content),
            len(accumulated_tool_calls),
            prompt_tokens + completion_tokens,
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
