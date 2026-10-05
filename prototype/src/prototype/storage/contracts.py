"""Data exchanged with the generated-tests storage. No side effects.

Creating these objects validates input only: nothing is read, written or run.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any


TEST_FILE_NAME = re.compile(r"test_[a-z0-9_]+\.py")


def _non_empty_path(value: Any, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must be a non-empty path")
    return Path(value)


@dataclass(frozen=True)
class GeneratedFile:
    """One pytest module produced by the generator after post-processing.

    Names come from an LLM and are untrusted: only flat test_*.py names are
    accepted, so a file can never escape the version directory. conftest.py is
    rejected because fixtures belong to the trusted runtime (ADR 0002).
    """

    name: str
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not TEST_FILE_NAME.fullmatch(self.name):
            raise ValueError(
                f"file name must match test_[a-z0-9_]+.py, got {self.name!r}"
            )
        if not isinstance(self.code, str):
            raise ValueError(f"code of {self.name} must be a string")


@dataclass(frozen=True)
class SaveRequest:
    """Everything save_test_suite needs to store one generation run.

    Saving defaults to static analysis without importing generated code.
    collect=True is a legacy opt-in for trusted, controlled fixtures only:
    collection imports modules on the host and provides no sandbox.
    """

    contract_path: Path | str
    files: tuple[GeneratedFile, ...]
    model: str | None = None
    generator_meta: Mapping[str, Any] = field(default_factory=dict)
    output_root: Path | str | None = None
    collect: bool = False
    collect_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_path", _non_empty_path(self.contract_path, "contract_path")
        )
        files = tuple(self.files)
        if not files or not all(isinstance(item, GeneratedFile) for item in files):
            raise ValueError("files must contain at least one GeneratedFile")
        names = [item.name for item in files]
        if len(names) != len(set(names)):
            raise ValueError("files must have unique names")
        object.__setattr__(self, "files", files)
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")
        if not isinstance(self.generator_meta, Mapping):
            raise ValueError("generator_meta must be a JSON-serializable mapping")
        try:
            meta = json.loads(json.dumps(dict(self.generator_meta), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("generator_meta must be a JSON-serializable mapping") from exc
        object.__setattr__(self, "generator_meta", meta)
        if self.output_root is not None:
            object.__setattr__(
                self, "output_root", _non_empty_path(self.output_root, "output_root")
            )
        if not isinstance(self.collect, bool):
            raise ValueError("collect must be a boolean")
        timeout = self.collect_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("collect_timeout_seconds must be a finite positive number")


@dataclass(frozen=True)
class SavedSuite:
    """A stored, immutable version; tests_dir is what RunConfig.tests_dir expects."""

    run_id: str
    run_dir: Path
    tests_dir: Path
    manifest_path: Path
    manifest: dict
