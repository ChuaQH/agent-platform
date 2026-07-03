"""Web fetch tool — fetches and extracts text from a URL."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from core.tool_framework import Tool

logger = logging.getLogger(__name__)

# HTML tags to strip
TAG_RE = re.compile(r"<[^>]+>")


class WebFetchTool(Tool):
    """Fetch a URL and extract readable text content."""

    name = "web_fetch"
    description = (
        "Fetch the content of a URL and extract readable text. "
        "Use this after web_search to read a specific page in detail. "
        "Returns the page content as plain text."
    )
    when_to_use = "To read the full content of a URL after finding it via web_search, or when the user provides a URL."
    how_to_use = "Provide the complete URL including protocol (https://...)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch.",
            },
        },
        "required": ["url"],
    }

    MAX_CONTENT_CHARS = 20000
    TIMEOUT = 15.0

    async def execute(self, url: str, **kwargs: Any) -> str:
        """Fetch URL and return extracted text."""
        try:
            content = await self._fetch(url)
        except httpx.HTTPStatusError as e:
            return f"HTTP error fetching {url}: {e.response.status_code}"
        except httpx.TimeoutException:
            return f"Timeout fetching {url}"
        except Exception as e:
            logger.error("Fetch failed for %s: %s", url, e)
            return f"Failed to fetch {url}: {e}"

        text = self._extract_text(content)

        if len(text) > self.MAX_CONTENT_CHARS:
            text = text[:self.MAX_CONTENT_CHARS] + "\n\n[Content truncated...]"

        return f"Content from {url}:\n\n{text}"

    async def _fetch(self, url: str) -> str:
        """Fetch URL content as string."""
        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "ACA-Assistant/0.1",
                    "Accept": "text/html,text/plain",
                },
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.text

    def _extract_text(self, html: str) -> str:
        """Extract readable text from HTML.

        Strips tags, scripts, styles, and collapses whitespace.
        """
        # Remove script and style sections
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML comments
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

        # Replace block-level tags with newlines
        for tag in ["p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"]:
            html = re.sub(rf"</?{tag}[^>]*>", "\n", html, flags=re.IGNORECASE)

        # Strip remaining tags
        text = TAG_RE.sub("", html)

        # Decode HTML entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")

        # Collapse whitespace
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)
