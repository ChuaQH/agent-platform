"""File I/O tools — read and write files within a sandboxed workspace.

Path traversal protection ensures tools cannot escape the workspace directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.tool_framework import Tool

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
        except Exception as e:
            return f"Error reading file: {e}"

        lines = text.split("\n")
        total_lines = len(lines)
        if total_lines > _MAX_LINES:
            text = "\n".join(lines[:_MAX_LINES])
            text += f"\n\n... truncated ({total_lines - _MAX_LINES} more lines, {total_lines} total)"

        header = f"File: {path} ({total_lines} lines, {size} bytes)\n\n"
        return header + text


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write content to a file in the workspace. "
        "Creates parent directories if needed. "
        "⚠️ This overwrites existing files — use with care. "
        "Set 'confirmed' to true to proceed."
    )
    when_to_use = "When the user asks to save or write content to a file. Requires user confirmation."
    how_to_use = "Provide the file path, content, and set confirmed=true to actually write."
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
            "confirmed": {
                "type": "boolean",
                "description": "Must be true to actually write. Set to false to preview.",
            },
        },
        "required": ["path", "content", "confirmed"],
    }

    def __init__(self, workspace: Path | None = None):
        self.workspace = Path(workspace or _DEFAULT_WORKSPACE).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def execute(self, path: str = "", content: str = "", confirmed: bool = False, **kwargs: Any) -> str:
        try:
            target = _resolve(path, self.workspace)
        except ValueError as e:
            return f"Error: {e}"

        if not confirmed:
            preview = content[:200] + ("..." if len(content) > 200 else "")
            return f"Preview of {path}:\n\n{preview}\n\nSet confirmed=true to write."

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {e}"
