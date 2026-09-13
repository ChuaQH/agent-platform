"""File I/O tools — read and write files within a sandboxed workspace.

Path traversal protection ensures tools cannot escape the workspace directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.tool_framework import Tool, ToolCapability

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = Path("agent-data") / "workspace"
_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB read limit
_MAX_LINES = 2000  # Truncate reads to this many lines


def _resolve(path: str, workspace: Path) -> Path:
    """Resolve a path within the workspace, blocking traversal escapes."""
    workspace = workspace.resolve()
    target = (workspace / path).resolve()
    if workspace not in target.parents and target != workspace:
        raise ValueError(f"Path escapes workspace: {path}")
    return target


class ReadFileTool(Tool):
    name = "read_file"
    capabilities = frozenset({ToolCapability.WORKSPACE_READ})
    description = (
        "Read the contents of a file from the workspace. "
        "Returns the file content as text. Large files are truncated. "
        "The workspace root is the project's agent-data/workspace directory."
    )
    when_to_use = "To read the contents of a file from the workspace."
    how_to_use = "Provide a relative path to the file within the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the workspace.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path | None = None):
        self.workspace = Path(workspace or _DEFAULT_WORKSPACE).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            target = _resolve(path, self.workspace)
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"File not found: {path}"
        if not target.is_file():
            return f"Not a file: {path}"

        size = target.stat().st_size
        if size > _MAX_FILE_SIZE:
            return f"File too large ({size} bytes, max {_MAX_FILE_SIZE})."

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.error("Failed to read file: %s", target, exc_info=True)
            return f"Error reading file: {path}"

        lines = text.split("\n")
        total_lines = len(lines)
        if total_lines > _MAX_LINES:
            text = "\n".join(lines[:_MAX_LINES])
            text += f"\n\n... truncated ({total_lines - _MAX_LINES} more lines, {total_lines} total)"

        header = f"File: {path} ({total_lines} lines, {size} bytes)\n\n"
        return header + text


class WriteFileTool(Tool):
    name = "write_file"
    capabilities = frozenset({ToolCapability.WORKSPACE_WRITE})
    description = (
        "Write content to a file in the workspace. "
        "Creates parent directories if needed. "
        "⚠️ This overwrites existing files — use with care. "
        "Execution requires a separate server-side approval."
    )
    when_to_use = "When the user clearly asks to save or write content to a file."
    how_to_use = (
        "Provide the exact file path and content as a proposal. Server-side approval controls "
        "execution; do not request a separate chat confirmation or add confirmation fields."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the workspace.",
            },
            "content": {
                "type": "string",
                "description": "The text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: Path | None = None):
        self.workspace = Path(workspace or _DEFAULT_WORKSPACE).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def execute(self, path: str = "", content: str = "", **kwargs: Any) -> str:
        try:
            target = _resolve(path, self.workspace)
        except ValueError as e:
            return f"Error: {e}"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception:
            logger.error("Failed to write file: %s", target, exc_info=True)
            return f"Error writing file: {path}"
