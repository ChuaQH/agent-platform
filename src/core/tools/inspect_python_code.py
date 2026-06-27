"""Python code inspection tool — static analysis via AST, no execution.

Provides grounded structural facts: syntax validity, imports, functions,
classes, loops, recursion, suspicious patterns, dangerous operations,
and debugging hints.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Literal

from core.tool_framework import Tool

Focus = Literal["general", "complexity", "safety", "structure", "debugging"]
Detail = Literal["compact", "normal", "full"]

# ── Dangerous builtins / patterns ──────────────────────────────────────
_DANGEROUS_BUILTINS = {
    "eval", "exec", "compile", "__import__",
    "open", "input",
}
_DANGEROUS_MODULES = {
    "os", "subprocess", "shutil", "socket", "http", "urllib", "requests",
    "pickle", "shelve", "marshal",
    "ctypes", "cffi",
    "pty", "popen", "signal",
}
_DANGEROUS_ATTRS = {
    "os.system", "os.popen", "os.execv", "os.execve", "os.spawnl",
    "subprocess.call", "subprocess.Popen", "subprocess.run",
    "shutil.rmtree", "shutil.move", "shutil.copy",
    "socket.socket",
    "pickle.load", "pickle.loads",
    "eval", "exec", "compile",
}


class InspectPythonCodeTool(Tool):
    name = "inspect_python_code"
    description = (
        "Analyze Python source code without executing it. "
        "Returns syntax validity, imports, functions, classes, loops, "
        "recursion, suspicious patterns, dangerous operations, and debugging hints. "
        "Use focus=general|complexity|safety|structure|debugging and "
        "detail=compact|normal|full to control output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python source code to inspect.",
            },
            "focus": {
                "type": "string",
                "enum": ["general", "complexity", "safety", "structure", "debugging"],
                "description": "What aspect to focus on.",
            },
            "detail": {
                "type": "string",
                "enum": ["compact", "normal", "full"],
                "description": "How much detail to return.",
            },
        },
        "required": ["code"],
    }

    async def execute(self, code: str = "", focus: str = "general", detail: str = "normal", **kwargs: Any) -> str:
        if not code.strip():
            return json.dumps({"error": "No code provided."})

        result = _inspect(code, focus, detail)
        return json.dumps(result, indent=2, ensure_ascii=False)


# ── Core inspection logic ────────────────────────────────────────────────


def _inspect(code: str, focus: str, detail: str) -> dict[str, Any]:
    result: dict[str, Any] = {}

    # Parse
    try:
        tree = ast.parse(code)
        _set_parents(tree)
        result["syntax_valid"] = True
    except SyntaxError as e:
        result["syntax_valid"] = False
        result["syntax_error"] = {"line": e.lineno, "offset": e.offset, "message": e.msg}
        return result

    include = _includes(focus)

    if include["imports"]:
        result["imports"] = _collect_imports(tree)
    if include["functions"]:
        result["functions"] = _collect_functions(tree)
    if include["classes"]:
        result["classes"] = _collect_classes(tree)
    if include["loops"]:
        result["loops"] = _collect_loops(tree)
    if include["recursion"]:
        result["recursion"] = _collect_recursion(tree)
    if include["suspicious"]:
        result["suspicious_patterns"] = _collect_suspicious(tree, detail)
    if include["dangerous"]:
        dangerous = _collect_dangerous(tree)
        if dangerous or detail != "compact":
            result["dangerous_operations"] = dangerous
    if include["complexity"]:
        result["complexity_hints"] = _collect_complexity_hints(tree)
    if include["debugging"]:
        result["debugging_hints"] = _collect_debugging_hints(tree)

    # Full detail extras
    if detail == "full":
        result["line_count"] = len(code.splitlines())
        result["node_count"] = sum(1 for _ in ast.walk(tree))

    return result


def _includes(focus: str) -> dict[str, bool]:
    base = {"imports": True, "functions": True, "classes": True, "loops": True,
            "recursion": True, "suspicious": True, "dangerous": True,
            "complexity": True, "debugging": True}

    if focus == "general":
        return base
    if focus == "complexity":
        return {**base, "imports": False, "classes": False, "debugging": False,
                "dangerous": False}
    if focus == "safety":
        return {**base, "imports": False, "classes": False, "complexity": False,
                "functions": False, "debugging": False}
    if focus == "structure":
        return {**base, "complexity": False, "debugging": False, "dangerous": False,
                "suspicious": False}
    if focus == "debugging":
        return {**base, "classes": False, "complexity": False, "structure": False}
    return base


# ── Collectors ────────────────────────────────────────────────────────────


def _collect_imports(tree: ast.AST) -> list[str]:
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                imports.append(f"{mod}.{alias.name}" if mod else alias.name)
    return sorted(set(imports))


def _collect_functions(tree: ast.AST) -> list[dict[str, Any]]:
    funcs: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append({
                "name": node.name,
                "line": node.lineno,
                "args": [a.arg for a in node.args.args],
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": [_decorator_name(d) for d in node.decorator_list],
            })
    return funcs


def _collect_classes(tree: ast.AST) -> list[dict[str, Any]]:
    classes: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "bases": [_name_str(b) for b in node.bases],
                "methods": [
                    m.name for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
            })
    return classes


def _collect_loops(tree: ast.AST) -> dict[str, int]:
    for_count = 0
    while_count = 0
    max_nesting = 0
    for node in ast.walk(tree):
        nesting = _loop_nesting(node)
        max_nesting = max(max_nesting, nesting)
        if isinstance(node, ast.For):
            for_count += 1
        elif isinstance(node, ast.While):
            while_count += 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for_count += 1
    return {"for_count": for_count, "while_count": while_count, "max_nesting": max_nesting}


def _loop_nesting(node: ast.AST) -> int:
    depth = 0
    current = node
    while hasattr(current, "parent"):
        current = getattr(current, "parent", None)
        if current is None:
            break
        if isinstance(current, (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp)):
            depth += 1
    return depth


def _collect_recursion(tree: ast.AST) -> dict[str, Any]:
    func_calls: dict[str, set[str]] = {}
    func_nodes: dict[str, ast.AST] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_nodes[node.name] = node
            func_calls[node.name] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    func_calls[node.name].add(child.func.id)

    recursive = [
        name for name, calls in func_calls.items()
        if name in calls
    ]
    mutually_recursive: list[list[str]] = []
    seen: set[str] = set()
    for a in recursive:
        if a in seen:
            continue
        group = [a]
        seen.add(a)
        for b in recursive:
            if b != a and b not in seen:
                if a in func_calls.get(b, set()) and b in func_calls.get(a, set()):
                    group.append(b)
                    seen.add(b)
        if len(group) > 1:
            mutually_recursive.append(group)

    result: dict[str, Any] = {}
    if recursive:
        result["recursive_functions"] = recursive
    if mutually_recursive:
        result["mutually_recursive_groups"] = mutually_recursive

    return result


def _collect_suspicious(tree: ast.AST, detail: str) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        p = _check_suspicious_node(node, detail)
        if p:
            patterns.append(p)
    return patterns


def _check_suspicious_node(node: ast.AST, detail: str) -> dict[str, Any] | None:
    # O(n) list membership check inside a loop
    if isinstance(node, ast.In) and _inside_loop(node):
        return {"kind": "list_membership_check", "line": node.lineno,
                "message": "Linear list membership check inside a loop."}

    # List copy inside recursion
    if isinstance(node, ast.Call) and _is_list_copy(node):
        if _inside_recursive(node):
            return {"kind": "list_copy_in_recursion", "line": node.lineno,
                    "message": "List copy detected inside a recursive function."}

    # Bare except
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        return {"kind": "bare_except", "line": node.lineno,
                "message": "Bare except clause — catches all exceptions including KeyboardInterrupt."}

    # Mutable default argument
    if isinstance(node, ast.FunctionDef):
        for arg in node.args.defaults + node.args.kw_defaults:
            if isinstance(arg, (ast.List, ast.Dict, ast.Set)):
                return {"kind": "mutable_default", "line": node.lineno,
                        "message": f"Mutable default argument in function '{node.name}'."}

    # Overly broad exception
    if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "Exception":
        return {"kind": "broad_except", "line": node.lineno,
                "message": "Catching 'Exception' — prefer more specific exception types."}

    return None


def _inside_loop(node: ast.AST) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp)):
            return True
        parent = getattr(parent, "parent", None)
    return False


def _inside_recursive(node: ast.AST) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = set()
            for child in ast.walk(parent):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    calls.add(child.func.id)
            if parent.name in calls:
                return True
        parent = getattr(parent, "parent", None)
    return False


def _is_list_copy(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Attribute) and node.func.attr in ("copy", "deepcopy"):
        return True
    if isinstance(node.func, ast.Name) and node.func.id == "deepcopy":
        return True
    # list(something) pattern
    if isinstance(node.func, ast.Name) and node.func.id in ("list", "set", "dict", "tuple"):
        return True
    # [:] slice
    return False


def _collect_complexity_hints(tree: ast.AST) -> list[str]:
    hints: list[str] = []

    has_recursion = False
    has_nested_loops = False
    has_list_copy = False
    has_membership_in_loop = False
    has_sort = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    if child.func.id == node.name:
                        has_recursion = True
        if isinstance(node, (ast.For, ast.While)):
            if _loop_nesting(node) > 1:
                has_nested_loops = True
        if isinstance(node, ast.Call) and _is_list_copy(node):
            has_list_copy = True
        if isinstance(node, ast.In) and _inside_loop(node):
            has_membership_in_loop = True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "sort":
                has_sort = True
            elif isinstance(node.func, ast.Name) and node.func.id == "sorted":
                has_sort = True

    if has_recursion:
        hints.append("Recursive function detected.")
        if has_list_copy:
            hints.append("List copy inside recursion — may increase space complexity.")
    if has_nested_loops:
        hints.append("Nested loops detected — check for O(n²) or worse.")
    if has_membership_in_loop:
        hints.append("Membership check (x in list) inside loop — consider using a set.")
    if has_sort:
        hints.append("Sorting detected — typical O(n log n).")

    return hints


def _collect_dangerous(tree: ast.AST) -> list[dict[str, Any]]:
    dangerous: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        d = _check_dangerous_node(node)
        if d:
            dangerous.append(d)
    return dangerous


def _check_dangerous_node(node: ast.AST) -> dict[str, Any] | None:
    # Dangerous builtin calls
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _DANGEROUS_BUILTINS:
            return {"kind": "dangerous_builtin", "line": node.lineno,
                    "function": node.func.id,
                    "message": f"Calls '{node.func.id}' which can execute arbitrary code or access the filesystem."}

    # Dangerous module attribute access
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr = _attr_chain(node.func)
        if attr in _DANGEROUS_ATTRS:
            return {"kind": "dangerous_call", "line": node.lineno,
                    "function": attr,
                    "message": f"Calls '{attr}' which performs potentially dangerous operations."}

    # Dangerous imports
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _DANGEROUS_MODULES:
                return {"kind": "dangerous_import", "line": node.lineno,
                        "module": alias.name,
                        "message": f"Imports '{alias.name}' which can interact with the system, network, or files."}

    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        top = mod.split(".")[0] if mod else ""
        if top in _DANGEROUS_MODULES:
            return {"kind": "dangerous_import", "line": node.lineno,
                    "module": mod,
                    "message": f"Imports from '{mod}' which can interact with the system, network, or files."}

    return None


def _attr_chain(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _collect_debugging_hints(tree: ast.AST) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []

    defined_names: set[str] = set()
    used_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)
            for arg in node.args.args:
                defined_names.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined_names.add(node.id)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used_names.add(node.id)

    # Common LeetCode/platform names that are injected
    platform_names = {"List", "Optional", "ListNode", "TreeNode", "Node", "Solution",
                      "Counter", "defaultdict", "deque", "heapq", "bisect"}

    undefined = used_names - defined_names - platform_names - set(dir(__builtins__))
    if undefined:
        hints.append({
            "kind": "undefined_names",
            "names": sorted(undefined),
            "message": "Names used but not defined in the provided code."
        })

    # Unused imports
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)

    unused_imports = imported - used_names
    if unused_imports:
        hints.append({
            "kind": "unused_imports",
            "names": sorted(unused_imports),
            "message": "Imported modules that are not referenced in the code."
        })

    return hints


# ── AST helpers ────────────────────────────────────────────────────────────


def _set_parents(node: ast.AST, parent: ast.AST | None = None) -> None:
    """Walk the AST and set parent references on every node."""
    node.parent = parent  # type: ignore[attr-defined]
    for child in ast.iter_child_nodes(node):
        _set_parents(child, node)


def _name_str(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _attr_chain(node)
    if isinstance(node, ast.Constant):
        return str(node.value)
    return "..."


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _attr_chain(node)
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "..."

