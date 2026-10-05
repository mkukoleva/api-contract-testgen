"""
Пакет evaluate — модуль оценки качества тестов и API-контрактов.
"""

from prototype.evaluate.metrics import (
    GenerationMetrics,
    calculate_metrics,
    format_metrics_markdown,
    save_metrics_report,
)
from prototype.evaluate.runnability import (
    RAN_OUTCOMES,
    RunnabilityResult,
    compute_runnability,
    nodeid_matches,
)
from prototype.evaluate.mutation import (
    Mutant,
    MutationScoreResult,
    MutationType,
    calculate_mutation_score,
    count_mutants_by_operator,
    generate_mutants,
    mutate_remove_required,
    mutate_status_code_swap,
    mutate_type_change,
)

__all__ = [
    # Метрики качества (2.1.7)
    "GenerationMetrics",
    "calculate_metrics",
    "format_metrics_markdown",
    "save_metrics_report",
    # Запускаемость сгенерированных тестов (п. 2.1.2)
    "RunnabilityResult",
    "compute_runnability",
    "nodeid_matches",
    "RAN_OUTCOMES",
    # Операторы мутации контракта и оценка устойчивости тестов (2.2.9)
    "Mutant",
    "MutationScoreResult",
    "MutationType",
    "calculate_mutation_score",
    "count_mutants_by_operator",
    "generate_mutants",
    "mutate_remove_required",
    "mutate_status_code_swap",
    "mutate_type_change",
]
