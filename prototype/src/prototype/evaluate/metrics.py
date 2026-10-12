"""
Расчёт метрик качества генерации тестов.

Метрики:
- Test Suite Size
- Generation Time
- Contract Coverage
- Mutation Score
- Runnability
- Tests without manual edits
- LLM Calls
- Input Tokens
- Output Tokens
- Total Tokens
- Generation Cost
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
    test_suite_size: int | None = None

    generation_time_seconds: float | None = None

    contract_operations_total: int | None = None2.1.7
    contract_operations_covered: int | None = None
    contract_coverage_percent: float | None = None

    mutation_score_percent: float | None = None

    runnable_tests: int | None = None
    total_generated_tests: int | None = None
    runnability_percent: float | None = None

    tests_without_manual_edits: int | None = None
    tests_without_manual_edits_percent: float | None = None

    llm_calls: int = 0

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    generation_cost: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_content(message: Any) -> Any:
    content = getattr(message, "content", message)

    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    return content


def _extract_tool_results(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    messages = result.get("messages", [])

    tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.__class__.__name__ != "ToolMessage":
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
    messages = result.get("messages", [])

    return sum(
        1
        for message in messages
        if message.__class__.__name__ == "AIMessage"
    )


def _extract_token_usage(
    result: dict[str, Any],
) -> tuple[int | None, int | None, int | None]:

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

        in_tokens = int(
            usage.get("input_tokens", 0) or 0
        )

        out_tokens = int(
            usage.get("output_tokens", 0) or 0
        )

        total = usage.get("total_tokens")

        if total is None:
            total = in_tokens + out_tokens

        input_tokens += in_tokens
        output_tokens += out_tokens
        total_tokens += int(total or 0)

    if not found_usage:
        return None, None, None

    return input_tokens, output_tokens, total_tokens


def _calculate_generation_cost(
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:

    if input_tokens is None or output_tokens is None:
        return None

    input_price = os.getenv(
        "DEEPCODE_INPUT_COST_PER_1K"
    )

    output_price = os.getenv(
        "DEEPCODE_OUTPUT_COST_PER_1K"
    )

    if input_price is None or output_price is None:
        return None

    try:
        input_price = float(input_price)
        output_price = float(output_price)
    except ValueError:
        return None

    cost = (
        input_tokens / 1000 * input_price
        + output_tokens / 1000 * output_price
    )

    return round(cost, 6)


def _find_generation_result(
    tool_results: list[dict[str, Any]],
) -> dict[str, Any] | None:

    for result in tool_results:

        if result.get("tool") == "generate_tests_tool":
            return result

        if "pipeline_status" in result:
            return result

        if "test_suite_size" in result:
            return result

        if "generated_tests" in result:
            return result

    return None


def _extract_test_suite_size(
    tool_results: list[dict[str, Any]],
) -> int | None:

    result = _find_generation_result(tool_results)

    if result is None:
        return None

    possible_fields = (
        "test_suite_size",
        "generated_tests",
        "tests_count",
        "test_count",
    )

    for field in possible_fields:

        value = result.get(field)

        if isinstance(value, int):
            return value

        if isinstance(value, list):
            return len(value)

        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass

    return None


def _extract_runnability(
    tool_results: list[dict[str, Any]],
    test_suite_size: int | None,
) -> tuple[int | None, float | None]:

    result = _find_generation_result(tool_results)

    if result is None:
        return None, None

    runnable = result.get("runnable_tests")

    if runnable is None:
        runnable = result.get("passed_tests")

    total = result.get("total_generated_tests")

    if total is None:
        total = test_suite_size

    try:
        runnable = int(runnable) if runnable is not None else None
    except (TypeError, ValueError):
        runnable = None

    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = test_suite_size

    if runnable is None or total is None or total <= 0:
        return None, None

    runnable = max(0, min(runnable, total))

    percent = runnable / total * 100

    return runnable, round(percent, 2)


def _extract_manual_edit_metric(
    tool_results: list[dict[str, Any]],
    test_suite_size: int | None,
) -> tuple[int | None, float | None]:

    result = _find_generation_result(tool_results)

    if result is None:
        return None, None

    value = result.get("tests_without_manual_edits")

    if value is None:
        value = result.get("manual_edit_free_tests")

    if value is None:
        value = result.get("tests_without_edits")

    if value is None:
        return None, None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None, None

    if test_suite_size is None or test_suite_size <= 0:
        return value, None

    value = max(0, min(value, test_suite_size))

    percent = value / test_suite_size * 100

    return value, round(percent, 2)


def _extract_schemathesis_result(
    tool_results: list[dict[str, Any]],
) -> dict[str, Any] | None:

    for result in tool_results:

        if result.get("tool") == "schemathesis_tool":
            return result

        if result.get("service") == "schemathesis":
            return result

    return None


def _calculate_contract_coverage(
    contract_operations: int | None,
    schemathesis_result: dict[str, Any] | None,
    generation_result: dict[str, Any] | None,
) -> tuple[int | None, float | None]:

    if contract_operations is None:
        return None, None

    covered = None

    if generation_result is not None:

        covered = generation_result.get(
            "contract_operations_covered"
        )

        if covered is None:
            covered = generation_result.get(
                "covered_operations"
            )

    if covered is None and schemathesis_result is not None:

        covered = schemathesis_result.get("operations")

    if covered is None:
        return None, None

    try:
        covered = int(covered)
    except (TypeError, ValueError):
        return None, None

    if contract_operations <= 0:
        return None, None

    covered = min(
        max(covered, 0),
        contract_operations,
    )

    coverage = (
        covered / contract_operations * 100
    )

    return covered, round(coverage, 2)


def calculate_metrics(
    *,
    agent_result: dict[str, Any],
    contract_summary: dict[str, Any],
    generation_time_seconds: float,
    mutation_score_percent: float | None = None,
) -> GenerationMetrics:

    tool_results = _extract_tool_results(
        agent_result
    )

    schemathesis_result = _extract_schemathesis_result(
        tool_results
    )

    generation_result = _find_generation_result(
        tool_results
    )

    total_operations = contract_summary.get(
        "operation_count"
    )

    try:
        total_operations = (
            int(total_operations)
            if total_operations is not None
            else None
        )
    except (TypeError, ValueError):
        total_operations = None

    test_suite_size = _extract_test_suite_size(
        tool_results
    )

    covered_operations, coverage = (
        _calculate_contract_coverage(
            total_operations,
            schemathesis_result,
            generation_result,
        )
    )

    runnable_tests, runnability = _extract_runnability(
        tool_results,
        test_suite_size,
    )

    (
        tests_without_edits,
        tests_without_edits_percent,
    ) = _extract_manual_edit_metric(
        tool_results,
        test_suite_size,
    )

    llm_calls = _count_llm_calls(
        agent_result
    )

    input_tokens, output_tokens, total_tokens = (
        _extract_token_usage(agent_result)
    )

    generation_cost = _calculate_generation_cost(
        input_tokens,
        output_tokens,
    )

    mutation_score = mutation_score_percent

    if mutation_score is None:
        mutation_score = agent_result.get(
            "mutation_score_percent"
        )

    return GenerationMetrics(

        test_suite_size=test_suite_size,

        generation_time_seconds=round(
            generation_time_seconds,
            3,
        ),

        contract_operations_total=total_operations,

        contract_operations_covered=covered_operations,

        contract_coverage_percent=coverage,

        mutation_score_percent=mutation_score,

        runnable_tests=runnable_tests,

        total_generated_tests=test_suite_size,

        runnability_percent=runnability,

        tests_without_manual_edits=tests_without_edits,

        tests_without_manual_edits_percent=(
            tests_without_edits_percent
        ),

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

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    now = datetime.now()

    report_path = output_dir / (
        f"metrics_{now.strftime('%Y-%m-%d_%H%M%S')}.json"
    )

    payload = {
        "generated_at": now.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
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


def format_metrics_markdown(
    metrics: GenerationMetrics,
) -> str:

    def value(
        item: Any,
        suffix: str = "",
    ) -> str:

        if item is None:
            return "N/A"

        return f"{item}{suffix}"

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
        (
            "| Tests without manual edits | "
            f"{value(metrics.tests_without_manual_edits_percent, ' %')} |"
        ),
        f"| LLM Calls | {metrics.llm_calls} |",
        (
            "| Input Tokens | "
            f"{value(metrics.input_tokens)} |"
        ),
        (
            "| Output Tokens | "
            f"{value(metrics.output_tokens)} |"
        ),
        (
            "| Total Tokens | "
            f"{value(metrics.total_tokens)} |"
        ),
        (
            "| Generation Cost | "
            f"{value(metrics.generation_cost, ' ' + metrics.currency)} |"
        ),
        "",
    ]

    return "\n".join(lines)
