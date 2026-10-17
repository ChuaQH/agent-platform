"""Deterministic coverage for server-derived file workspaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant import server
from core.config import Config
from core.tools.document import ExtractTextTool, ReadSpreadsheetTool
from core.tools.file_io import ReadFileTool, WriteFileTool
from core.user_workspace import UserWorkspace, WorkspacePathError, safe_user_directory


@pytest.mark.asyncio
async def test_file_tools_isolate_users_with_the_same_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    user_a = UserWorkspace(root, "account-a")
    user_b = UserWorkspace(root, "account-b")
    write_a, read_a = WriteFileTool(user_a), ReadFileTool(user_a)
    write_b, read_b = WriteFileTool(user_b), ReadFileTool(user_b)

    assert await write_a.execute(path="notes.txt", content="A's notes") == "Wrote 9 bytes to notes.txt"
    assert await write_b.execute(path="notes.txt", content="B's notes") == "Wrote 9 bytes to notes.txt"

    assert (user_a.path / "notes.txt").read_text(encoding="utf-8") == "A's notes"
    assert (user_b.path / "notes.txt").read_text(encoding="utf-8") == "B's notes"
    assert user_a.path != user_b.path
    assert "account-a" not in user_a.path.name
    assert "account-b" not in user_b.path.name
    assert "A's notes" in await read_a.execute(path="notes.txt")
    assert "B's notes" in await read_b.execute(path="notes.txt")


@pytest.mark.asyncio
async def test_file_tools_reject_cross_user_guessing_and_unsafe_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    user_a = UserWorkspace(root, "account-a")
    user_b = UserWorkspace(root, "account-b")
    await WriteFileTool(user_a).execute(path="private.txt", content="only A")
    read_b = ReadFileTool(user_b)

    # A guessed internal directory is just a relative path under B's own root.
    guessed = f"users/{safe_user_directory('account-a')}/private.txt"
    assert "File not found" in await read_b.execute(path=guessed)

    for path in ("../private.txt", "nested/../private.txt", r"nested\..\private.txt",
                 "/etc/passwd", r"C:\temp\private.txt", "C:private.txt",
                 r"\\server\share\private.txt"):
        result = await read_b.execute(path=path)
        assert result.startswith("Error:")
        assert str(root) not in result


@pytest.mark.asyncio
async def test_document_tools_use_the_same_user_workspace(tmp_path: Path) -> None:
    workspace = UserWorkspace(tmp_path / "workspace", "document-user")
    writer = WriteFileTool(workspace)
    assert "Wrote" in await writer.execute(path="docs/note.txt", content="private document")
    assert "Wrote" in await writer.execute(path="data/items.csv", content="name,amount\nwidget,2\n")

    extracted = await ExtractTextTool(workspace).execute(path="docs/note.txt")
    spreadsheet = await ReadSpreadsheetTool(workspace).execute(path="data/items.csv")
    assert "private document" in extracted
    assert "Columns: name, amount" in spreadsheet
    assert not (tmp_path / "workspace" / "docs" / "note.txt").exists()


def test_resolver_rejects_symlink_escape_when_supported(tmp_path: Path) -> None:
    workspace = UserWorkspace(tmp_path / "workspace", "link-user")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace.path / "escape.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(WorkspacePathError):
        workspace.resolve("escape.txt")


def test_uuid_identity_uses_its_canonical_directory_name() -> None:
    assert safe_user_directory("A0D43F44-29CB-4E95-9FEF-18AF7BDCE0C5") == "a0d43f44-29cb-4e95-9fef-18af7bdce0c5"


@pytest.mark.asyncio
async def test_server_registry_derives_one_workspace_from_trusted_user_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(project_root=tmp_path)
    monkeypatch.setattr(server, "config", config)
    config.workspace_root.mkdir(parents=True)
    (config.workspace_root / "legacy.txt").write_text("legacy shared data", encoding="utf-8")

    registry = server._build_tool_registry("42", object())
    file_tools = [
        registry.get("read_file"), registry.get("write_file"),
        registry.get("extract_text"), registry.get("read_spreadsheet"),
    ]
    assert all(tool is not None for tool in file_tools)
    workspaces = [tool.workspace for tool in file_tools]  # type: ignore[union-attr]
    assert all(workspace is workspaces[0] for workspace in workspaces)
    assert workspaces[0].path.parent.name == "users"
    assert workspaces[0].path.name == safe_user_directory("42")
    read = registry.get("read_file")
    assert isinstance(read, ReadFileTool)
    assert "File not found" in await read.execute(path="legacy.txt")
