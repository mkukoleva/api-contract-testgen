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
from ..parser.contract_input import ContractDocument, normalize_location
from ..service_tools.runner.contracts import _validate_base_url

# Default only: TZ v3 4.1.7.1 requires a configurable repair limit.
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
# Schemathesis enrichment budget: the microservice stops the run at this
# max_time; the HTTP client timeout adds its own margin on top.
DEFAULT_SCHEMATHESIS_TIMEOUT_SECONDS = 60.0
MAX_SCHEMATHESIS_TIMEOUT_SECONDS = 600.0


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
        return _non_negative_int(int(raw), name)
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
    # Whole-contract summary budget for the generation prompt (TZ v3 4.1.2.2).
    # Character-based enforcement unit: 8000 chars ≈ 2-2.5k tokens for this
    # mostly-Cyrillic rendering; the actual prompt tokens are measured from
    # the provider usage and recorded on the run.
    summary_max_chars: int = 8000
    source_code_path: Path | str | None = None
    # Optional context enrichment with a deterministic Schemathesis run
    # (TZ v3 4.1.2.3). The digest is diagnostic context only: the contract
    # remains the sole source of expected values. The service address falls
    # back to SCHEMATHESIS_SERVICE_URL, then http://127.0.0.1:8080.
    enrich_with_schemathesis: bool = True
    schemathesis_service_url: str | None = None
    schemathesis_timeout_seconds: float = DEFAULT_SCHEMATHESIS_TIMEOUT_SECONDS
    input_document: ContractDocument | None = field(default=None, repr=False, compare=False)
    source_context: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_path", normalize_location(self.contract_path)
        )
        if self.source_code_path is not None:
            object.__setattr__(self, "source_code_path", _str_path(self.source_code_path, "source_code_path"))
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")
        if self.base_url is not None:
            _validate_base_url(self.base_url)
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
            _non_negative_int(self.max_repair_attempts, "max_repair_attempts"),
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
        object.__setattr__(
            self, "summary_max_chars", _non_negative_int(self.summary_max_chars, "summary_max_chars")
        )
        if not isinstance(self.enrich_with_schemathesis, bool):
            raise ValueError("enrich_with_schemathesis must be a bool")
        if self.schemathesis_service_url is not None and (
                not isinstance(self.schemathesis_service_url, str)
                or not self.schemathesis_service_url.strip()):
            raise ValueError("schemathesis_service_url must be a non-empty URL or None")
        timeout = self.schemathesis_timeout_seconds
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 0 < timeout <= MAX_SCHEMATHESIS_TIMEOUT_SECONDS):
            raise ValueError(
                f"schemathesis_timeout_seconds must be a finite number in "
                f"(0, {MAX_SCHEMATHESIS_TIMEOUT_SECONDS}]")


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
    contract_path: Path | str
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
    # Additive provenance; metrics and existing consumer fields are unchanged.
    provenance: dict[str, Any] = field(default_factory=dict)
    recommendations: dict[str, Any] = field(default_factory=dict)

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
            "provenance": self.provenance,
            "recommendations": self.recommendations,
        }
