"""
Расчёт метрик качества генерации тестов.

Метрики:
- Test Suite Size
- Generation Time
- Contract Coverage
- Mutation Score
- Runnability
- LLM Calls
- Input Tokens
- Output Tokens
- Total Tokens
- Generation Cost

Модуль не выполняет тесты и не обращается к LLM самостоятельно.
Он получает уже готовый результат работы агента и рассчитывает
метрики на его основе.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class GenerationMetrics:
    """Набор метрик одного запуска генератора."""

    test_suite_size: int | None = None

    generation_time_seconds: float | None = None

    contract_operations_total: int | None = None
    contract_operations_covered: int | None = None
    contract_coverage_percent: float | None = None

    mutation_score_percent: float | None = None

    runnable_tests: int | None = None
    total_generated_tests: int | None = None
    runnability_percent: float | None = None

    llm_calls: int = 0

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    generation_cost: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать метрики в JSON-совместимый словарь."""
        return asdict(self)


def _extract_content(message: Any) -> Any:
    """
    Получить content из LangChain-сообщения.

    Content может быть:
    - строкой;
    - словарём;
    - списком блоков;
    - другим JSON-совместимым объектом.
    """

    content = getattr(message, "content", message)

    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    return content


def _extract_tool_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Извлечь результаты вызовов tools из результата LangChain agent.invoke().
    """

    messages = result.get("messages", [])

    tool_results: list[dict[str, Any]] = []

    for message in messages:
        message_type = message.__class__.__name__

        if message_type != "ToolMessage":
            continue

        content = _extract_content(message)

        if isinstance(content, dict):
            tool_results.append(content)

        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    tool_results.append(item)

    return tool_results


def _count_llm_calls(result: dict[str, Any]) -> int:
    """
    Посчитать количество ответов языковой модели.

    В LangChain каждый AIMessage соответствует одному ответу модели.
    """

    messages = result.get("messages", [])

    return sum(
        1
        for message in messages
        if message.__class__.__name__ == "AIMessage"
    )


def _extract_token_usage(result: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """
    Собрать статистику токенов из usage_metadata AIMessage.

    Поддерживаются стандартные поля LangChain:
    input_tokens
    output_tokens
    total_tokens
    """

    messages = result.get("messages", [])

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0

    found_usage = False

    for message in messages:
        if message.__class__.__name__ != "AIMessage":
            continue

        usage = getattr(message, "usage_metadata", None)

        if not usage:
            continue

        found_usage = True

        input_tokens += int(usage.get("input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        total_tokens += int(
            usage.get(
                "total_tokens",
                (usage.get("input_tokens", 0) or 0)
                + (usage.get("output_tokens", 0) or 0),
            )
            or 0
        )

    if not found_usage:
        return None, None, None

    return input_tokens, output_tokens, total_tokens


def _calculate_generation_cost(
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """
    Рассчитать стоимость обращений к LLM.

    Стоимость задаётся через переменные окружения:

    DEEPCODE_INPUT_COST_PER_1K
    DEEPCODE_OUTPUT_COST_PER_1K

    Например:

    DEEPCODE_INPUT_COST_PER_1K=0.001
    DEEPCODE_OUTPUT_COST_PER_1K=0.002

    Если цены не заданы, стоимость возвращается как None.
    Это важно: стоимость нельзя придумывать.
    """

    if input_tokens is None or output_tokens is None:
        return None

    input_price = os.getenv("DEEPCODE_INPUT_COST_PER_1K")
    output_price = os.getenv("DEEPCODE_OUTPUT_COST_PER_1K")

    if input_price is None or output_price is None:
        return None

    try:
        input_cost_per_1k = float(input_price)
        output_cost_per_1k = float(output_price)
    except ValueError:
        return None

    input_cost = input_tokens / 1000 * input_cost_per_1k
    output_cost = output_tokens / 1000 * output_cost_per_1k

    return round(input_cost + output_cost, 6)


def _extract_schemathesis_result(
    tool_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Найти результат schemathesis_tool."""

    for result in tool_results:
        if result.get("tool") == "schemathesis_tool":
            return result

        if result.get("service") == "schemathesis":
            return result

    return None


def _extract_generated_test_count(
    tool_results: list[dict[str, Any]],
) -> int | None:
    """
    Определить размер тестового набора.

    В текущей версии прототипа Schemathesis сообщает количество
    сгенерированных сценариев через scenarios.total.

    Если инструмент генерации тестов вернёт test_suite_size,
    оно также будет поддержано.
    """

    for result in tool_results:
        if "test_suite_size" in result:
            try:
                return int(result["test_suite_size"])
            except (TypeError, ValueError):
                pass

        scenarios = result.get("scenarios")

        if isinstance(scenarios, dict) and "total" in scenarios:
            try:
                return int(scenarios["total"])
            except (TypeError, ValueError):
                pass

    return None


def _calculate_contract_coverage(
    contract_operations: int | None,
    schemathesis_result: dict[str, Any] | None,
) -> tuple[int | None, float | None]:
    """
    Рассчитать покрытие контракта на уровне HTTP-операций.

    Формула:

        covered_operations / total_operations * 100

    Для текущей интеграции Schemathesis считает все операции контракта,
    переданные движку. Поэтому при успешном запуске число покрытых
    операций берётся из поля operations.

    Если данных недостаточно, возвращается None.
    """

    if contract_operations is None:
        return None, None

    if schemathesis_result is None:
        return None, None

    status = schemathesis_result.get("status")

    if status != "success":
        return None, None

    covered = schemathesis_result.get("operations")

    if covered is None:
        return None, None

    try:
        covered = int(covered)
    except (TypeError, ValueError):
        return None, None

    if contract_operations <= 0:
        return None, None

    covered = min(covered, contract_operations)

    coverage = covered / contract_operations * 100

    return covered, round(coverage, 2)


def calculate_metrics(
    *,
    agent_result: dict[str, Any],
    contract_summary: dict[str, Any],
    generation_time_seconds: float,
) -> GenerationMetrics:
    """
    Рассчитать все доступные метрики по одному запуску.

    Args:
        agent_result:
            Результат agent.invoke(...).

        contract_summary:
            Краткое описание OpenAPI-контракта.

        generation_time_seconds:
            Время работы agent.invoke().

    Returns:
        GenerationMetrics.
    """

    tool_results = _extract_tool_results(agent_result)

    schemathesis_result = _extract_schemathesis_result(tool_results)

    total_operations = contract_summary.get("operation_count")

    if total_operations is not None:
        try:
            total_operations = int(total_operations)
        except (TypeError, ValueError):
            total_operations = None

    covered_operations, contract_coverage = _calculate_contract_coverage(
        total_operations,
        schemathesis_result,
    )

    test_suite_size = _extract_generated_test_count(tool_results)

    llm_calls = _count_llm_calls(agent_result)

    input_tokens, output_tokens, total_tokens = _extract_token_usage(
        agent_result
    )

    generation_cost = _calculate_generation_cost(
        input_tokens,
        output_tokens,
    )

    # Mutation Score пока не рассчитывается автоматически:
    # отдельный mutation testing runner в текущем прототипе
    # ещё не подключён.
    mutation_score = None

    # Аналогично Runnability:
    # полноценный pytest-runner пока не подключён к агенту.
    runnability = None
    runnable_tests = None
    total_generated_tests = test_suite_size

    return GenerationMetrics(
        test_suite_size=test_suite_size,

        generation_time_seconds=round(
            generation_time_seconds,
            3,
        ),

        contract_operations_total=total_operations,
        contract_operations_covered=covered_operations,
        contract_coverage_percent=contract_coverage,

        mutation_score_percent=mutation_score,

        runnable_tests=runnable_tests,
        total_generated_tests=total_generated_tests,
        runnability_percent=runnability,

        llm_calls=llm_calls,

        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,

        generation_cost=generation_cost,
        currency="USD",
    )


def save_metrics_report(
    metrics: GenerationMetrics,
    output_dir: Path,
) -> Path:
    """
    Сохранить метрики в JSON-файл.

    Returns:
        Путь к созданному JSON-файлу.
    """

    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()

    report_path = output_dir / (
        f"metrics_{now.strftime('%Y-%m-%d_%H%M%S')}.json"
    )

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics.to_dict(),
    }

    report_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return report_path


def format_metrics_markdown(metrics: GenerationMetrics) -> str:
    """
    Сформировать читаемый Markdown-блок с метриками.
    """

    def value(value: Any, suffix: str = "") -> str:
        if value is None:
            return "N/A"

        return f"{value}{suffix}"

    lines = [
        "## Метрики качества генерации",
        "",
        "| Метрика | Значение |",
        "|---|---:|",
        f"| Test Suite Size | {value(metrics.test_suite_size)} |",
        (
            "| Generation Time | "
            f"{value(metrics.generation_time_seconds, ' с')} |"
        ),
        (
            "| Contract Coverage | "
            f"{value(metrics.contract_coverage_percent, ' %')} |"
        ),
        (
            "| Mutation Score | "
            f"{value(metrics.mutation_score_percent, ' %')} |"
        ),
        (
            "| Runnability | "
            f"{value(metrics.runnability_percent, ' %')} |"
        ),
        f"| LLM Calls | {metrics.llm_calls} |",
        f"| Input Tokens | {value(metrics.input_tokens)} |",
        f"| Output Tokens | {value(metrics.output_tokens)} |",
        f"| Total Tokens | {value(metrics.total_tokens)} |",
        (
            "| Generation Cost | "
            f"{value(metrics.generation_cost, ' ' + metrics.currency)} |"
        ),
        "",
    ]

    if metrics.mutation_score_percent is None:
        lines.extend(
            [
                "> Mutation Score: N/A — "
                "mutation testing runner ещё не подключён.",
                "",
            ]
        )

    if metrics.runnability_percent is None:
        lines.extend(
            [
                "> Runnability: N/A — "
                "pytest-runner ещё не подключён к циклу генерации.",
                "",
            ]
        )

    if metrics.generation_cost is None:
        lines.extend(
            [
                "> Generation Cost: N/A — "
                "цены токенов не заданы через DEEPCODE_*_COST_PER_1K.",
                "",
            ]
        )

    return "\n".join(lines)
