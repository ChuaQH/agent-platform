"""Calculator tool — safe mathematical expression evaluation."""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from core.tool_framework import Tool, ToolCapability

# Allowed operators and functions (no eval, no __import__, etc.)
_ALLOWED_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_ALLOWED_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "pow": math.pow,
    "ceil": math.ceil,
    "floor": math.floor,
    "pi": math.pi,
    "e": math.e,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.UAdd,
    ast.Constant,
    ast.Call,
    ast.Name,
    ast.Load,
)


class CalculatorTool(Tool):
    """Evaluate mathematical expressions safely."""

    name = "calculator"
    capabilities = frozenset({ToolCapability.COMPUTE})
    description = (
        "Evaluate a mathematical expression safely. "
        "Supports basic arithmetic, trigonometry, logarithms, and constants (pi, e). "
        "Examples: 'sqrt(144) + 3*5', 'sin(pi/2)', 'log(100, 10)'"
    )
    when_to_use = "For mathematical calculations that are best done precisely rather than mentally."
    how_to_use = "Provide a mathematical expression (e.g., 'sqrt(144) + 3*5')."
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The mathematical expression to evaluate.",
            },
        },
        "required": ["expression"],
    }

    async def execute(self, expression: str, **kwargs: Any) -> str:
        """Evaluate a mathematical expression safely."""
        try:
            # Parse the expression
            tree = ast.parse(expression.strip(), mode="eval")

            # Validate — only allow safe nodes
            self._validate(tree)

            # Evaluate
            result = self._eval(tree.body)

            # Format
            if isinstance(result, float):
                if result == int(result):
                    return f"{expression} = {int(result)}"
                return f"{expression} = {result:.10g}"

            return f"{expression} = {result}"

        except SyntaxError as e:
            return f"Syntax error in expression: {e}"
        except (ValueError, ZeroDivisionError) as e:
            return f"Math error: {e}"
        except Exception as e:
            return f"Error evaluating expression: {e}"

    def _validate(self, tree: ast.AST) -> None:
        """Ensure only safe AST nodes are present."""
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ValueError(
                    f"Disallowed operation: {type(node).__name__}"
                )

            if isinstance(node, ast.Call):
                func_name = node.func.id if isinstance(node.func, ast.Name) else None
                if func_name not in _ALLOWED_FUNCTIONS:
                    raise ValueError(f"Disallowed function: {func_name}")

    def _eval(self, node: ast.AST) -> float | int:
        """Recursively evaluate a safe AST node."""
        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPS:
                raise ValueError(f"Disallowed operator: {op_type.__name__}")
            return _ALLOWED_OPS[op_type](left, right)

        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand)
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPS:
                raise ValueError(f"Disallowed unary operator: {op_type.__name__}")
            return _ALLOWED_OPS[op_type](operand)

        if isinstance(node, ast.Call):
            func_name = node.func.id if isinstance(node.func, ast.Name) else None
            if func_name not in _ALLOWED_FUNCTIONS:
                raise ValueError(f"Disallowed function: {func_name}")
            args = [self._eval(arg) for arg in node.args]
            return _ALLOWED_FUNCTIONS[func_name](*args)

        if isinstance(node, ast.Name):
            if node.id in _ALLOWED_FUNCTIONS:
                val = _ALLOWED_FUNCTIONS[node.id]
                if callable(val):
                    raise ValueError(f"'{node.id}' is a function, not a value")
                return val
            raise ValueError(f"Unknown name: {node.id}")

        raise ValueError(f"Cannot evaluate: {type(node).__name__}")
