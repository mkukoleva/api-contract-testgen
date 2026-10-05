"""Host-side pytest compatibility checks. Standard library only.

``precheck_syntax`` parses every Python file in the tests directory without
importing or running it, so a broken file is reported with an exact location
(path:line:column) before any Docker container is created. Issues that match
unambiguous "obvious fix" patterns carry ``auto_fixable=True``: those are the
structured input for the post-processing module, which is outside the scope
of this stage.

The module does not import the agent, pytest or third-party packages, so it
can be exercised under ``python -I -S`` like ``contracts``.
"""

import ast
from pathlib import Path
import tokenize

from .contracts import CompatibilityIssue, IssueCategory

# Substrings of SyntaxError.msg that indicate a clearly truncated or
# single-token broken fragment. This is the conservative set of "obvious
# fixes" handed to the post-processing module; everything else is left for
# the generator to rewrite.
_AUTOFIXABLE_MARKERS = (
    "unexpected eof",
    "unterminated string literal",
    "missing parentheses",
    "was never closed",
)


def precheck_syntax(tests_dir: Path | str) -> tuple[CompatibilityIssue, ...]:
    """Return syntax issues for every ``*.py`` under tests_dir.

    Reads and parses files only; never imports or executes them. Files are
    checked in sorted order for deterministic output.
    """
    root = Path(tests_dir)
    if not root.is_dir():
        raise ValueError(f"tests_dir must be a directory: {root}")
    issues: list[CompatibilityIssue] = []
    for path in sorted(root.rglob("*.py")):
        _check_file(path, issues)
    return tuple(issues)


def _check_file(path: Path, issues: list[CompatibilityIssue]) -> None:
    try:
        with tokenize.open(path) as stream:
            source = stream.read()
    except (UnicodeError, SyntaxError, OSError) as exc:
        issues.append(CompatibilityIssue(
            IssueCategory.SYNTAX, str(path),
            f"cannot decode Python source (UTF-8 unless an encoding is declared): {exc}", auto_fixable=False))
        return
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        issues.append(CompatibilityIssue(
            IssueCategory.SYNTAX, str(path),
            exc.msg or "invalid syntax",
            line=exc.lineno, column=exc.offset,
            auto_fixable=_auto_fixable(exc.msg or "")))


def _auto_fixable(message: str) -> bool:
    return any(marker in message.lower() for marker in _AUTOFIXABLE_MARKERS)
