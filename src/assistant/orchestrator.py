"""Assistant orchestrator — ReAct loop with tool execution.

Handles the conversation loop: user message → LLM thinks → calls tools → observes → responds.
Uses the shared core infrastructure (LLM client, conversation, tool framework).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from core.config import Config
from core.llm_client import LLMClient
from core.conversation import Conversation
from core.tool_framework import ToolRegistry
from core.logging import AgentLogger

from core.tools.calculator import CalculatorTool
from core.tools.datetime_tools import CurrentDatetimeTool, UserTimezoneTool, UserLocaleTool, DateMathTool
from core.tools.user_location import UserLocationTool
from assistant.tools.web_search import WebSearchTool
from assistant.tools.web_fetch import WebFetchTool
from assistant.tools.personal_memory import RememberTool, RecallTool
from assistant.memory.personal import PersonalMemory
from assistant.memory.conversations import ConversationMemory

logger = logging.getLogger(__name__)

# System prompt for the general assistant
ASSISTANT_SYSTEM_PROMPT = """\
You are a helpful AI assistant with access to tools for web search, calculations, datetime utilities, and memory.

Your capabilities:
- **Web search**: You can search the web for current information when you need facts or data beyond your training.
- **Web fetch**: You can read the contents of a specific URL for detailed information.
- **Calculator**: You can evaluate mathematical expressions accurately.
- **Date/time**: You can get the current datetime, the user's timezone, locale, and perform date arithmetic (e.g. "3 days from now").
- **Location**: You can get the user's approximate location for context-aware responses.
- **Memory**: You can remember facts about the user and recall them later.

Guidelines:
- Be concise and helpful.
- Use web search when you need current or factual information you're unsure about.
- Use the calculator for math rather than doing it in your head.
- Use datetime tools when asked about time, dates, or scheduling.
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

        # Structured trace logger
        self.trace = AgentLogger("assistant", config.agent_data_dir)

    def _register_tools(self) -> None:
        """Register all assistant tools."""
        self.tools.register(WebSearchTool(api_key=self.config.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(CalculatorTool())
        self.tools.register(CurrentDatetimeTool())
        self.tools.register(UserTimezoneTool())
        self.tools.register(UserLocaleTool())
        self.tools.register(DateMathTool())
        self.tools.register(UserLocationTool())
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

    async def handle_message_stream(
        self, user_message: str
    ) -> AsyncGenerator[str, None]:
        """Process a user message and stream events via SSE.

        Decision steps use non-streaming chat() for complete reasoning text.
        The final answer is chunked for smooth streaming UX.

        Yields SSE events:
        - "thinking": complete reasoning text from LLM (shown once, clean)
        - "tool_call": a tool is about to be executed
        - "tool_result": tool execution completed (with sources for web_search)
        - "response": final answer, streamed token by token
        - "done": stream complete
        - "error": something went wrong
        """
        self.conversation.add_user_message(user_message)
        self.trace.request_start(user_message)

        max_turns = self.config.max_tool_calls_per_turn
        for turn in range(max_turns):
            # ── Non-streaming: get complete reasoning + tool calls ──
            self.trace.llm_call(
                turn, len(self.conversation.messages), len(self.tools)
            )
            response = await self.llm.chat(
                messages=self.conversation.get_messages_for_api(),
                tools=self.tools.to_openai_schemas(),
            )
            self.trace.llm_response(
                turn,
                content=response.content or "",
                tool_calls=response.tool_calls,
                usage=response.usage,
                finish_reason=response.finish_reason,
            )

            reasoning_text = response.content or ""

            # ── No tool calls → final answer ──────
            if not response.tool_calls:
                self.conversation.add_assistant_message(content=reasoning_text)

                # Stream the response in chunks for smooth UX
                if reasoning_text.strip():
                    import asyncio
                    chunk_size = 80  # characters per chunk
                    for i in range(0, len(reasoning_text), chunk_size):
                        chunk = reasoning_text[i:i + chunk_size]
                        yield self._sse("response", {"content": chunk})
                        await asyncio.sleep(0.02)  # 20ms delay — smooth typing feel

                yield self._sse("done", {})
                self.trace.request_end()
                return

            # ── Has tool calls → execute them ─────
            # Emit reasoning as thinking bubble
            if reasoning_text.strip():
                yield self._sse("thinking", {"content": reasoning_text})
                self.trace.thinking(reasoning_text)

            # Add assistant message with tool calls to conversation
            self.conversation.add_assistant_message(
                content=reasoning_text or None,
                tool_calls=response.tool_calls,
            )

            for tc in response.tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                # Notify frontend: tool is running
                query_preview = ""
                if tool_name == "web_search":
                    query_preview = arguments.get("query", "")
                elif tool_name == "web_fetch":
                    query_preview = arguments.get("url", "")
                elif tool_name == "calculator":
                    query_preview = arguments.get("expression", "")
                elif tool_name == "assistant_remember":
                    query_preview = arguments.get("fact", "")[:80]

                yield self._sse("tool_call", {
                    "tool_name": tool_name,
                    "query": query_preview,
                })

                # Execute the tool
                logger.info("Tool call: %s(%s)", tool_name, arguments)
                self.trace.tool_start(tool_name, arguments)
                result = await self.tools.execute(tool_name, **arguments)
                success = not result.startswith("Search failed") and not result.startswith("Error")
                self.trace.tool_end(tool_name, success, result)
                self.conversation.add_tool_result(
                    tool_call_id=tc["id"],
                    tool_name=tool_name,
                    result=result,
                )

                # Notify frontend: tool done
                tool_result_data: dict[str, Any] = {
                    "tool_name": tool_name,
                    "success": success,
                }

                if tool_name == "web_search":
                    sources = self._extract_sources(result)
                    tool_result_data["sources"] = sources

                yield self._sse("tool_result", tool_result_data)

            # Continue loop — LLM will process tool results in next turn
            continue

        # Max turns exceeded
        yield self._sse("error", {"message": "Max tool calls reached."})

    def _extract_sources(self, search_result: str) -> list[dict[str, str]]:
        """Extract URL + title sources from a web_search result string."""
        sources: list[dict[str, str]] = []
        lines = search_result.split("\n")
        current_title = ""
        for line in lines:
            line = line.strip()
            if line.startswith("URL: "):
                url = line[5:].strip()
                if url and current_title:
                    # Clean title (remove leading number + dot)
                    clean_title = current_title
                    if ". " in clean_title[:5]:
                        clean_title = clean_title.split(". ", 1)[1]
                    sources.append({"title": clean_title, "url": url})
                current_title = ""
            elif line and not line.startswith("Search results") and not line.startswith("URL:") and line[0].isdigit() and ". " in line[:5]:
                current_title = line
        return sources[:5]

    @staticmethod
    def _sse(event_type: str, data: dict[str, Any]) -> str:
        """Format a dict as an SSE data line."""
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        return f"data: {payload}\n\n"

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
