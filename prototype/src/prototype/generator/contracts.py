"""Settings and structured results of the generation + self-repair pipeline.

Creating these objects validates input only: nothing is read, written or run.
The module stays free of third-party imports so that the rest of the pipeline
can load it without side effects (same convention as prototype.storage).
"""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Any, Literal

# The team plan caps repair rounds at three (report_metrics.md: "≤3 attempts").
DEFAULT_MAX_REPAIR_ATTEMPTS = 3
ENV_MAX_REPAIR_ATTEMPTS = "TESTGEN_MAX_REPAIR_ATTEMPTS"

# Total budget for repaired output tokens, a natural limit of a repair loop.
DEFAULT_REPAIR_TOKEN_BUDGET = 30_000
ENV_REPAIR_TOKEN_BUDGET = "TESTGEN_REPAIR_TOKEN_BUDGET"

# Compact repair payload limits (ADR 0004): only the failing test fragment, a
# relevant contract fragment and shortened diagnostics reach the model.
DEFAULT_TEST_FRAGMENT_LINES = 30
DEFAULT_CONTRACT_FRAGMENT_CHARS = 1500
DEFAULT_DIAGNOSTIC_CHARS = 2000


def _str_path(value: Any, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must be a non-empty path")
    return Path(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return _positive_int(int(raw), name)
    except ValueError:
        return default


def _env_networks(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class GenerationSettings:
    """Everything the generation pipeline needs for one contract run.

    ``max_repair_attempts`` and ``repair_token_budget`` bound the self-repair
    loop; both can be overridden through the TESTGEN_*_ environment variables.
    ``base_url`` / ``network`` / ``blocked_networks`` follow the runner's
    service-mode contract (ADR 0002); ``None`` base_url requests unit mode.
    """

    contract_path: Path | str
    model: str | None = None
    base_url: str | None = None
    network: str | None = None
    blocked_networks: tuple[str, ...] = ()
    output_dir: Path | str | None = None
    output_root: Path | str | None = None
    runner_timeout_seconds: float = 60.0
    max_repair_attempts: int = field(
        default_factory=lambda: _env_positive_int(
            ENV_MAX_REPAIR_ATTEMPTS, DEFAULT_MAX_REPAIR_ATTEMPTS
        )
    )
    repair_token_budget: int = field(
        default_factory=lambda: _env_positive_int(
            ENV_REPAIR_TOKEN_BUDGET, DEFAULT_REPAIR_TOKEN_BUDGET
        )
    )
    test_fragment_lines: int = DEFAULT_TEST_FRAGMENT_LINES
    contract_fragment_chars: int = DEFAULT_CONTRACT_FRAGMENT_CHARS
    diagnostic_chars: int = DEFAULT_DIAGNOSTIC_CHARS

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_path", _str_path(self.contract_path, "contract_path")
        )
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")
        if self.base_url is not None and not isinstance(self.base_url, str):
            raise ValueError("base_url must be a string URL or None")
        if self.network is not None and not isinstance(self.network, str):
            raise ValueError("network must be a string network name or None")
        networks = tuple(self.blocked_networks)
        if not all(isinstance(item, str) and item.strip() for item in networks):
            raise ValueError("blocked_networks must contain non-empty strings")
        object.__setattr__(self, "blocked_networks", networks)
        if self.output_dir is not None:
            object.__setattr__(
                self, "output_dir", _str_path(self.output_dir, "output_dir")
            )
        if self.output_root is not None:
            object.__setattr__(
                self, "output_root", _str_path(self.output_root, "output_root")
            )
        timeout = self.runner_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("runner_timeout_seconds must be a finite positive number")
        object.__setattr__(
            self,
            "max_repair_attempts",
            _positive_int(self.max_repair_attempts, "max_repair_attempts"),
        )
        object.__setattr__(
            self,
            "repair_token_budget",
            _non_negative_int(self.repair_token_budget, "repair_token_budget"),
        )
        object.__setattr__(
            self,
            "test_fragment_lines",
            _non_negative_int(self.test_fragment_lines, "test_fragment_lines"),
        )
        object.__setattr__(
            self,
            "contract_fragment_chars",
            _non_negative_int(self.contract_fragment_chars, "contract_fragment_chars"),
        )
        object.__setattr__(
            self, "diagnostic_chars", _non_negative_int(self.diagnostic_chars, "diagnostic_chars")
        )


@dataclass(frozen=True)
class RepairAttempt:
    """One self-repair attempt logged by the pipeline."""

    index: int                      # 1-based repair call number
    target: str                     # file name or "file::function" identifier
    kind: Literal["file", "function"]
    error_signature: str
    outcome: str                    # repaired / stuck / budget_stopped / attempts_exhausted
    payload_chars: int = 0
    tokens_output: int = 0
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "target": self.target,
            "kind": self.kind,
            "error_signature": self.error_signature,
            "outcome": self.outcome,
            "payload_chars": self.payload_chars,
            "tokens_output": self.tokens_output,
            "message": self.message,
        }


@dataclass(frozen=True)
class GenerationRun:
    """Structured result of one pipeline run (generation + self-repair)."""

    status: str
    contract_path: Path
    model: str | None
    attempts: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tests: tuple[dict[str, Any], ...] = ()
    repair_log: tuple[RepairAttempt, ...] = ()
    suspected_defects: tuple[dict[str, Any], ...] = ()
    environmental: tuple[dict[str, Any], ...] = ()
    saved_versions: tuple[dict[str, Any], ...] = ()
    generator_errors: tuple[str, ...] = ()
    run_result: dict[str, Any] | None = None
    # Expected pytest nodeids of the final suite (runnability denominator).
    test_cases: tuple[str, ...] = ()
    # Runnability facts (prototype.evaluate.runnability), always a dict from
    # the pipeline; None only when constructed without calculation.
    runnability: dict[str, Any] | None = None
    # Paths of the JSON/Markdown generation report if one was saved.
    report_paths: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "contract_path": str(self.contract_path),
            "model": self.model,
            "attempts": self.attempts,
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "total": self.total_tokens,
            },
            "tests": list(self.tests),
            "repair_log": [attempt.to_dict() for attempt in self.repair_log],
            "suspected_defects": list(self.suspected_defects),
            "environmental": list(self.environmental),
            "saved_versions": list(self.saved_versions),
            "generator_errors": list(self.generator_errors),
            "run_result": self.run_result,
            "test_cases": list(self.test_cases),
            "runnability": self.runnability,
            "report_paths": self.report_paths,
        }
