"""Deterministic post-processing of generated test files before they run.

The LLM output is untrusted: every file is normalised (line endings, BOM) and
statically analysed with ast without executing it. Files with a syntax error
are not dropped here — the repair loop needs them to ask the model for a fixed
version (ADR 0004), so they stay in the attempt directory and are flagged.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import GeneratedFile
from ..storage.analysis import analyze_test_file, normalize_code

Status = str  # "ok" | "no_tests" | "syntax_error"


@dataclass(frozen=True)
class PreparedFile:
    """One normalised, statically checked generated module."""

    name: str
    code: str
    status: Status
    test_functions: int | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "test_functions": self.test_functions,
            "error": self.error,
        }


def prepare_file(name: str, code: str) -> PreparedFile:
    """Normalise and statically check one file; never executes the code."""
    normalised = normalize_code(code)
    analysis = analyze_test_file(normalised, name)
    return PreparedFile(
        name=name,
        code=normalised,
        status=analysis.status,
        test_functions=analysis.test_functions,
        error=analysis.error,
    )


def prepare_files(files: tuple[GeneratedFile, ...]) -> tuple[PreparedFile, ...]:
    return tuple(prepare_file(item.name, item.code) for item in files)


def as_generated_files(files: tuple[PreparedFile, ...]) -> tuple[GeneratedFile, ...]:
    """Back the prepared files into the storage boundary type."""
    return tuple(GeneratedFile(item.name, item.code) for item in files)


def write_attempt_dir(
    files: tuple[PreparedFile, ...], tests_dir: Path
) -> Path:
    """Write the full prepared set (including syntax-error files) to tests_dir.

    The runner gets this directory as RunConfig.tests_dir; syntax-error files
    are kept so the runner's precheck reports them and repair can fix them.
    """
    tests_dir.mkdir(parents=True, exist_ok=True)
    for item in files:
        (tests_dir / item.name).write_text(item.code, encoding="utf-8")
    return tests_dir
