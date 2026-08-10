"""Web search tool for the assistant agent.

Uses Brave Search API (brave.com/search/api/).
Free tier: 2,000 queries/month. Requires BRAVE_API_KEY in .env or environment.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.tool_framework import Tool

logger = logging.getLogger(__name__)

# Brave Search API
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


class WebSearchTool(Tool):
    """Search the web via Brave Search API and return results."""

    name = "web_search"
    description = (
        "Search the web for current information. "
        "Use when you need facts, news, or documentation that may have "
        "changed since your training cutoff, or when you need to verify "
        "something you're unsure about."
    )
    when_to_use = "When you need current information, news, facts, or data beyond your training cutoff."
    how_to_use = "Provide a specific, targeted search query."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "n_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of results to return (max 10).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    async def execute(self, query: str, n_results: int = 5, **kwargs: Any) -> str:
        """Search Brave and return formatted results."""
        if not self.api_key:
            return (
                "Web search is not configured. Set the BRAVE_API_KEY environment "
                "variable to enable web search. Get a free key at https://brave.com/search/api/"
            )

        n_results = min(n_results, 10)

        try:
            results = await self._search_brave(query, n_results)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return "Search rate limit reached. Please wait a moment and try again."
            logger.error("Brave search HTTP error: %s", e)
            return f"Search failed (HTTP {e.response.status_code})"
        except Exception:
            logger.error("Brave search failed", exc_info=True)
            return "Search failed. Please try again."

        if not results:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            snippet = r.get("snippet", "")
            url = r.get("url", "")
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append(f"   URL: {url}")
            lines.append("")

        return "\n".join(lines)

    async def _search_brave(
        self, query: str, n_results: int
    ) -> list[dict[str, str]]:
        """Search using Brave Search API.

        API docs: https://api.search.brave.com/app/documentation/web-search
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                BRAVE_API_URL,
                params={
                    "q": query,
                    "count": min(n_results, 20),
                },
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.api_key,
                },
            )
            response.raise_for_status()
            data = response.json()

            results: list[dict[str, str]] = []

            # Web results
            web_results = data.get("web", {}).get("results", [])
            for r in web_results[:n_results]:
                results.append({
                    "title": r.get("title", ""),
                    "snippet": (r.get("description") or "")[:500],
                    "url": r.get("url", ""),
                })

            return results
