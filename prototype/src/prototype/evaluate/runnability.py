"""Runnability: доля ожидаемых тестов генератора, реально запущенных.

Числитель и знаменатель берутся из разных источников:

- знаменатель — перечень ожидаемых pytest-nodeid, статически собранный
  постобработкой/хранилищем генератора (``FileAnalysis.test_cases``): это
  «список ожидаемых тестовых случаев от генератора» из плана пункта 2.1.2;
- числитель — nodeid, чьё тело действительно выполнилось в прогоне
  изолированного pytest-runner. Исход ``passed`` **и** ``failed`` доказывают
  выполнение тела: тест, обнаруживший дефект сервиса (например,
  недокументированный 500 на неизвестный товар), корректно даёт ``failed``
  и считается запустившимся. ``skipped`` и ``error`` запуском не считаются:
  skip не доказывает выполнение тела, error означает, что выполнение не
  доведено до assert.

Процент не выдаётся там, где он был бы недостоверен:

- знаменатель пуст/неизвестен — нечего делить;
- фактические результаты прогона отсутствуют (infrastructure) — числителя нет;
- статус прогона ``infrastructure_error`` — тесты не выполнялись вовсе;
- статус ``timeout``/``interrupted`` — результаты заведомо неполные, процент
  был бы занижен без возможности отличить «не запустился» от
  «не успели запустить».

Модуль использует только стандартную библиотеку, не выполняет тесты,
не обращается к LLM и не изменяет сервисы.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Исход, доказывающий выполнение тела теста. Падение assert (в том числе по
# дефекту сервиса) — это факт запуска, а не ошибка запускаемости.
RAN_OUTCOMES = frozenset({"passed", "failed"})

# Статусы, при которых фактических результатов нет или они заведомо неполные:
# инфраструктурная ошибка — тесты вообще не выполнялись; timeout/interrupted —
# выполнение прервано и часть тестов не успела стартовать.
UNRELIABLE_STATUSES = frozenset({"timeout", "interrupted", "infrastructure_error"})


def nodeid_matches(expected: str, actual: str) -> bool:
    """Static nodeid vs actual runner nodeid (may carry a ``[param]`` suffix)."""
    return actual == expected or actual.startswith(expected + "[")


@dataclass(frozen=True)
class RunnabilityResult:
    """Runnability facts of one generation run (JSON-compatible)."""

    expected_total: int
    ran_total: int
    ran_nodeids: tuple[str, ...]
    not_ran_nodeids: tuple[str, ...]
    runnability_percent: float | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_total": self.expected_total,
            "ran_total": self.ran_total,
            "ran_nodeids": list(self.ran_nodeids),
            "not_ran_nodeids": list(self.not_ran_nodeids),
            "runnability_percent": self.runnability_percent,
            "reason": self.reason,
        }


def compute_runnability(
    expected: Iterable[str],
    run_result: dict[str, Any] | None,
) -> RunnabilityResult:
    """Compute runnability from the generator's expected cases and runner facts.

    Args:
        expected: static pytest nodeids produced by the generator (denominator).
        run_result: ``RunResult.to_dict()`` of the final run, or None when the
            run never produced facts (infrastructure failure, interrupted).
    """
    expected = tuple(dict.fromkeys(expected))  # dedupe, keep order

    if not expected:
        return RunnabilityResult(
            0, 0, (), (), None,
            "нет ожидаемых тестовых случаев от генератора",
        )

    if run_result is None:
        return RunnabilityResult(
            len(expected), 0, (), tuple(expected), None,
            "нет фактических результатов прогона",
        )

    outcomes = {
        item.get("nodeid"): item.get("outcome")
        for item in (run_result.get("tests") or [])
        if isinstance(item, dict) and item.get("nodeid")
    }

    ran: list[str] = []
    not_ran: list[str] = []
    for nodeid in expected:
        matched = [
            outcome
            for actual, outcome in outcomes.items()
            if nodeid_matches(nodeid, actual)
        ]
        if matched and any(outcome in RAN_OUTCOMES for outcome in matched):
            ran.append(nodeid)
        else:
            not_ran.append(nodeid)

    status = run_result.get("status")
    if status in UNRELIABLE_STATUSES:
        reason = (
            "тесты не выполнялись (infrastructure_error)"
            if status == "infrastructure_error"
            else f"результаты прогона неполные ({status})"
        )
        percent = None
    else:
        percent = round(len(ran) / len(expected) * 100, 2)
        reason = None

    return RunnabilityResult(
        expected_total=len(expected),
        ran_total=len(ran),
        ran_nodeids=tuple(ran),
        not_ran_nodeids=tuple(not_ran),
        runnability_percent=percent,
        reason=reason,
    )
