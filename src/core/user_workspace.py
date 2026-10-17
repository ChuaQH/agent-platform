"""Server-derived, per-user workspace paths for file-oriented tools.

This is application-level logical segregation, not an operating-system
sandbox.  Callers must provide trusted server identity; model arguments only
ever provide paths relative to the resulting workspace.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
from uuid import UUID


class WorkspacePathError(ValueError):
    """A model-provided path is not safe to use in a user workspace."""


def safe_user_directory(user_id: str | int) -> str:
    """Return a stable non-identifying directory name for trusted identity."""
    value = str(user_id).strip()
    if not value:
        raise ValueError("user identity is required for a workspace")
    try:
        return str(UUID(value))
    except ValueError:
        pass
    # Numeric database IDs and other non-UUID identifiers are not placed in
    # the filesystem verbatim.  A domain separator prevents accidental reuse
    # of this digest for a different identity purpose.
    digest = hashlib.sha256(f"assistant-workspace:{value}".encode("utf-8")).hexdigest()
    return f"user-{digest}"


class UserWorkspace:
    """Resolve model paths inside one server-derived user directory.

    ``for_testing`` is deliberately explicit for direct library tests.  The
    authenticated server path always uses ``UserWorkspace(root, user_id)``.
    """

    def __init__(self, root: Path | str, user_id: str | int) -> None:
        self._root = Path(root)
        self._directory_name = safe_user_directory(user_id)
        self._user_scoped = True

    @classmethod
    def for_testing(cls, workspace: Path | str) -> "UserWorkspace":
        """Build an explicit isolated workspace without a production identity.

        This compatibility seam is for deterministic direct tool construction,
        not for server request handling.
        """
        instance = cls.__new__(cls)
        instance._root = Path(workspace)
        instance._directory_name = ""
        instance._user_scoped = False
        return instance

    @property
    def path(self) -> Path:
        """The physical path for server-side use only; never serialize it."""
        return self._ensure_workspace()

    def resolve(self, supplied_path: str) -> Path:
        """Resolve one model path while rejecting absolute and escaping paths."""
        parts = self._relative_parts(supplied_path)
        workspace = self._ensure_workspace()
        target = (workspace.joinpath(*parts)).resolve(strict=False)
        if not _is_within(target, workspace):
            raise WorkspacePathError("Path must stay within the user workspace.")
        return target

    def _ensure_workspace(self) -> Path:
        if not self._user_scoped:
            self._root.mkdir(parents=True, exist_ok=True)
            return self._root.resolve()

        users_root = self._root / "users"
        users_root.mkdir(parents=True, exist_ok=True)
        users_root = users_root.resolve()
        workspace = users_root / self._directory_name
        workspace.mkdir(parents=True, exist_ok=True)
        resolved = workspace.resolve()
        if not _is_within(resolved, users_root):
            raise WorkspacePathError("User workspace is unavailable.")
        return resolved

    @staticmethod
    def _relative_parts(supplied_path: str) -> list[str]:
        if not isinstance(supplied_path, str) or not supplied_path.strip() or "\x00" in supplied_path:
            raise WorkspacePathError("Path must be a non-empty relative path.")
        windows_path = PureWindowsPath(supplied_path)
        if supplied_path.startswith(("/", "\\")) or windows_path.is_absolute() or windows_path.drive:
            raise WorkspacePathError("Absolute paths are not allowed.")

        # Treat both separator styles as separators on every platform before
        # checking traversal.  This blocks mixed forms such as ``dir\\..//x``.
        parts = [part for part in supplied_path.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts:
            raise WorkspacePathError("Path must be a non-empty relative path.")
        if any(part == ".." for part in parts):
            raise WorkspacePathError("Parent-directory traversal is not allowed.")
        return parts


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True
