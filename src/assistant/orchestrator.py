"""Assistant orchestrator — ReAct loop with tool execution.

Handles the conversation loop: user message → LLM thinks → calls tools → observes → responds.
Uses the shared core infrastructure (LLM client, conversation, tool framework).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import Config
from core.llm_client import LLMClient
from core.conversation import Conversation
from core.tool_framework import ToolRegistry

from assistant.tools.web_search import WebSearchTool
from assistant.tools.web_fetch import WebFetchTool
from assistant.tools.calculator import CalculatorTool
from assistant.tools.personal_memory import RememberTool, RecallTool
from assistant.memory.personal import PersonalMemory
from assistant.memory.conversations import ConversationMemory

logger = logging.getLogger(__name__)

# System prompt for the general assistant
ASSISTANT_SYSTEM_PROMPT = """\
You are a helpful AI assistant with access to tools for web search, calculations, and memory.

Your capabilities:
- **Web search**: You can search the web for current information when you need facts or data beyond your training.
- **Web fetch**: You can read the contents of a specific URL for detailed information.
- **Calculator**: You can evaluate mathematical expressions accurately.
- **Memory**: You can remember facts about the user and recall them later.

Guidelines:
- Be concise and helpful.
- Use web search when you need current or factual information you're unsure about.
- Use the calculator for math rather than doing it in your head.
- When a user tells you something about themselves (name, preferences, projects), use the memory tools to remember it.
- When starting a conversation, recall what you know about the user.
- If you don't know something, search for it rather than guessing.
"""


class AssistantOrchestrator:
    """General-purpose assistant orchestrator using a ReAct loop.

    Handles: user message → LLM → tool calls → execute → observe → respond.
    """

    def __init__(self, config: Config):
        self.config = config
        self.llm = LLMClient(config)

        # Memory
        data_dir = config.agent_data_dir / "assistant"
        self.personal_memory = PersonalMemory(data_dir)
        self.conversation_memory = ConversationMemory(data_dir / "sessions")

        # Tool registry
        self.tools = ToolRegistry()
        self._register_tools()

        # Active conversation (one per orchestrator instance)
        self.conversation = Conversation()
        self.session_id = uuid.uuid4().hex[:12]

    def _register_tools(self) -> None:
        """Register all assistant tools."""
        self.tools.register(WebSearchTool(api_key=self.config.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(CalculatorTool())
        self.tools.register(RememberTool(self.personal_memory))
        self.tools.register(RecallTool(self.personal_memory))
        logger.info(
            "Registered %d tools: %s",
            len(self.tools),
            ", ".join(self.tools.tool_names),
        )

    def start_session(self) -> None:
        """Initialize the conversation with the static system prefix."""
        self.conversation.set_static_prefix([ASSISTANT_SYSTEM_PROMPT])
        logger.info(
            "Session %s started. Static prefix: %d messages, hash=%s",
            self.session_id,
            self.conversation.static_prefix_length,
            self.conversation.static_prefix_hash,
        )

    async def handle_message(self, user_message: str) -> str:
        """Process a user message and return the assistant's response.

        This is the ReAct loop:
        1. Add user message to conversation
        2. Send to LLM with tools
        3. If LLM returns tool calls → execute → add results → go to 2
        4. If LLM returns text → return to user
        """
        self.conversation.add_user_message(user_message)

        max_turns = self.config.max_tool_calls_per_turn
        for turn in range(max_turns):
            response = await self.llm.chat(
                messages=self.conversation.get_messages_for_api(),
                tools=self.tools.to_openai_schemas(),
            )

            # LLM chose to respond with text (no tool calls)
            if response.content and not response.tool_calls:
                self.conversation.add_assistant_message(content=response.content)
                return response.content

            # LLM wants to call tools
            if response.tool_calls:
                # Add assistant message with tool calls
                self.conversation.add_assistant_message(
                    content=response.content,
                    tool_calls=response.tool_calls,
                )

                # Execute each tool
                for tc in response.tool_calls:
                    tool_name = tc["function"]["name"]
                    try:
                        import json
                        arguments = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}

                    logger.info("Tool call: %s(%s)", tool_name, arguments)
                    result = await self.tools.execute(tool_name, **arguments)

                    # Add tool result to conversation
                    self.conversation.add_tool_result(
                        tool_call_id=tc["id"],
                        tool_name=tool_name,
                        result=result,
                    )

                # Continue loop — LLM will process tool results
                continue

            # LLM finished without content or tool calls (shouldn't normally happen)
            return response.content or "I'm not sure how to help with that."

        # Max turns exceeded
        logger.warning("Max tool calls (%d) reached for session %s", max_turns, self.session_id)
        return "I've taken too many steps trying to answer. Could you rephrase or simplify your question?"

    def end_session(self) -> None:
        """Archive the current session."""
        self.conversation_memory.archive_session(
            self.session_id,
            self.conversation.messages,
        )
        logger.info("Session %s archived", self.session_id)

    @property
    def metrics(self) -> dict[str, Any]:
        """Return session metrics."""
        return {
            "session_id": self.session_id,
            "messages": len(self.conversation.messages),
            "estimated_tokens": self.conversation.estimate_tokens(),
            "llm_metrics": self.llm.cache_metrics_summary,
        }
