"""Static checks of generated test modules. Code is parsed with ast, never run."""

import ast
from dataclasses import dataclass


# Statements that do not run anything at import time beyond defining names.
_DEFINITIONS = (
    ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
    ast.ClassDef, ast.Assign, ast.AnnAssign,
)


@dataclass(frozen=True)
class FileAnalysis:
    status: str                  # "ok", "no_tests" or "syntax_error"
    test_functions: int | None   # None when the file could not be parsed
    warnings: tuple[str, ...]
    error: str | None


def normalize_code(code: str) -> str:
    """Use \\n line endings and drop a leading BOM, as LLM output may have both."""
    return code.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _count_tests(tree: ast.Module) -> int:
    # Mirrors pytest defaults: test* functions, Test* classes with test* methods.
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += node.name.startswith("test")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            count += sum(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("test")
                for item in node.body
            )
    return count


def analyze_test_file(code: str, name: str) -> FileAnalysis:
    try:
        tree = ast.parse(code, filename=name)
    except (SyntaxError, ValueError) as exc:
        lineno = getattr(exc, "lineno", None)
        message = getattr(exc, "msg", None) or str(exc)
        error = f"line {lineno}: {message}" if lineno else message
        return FileAnalysis("syntax_error", None, (), error)

    body = tree.body[1:] if tree.body and _is_docstring(tree.body[0]) else tree.body
    warnings = (
        ("top_level_code",)
        if any(not isinstance(node, _DEFINITIONS) for node in body)
        else ()
    )
    tests = _count_tests(tree)
    return FileAnalysis("ok" if tests else "no_tests", tests, warnings, None)
