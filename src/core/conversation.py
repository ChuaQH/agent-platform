"""Conversation state management.

Maintains append-only message history with static prefix tracking.
Handles conversation compression when approaching context limits.
Shared by all agents (aca, assistant).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    """Append-only conversation history.

    The first messages form the STATIC PREFIX (system prompt, tools, project identity).
    These are never modified after initialization. All subsequent messages are appended.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    static_prefix_length: int = 0
    static_prefix_hash: str = ""

    def set_static_prefix(self, system_prompts: list[str]) -> None:
        """Set the static (cacheable) prefix of the conversation.

        Called once at session start. Includes system prompt, tool definitions,
        and any static context (project identity, decision index, etc.).

        Args:
            system_prompts: List of system-level prompt strings.
        """
        self.messages = [
            {"role": "system", "content": prompt}
            for prompt in system_prompts
        ]
        self.static_prefix_length = len(self.messages)
        self.static_prefix_hash = self._compute_hash()
        logger.info(
            "Static prefix set: %d messages, hash=%s",
            self.static_prefix_length,
            self.static_prefix_hash,
        )

    def add_user_message(self, content: str) -> None:
        """Append a user message."""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an assistant message (with optional tool calls)."""
        msg: dict[str, Any] = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> None:
        """Append a tool result message."""
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        })

    def get_messages_for_api(self) -> list[dict[str, Any]]:
        """Return all messages for the API call."""
        return list(self.messages)

    def estimate_tokens(self) -> int:
        """Rough estimate of total token count (4 chars ≈ 1 token)."""
        total_chars = sum(
            len(str(msg.get("content", "")))
            + len(json.dumps(msg.get("tool_calls", [])))
            for msg in self.messages
        )
        return total_chars // 4

    def check_prefix_integrity(self) -> bool:
        """Verify the static prefix hasn't been modified.

        Returns False if the prefix hash differs from the original, indicating
        that the cache will be invalidated.
        """
        current_hash = self._compute_hash()
        if current_hash != self.static_prefix_hash:
            logger.warning(
                "Static prefix hash mismatch! Cache will be invalidated. "
                "Expected=%s, Got=%s",
                self.static_prefix_hash,
                current_hash,
            )
            return False
        return True

    def _compute_hash(self) -> str:
        """Compute hash of the static prefix messages."""
        prefix = self.messages[:self.static_prefix_length] if self.static_prefix_length else self.messages
        serialized = json.dumps(prefix, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    @property
    def dynamic_messages(self) -> list[dict[str, Any]]:
        """Messages after the static prefix."""
        return self.messages[self.static_prefix_length:]
