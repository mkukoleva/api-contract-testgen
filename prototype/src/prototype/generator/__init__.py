"""Real LLM test generation and limited self-repair (ТЗ 2.1.5, 2.1.2).

The pipeline generates pytest modules from an API contract, runs them through
the isolated pytest-runner, and repairs only errors that are attributable to
the generated code. Infrastructure failures, timeouts and suspected service
defects never trigger an LLM repair call (ADR 0004).
"""

from .contracts import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    DEFAULT_REPAIR_TOKEN_BUDGET,
    GenerationRun,
    GenerationSettings,
    RepairAttempt,
)
from .pipeline import run_generation_pipeline

__all__ = [
    "DEFAULT_MAX_REPAIR_ATTEMPTS",
    "DEFAULT_REPAIR_TOKEN_BUDGET",
    "GenerationRun",
    "GenerationSettings",
    "RepairAttempt",
    "run_generation_pipeline",
]
