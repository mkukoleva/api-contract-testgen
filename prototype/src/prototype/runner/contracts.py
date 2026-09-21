"""Data exchanged with the pytest runner. No execution or network side effects.

URL validation checks input syntax only; it does not enforce network isolation.
The runner implementation must establish isolation before collecting tests.
"""

from dataclasses import dataclass, field
from enum import StrEnum
import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


class RunStatus(StrEnum):
    """Execution lifecycle, independent of individual assertion outcomes."""

    COMPLETED = "completed"
    COLLECTION_ERROR = "collection_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    TIMEOUT = "timeout"
    NO_TESTS = "no_tests"
    INTERRUPTED = "interrupted"


class TestOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


def _validate_seconds(value: float, name: str, *, positive: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        bound = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {bound} number")


def _validate_base_url(value: str) -> None:
    try:
        if (
            not isinstance(value, str)
            or not value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in "\\?#")
        ):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            "base_url must be an HTTP(S) URL with a host and valid port, "
            "without credentials, whitespace, query or fragment"
        ) from exc


@dataclass(frozen=True)
class RunConfig:
    """Trusted launcher input; generated tests must not choose this policy.

    Paths are not resolved, created or checked for existence here. None as
    base_url requests a unit-test run with no network access. A URL requests
    access to that service only, including when a URL path prefix is present.
    """

    tests_dir: Path | str
    output_dir: Path | str
    base_url: str | None = None
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in ("tests_dir", "output_dir"):
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{name} must be a non-empty path")
            object.__setattr__(self, name, Path(value))
        _validate_seconds(self.timeout_seconds, "timeout_seconds", positive=True)
        if self.base_url is not None:
            _validate_base_url(self.base_url)


@dataclass(frozen=True)
class TestResult:
    """One final result per pytest nodeid, including parametrization suffixes.

    Setup/teardown errors override a call outcome; keep the other phase details
    in message. A skipped test is not evidence that its body was executed.
    """

    nodeid: str
    outcome: TestOutcome | str
    duration_seconds: float = 0.0
    phase: Literal["setup", "call", "teardown"] = "call"
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.nodeid, str) or not self.nodeid.strip():
            raise ValueError("nodeid must be a non-empty pytest test identifier")
        object.__setattr__(self, "outcome", TestOutcome(self.outcome))
        _validate_seconds(self.duration_seconds, "duration_seconds")
        if self.phase not in {"setup", "call", "teardown"}:
            raise ValueError("phase must be setup, call or teardown")


@dataclass(frozen=True)
class RunResult:
    """Structured output; completed does not mean all assertions passed.

    exit_code is pytest's actual exit code, or None if it was not obtained.
    No runnability percentage is inferred from incomplete collection results.
    """

    status: RunStatus | str
    exit_code: int | None
    duration_seconds: float
    tests: tuple[TestResult, ...] = ()
    collection_errors: tuple[str, ...] = ()
    error_message: str | None = None
    report_paths: dict[str, Path | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RunStatus(self.status))
        object.__setattr__(self, "tests", tuple(self.tests))
        object.__setattr__(self, "collection_errors", tuple(self.collection_errors))
        _validate_seconds(self.duration_seconds, "duration_seconds")
        nodeids = [test.nodeid for test in self.tests]
        if len(nodeids) != len(set(nodeids)):
            raise ValueError("tests must contain exactly one final result per nodeid")

    def to_dict(self) -> dict:
        """Return a JSON-compatible payload without writing any files."""
        summary = {outcome.value: 0 for outcome in TestOutcome}
        for test in self.tests:
            summary[test.outcome.value] += 1
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "summary": {"total": len(self.tests), **summary},
            "tests": [
                {
                    "nodeid": test.nodeid,
                    "outcome": test.outcome.value,
                    "duration_seconds": test.duration_seconds,
                    "phase": test.phase,
                    "message": test.message,
                }
                for test in self.tests
            ],
            "collection_errors": list(self.collection_errors),
            "error_message": self.error_message,
            "report_paths": {name: str(path) for name, path in self.report_paths.items()},
        }
