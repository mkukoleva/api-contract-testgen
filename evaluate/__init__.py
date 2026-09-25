"""
Модуль оценки качества генерации тестов.

Содержит расчёт метрик из задания 2.1.7:
- Test Suite Size;
- Generation Time;
- Contract Coverage;
- Mutation Score;
- Runnability;
- LLM Calls;
- Tokens;
- Generation Cost.
"""

from .metrics import GenerationMetrics, calculate_metrics, save_metrics_report

__all__ = [
    "GenerationMetrics",
    "calculate_metrics",
    "save_metrics_report",
]
