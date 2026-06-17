"""Tool framework — base classes and registry.

Provides the foundation for tool execution shared by all agents.
Each agent (aca, assistant) registers its own tools via ToolRegistry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Result of a tool execution."""

    name: str
    content: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.error is not None


class Tool(ABC):
    """Base class for all agent tools."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, **params: Any) -> str:
        """Execute the tool with given parameters. Returns result string."""
        ...

    def to_openai_schema(self) -> dict[str, Any]:
        """Export as an OpenAI-compatible function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registry of tools for an agent.

    Tools can be called directly or exported as OpenAI-compatible
    function definitions for LLM function calling.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        """Register multiple tools at once."""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    async def execute(self, name: str, **params: Any) -> str:
        """Execute a tool by name. Returns result string."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found"
        try:
            return await tool.execute(**params)
        except Exception as e:
            return f"Error executing tool '{name}': {e}"

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        """Export all tools as OpenAI-compatible function definitions."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    @property
    def tool_names(self) -> list[str]:
        """List of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
