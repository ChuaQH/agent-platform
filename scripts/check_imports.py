#!/usr/bin/env python3
"""Enforce architectural dependency rules across agent packages.

Rules:
  1. src/aca/     → may import from: core, stdlib, third-party packages
                     must NOT import from: assistant
  2. src/assistant/ → may import from: core, stdlib, third-party packages
                       must NOT import from: aca
  3. src/core/    → may import from: stdlib, third-party packages only
                     must NOT import from: aca, assistant

Usage:
  python scripts/check_imports.py
  python scripts/check_imports.py --fix   (not implemented — reports only)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

# Each package's allowed internal imports (from other packages in src/)
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "core":       set(),                       # core imports nothing internal
    "aca":        {"core"},                    # aca imports core only
    "assistant":  {"core"},                    # assistant imports core only
}

# Top-level packages in src/ that are internal (not third-party)
INTERNAL_PACKAGES: set[str] = {"core", "aca", "assistant"}


# ── Core Logic ─────────────────────────────────────────────────────────────

def find_python_files(directory: Path) -> list[Path]:
    """Recursively find all .py files in a directory."""
    files = []
    for path in directory.rglob("*.py"):
        # Skip __pycache__
        if "__pycache__" in path.parts:
            continue
        files.append(path)
    return sorted(files)


def get_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Extract all top-level imports and their line numbers from an AST.

    Returns list of (module_name, line_number).
    Handles: import X, import X.Y, from X import Y, from X.Y import Z.
    """
    imports: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.module.split(".")[0], node.lineno))

    return imports


def classify_package(file_path: Path) -> str | None:
    """Determine which internal package a file belongs to.

    Returns 'core', 'aca', 'assistant', or None (not in src/).
    """
    relative = file_path.relative_to(SRC_DIR)
    top_level = relative.parts[0]
    return top_level if top_level in INTERNAL_PACKAGES else None


def check_file(file_path: Path) -> list[str]:
    """Check a single file for import violations.

    Returns a list of violation messages (empty if clean).
    """
    violations: list[str] = []

    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception:
        return [f"Cannot read {file_path}"]

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        violations.append(f"  Syntax error: {e}")
        return violations

    package = classify_package(file_path)
    if package is None:
        return violations  # Not in a tracked package, skip

    allowed = ALLOWED_IMPORTS.get(package, set())
    imports = get_imports(tree)

    for module_name, line_no in imports:
        # Only check internal packages
        if module_name not in INTERNAL_PACKAGES:
            continue

        # Self-imports are fine (e.g., aca.cli.main importing from aca.tools)
        if module_name == package:
            continue

        if module_name not in allowed:
            violations.append(
                f"  {file_path.relative_to(PROJECT_ROOT)}:{line_no}: "
                f"imports '{module_name}' — NOT ALLOWED. "
                f"'{package}' may only import from: {allowed or '(nothing internal)'}"
            )

    return violations


def main() -> int:
    """Run import checks across all packages. Returns exit code."""
    all_violations: list[str] = []
    files_checked = 0

    for package_name in INTERNAL_PACKAGES:
        package_dir = SRC_DIR / package_name
        if not package_dir.exists():
            continue

        py_files = find_python_files(package_dir)
        for file_path in py_files:
            files_checked += 1
            violations = check_file(file_path)
            if violations:
                all_violations.extend(violations)

    # ── Report ──
    print(f"Checked {files_checked} files across {len(INTERNAL_PACKAGES)} packages.\n")

    if all_violations:
        print(f"❌ {len(all_violations)} IMPORT VIOLATIONS FOUND:\n")
        for v in all_violations:
            print(v)
        print(f"\nRules:")
        for pkg, allowed in ALLOWED_IMPORTS.items():
            allowed_str = ", ".join(sorted(allowed)) if allowed else "(nothing internal)"
            print(f"  src/{pkg}/ → may import: {allowed_str}")
        return 1
    else:
        print("✅ All imports follow architectural boundaries.")
        print(f"   core/       imports from: (nothing internal)")
        print(f"   aca/        imports from: core")
        print(f"   assistant/  imports from: core")
        return 0


if __name__ == "__main__":
    sys.exit(main())
