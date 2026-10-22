"""
Generation-run report: JSON + Markdown, plain code, no LLM.

Includes TZ v3 section 4.1.5 result explanation and tracing.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any

from ..evaluate.runnability import compute_runnability
from ..generator.contracts import GenerationRun
from .tracing import build_result_trace


def _text(value) -> str:
    """Escape untrusted names and paths in Markdown."""
    text = html.escape(
        str(value),
        quote=False,
    ).replace("\\", "\\\\")

    for char in "`*_[]|":
        text = text.replace(
            char,
            "\\" + char,
        )

    return text.replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    ).replace(
        "\n",
        "<br>",
    )


def _code(value) -> str:
    return (
        "<pre>"
        + html.escape(
            str(value),
            quote=False,
        )
        + "</pre>"
    )


def _markdown(payload: dict) -> str:
    runnability = payload["runnability"]

    percent = (
        f"{runnability['runnability_percent']:g}%"
        if runnability[
            "runnability_percent"
        ] is not None
        else "не рассчитана"
    )

    lines = [
        "# Отчёт прогона генерации",
        "",
        f"Контракт: `{_text(payload['contract_path'])}`  ",
        f"Модель: `{_text(payload['model'])}`  ",
        f"Статус: `{_text(payload['status'])}`  ",
        f"Попыток ремонта: {payload['attempts']}  ",
        (
            "Токены (вход/выход/всего): "
            f"{payload['tokens']['input']} / "
            f"{payload['tokens']['output']} / "
            f"{payload['tokens']['total']}"
        ),
        "",
        "## Запускаемость (Runnability)",
        "",
        (
            "Доля ожидаемых тестов генератора, "
            "чьё тело действительно выполнилось."
        ),
        (
            "`failed` (в том числе по дефекту сервиса) "
            "считается запустившимся;"
        ),
        (
            "`skipped` и `error` — нет. Процент не выдаётся "
            "при пустом знаменателе,"
        ),
        (
            "отсутствии результатов прогона или неполных "
            "результатах (timeout/interrupted)."
        ),
        "",
        "| Ожидаемые тесты | Запустившиеся | Runnability |",
        "|---|---:|---:|",
        (
            f"| {runnability['expected_total']} | "
            f"{runnability['ran_total']} | "
            f"{percent} |"
        ),
        "",
    ]

    if runnability["reason"]:
        lines.extend(
            [
                (
                    f"> Причина: "
                    f"{_text(runnability['reason'])}."
                ),
                "",
            ]
        )

    if runnability["ran_nodeids"]:
        lines.append(
            "Запустившиеся тесты:"
        )

        lines.extend(
            f"- `{_text(nodeid)}`"
            for nodeid in runnability[
                "ran_nodeids"
            ]
        )

        lines.append("")

    if runnability["not_ran_nodeids"]:
        lines.append(
            "Не запустившиеся тесты:"
        )

        lines.extend(
            f"- `{_text(nodeid)}`"
            for nodeid in runnability[
                "not_ran_nodeids"
            ]
        )

        lines.append("")

    evidence = payload.get("category_evidence")
    if evidence:
        lines += ["## Категории по операциям (4.1.4.1)", "", _text(evidence["scope"]), "",
                  "Статус выполнения pytest выше не означает полноту категорий или приёмку по ТЗ.", "",
                  "| Операция | normal | boundary | error |", "|---|---|---|---|"]
        for row in evidence["operations"]:
            lines.append("| " + " | ".join([_text(row["operation"]),
                *(_text(row["categories"][kind]["status"]) for kind in ("normal", "boundary", "error"))]) + " |")
        lines += ["", "observed — есть успешный тест с HTTP-наблюдением; missing — категория не подтверждена; "
                  "needs_contract_clarification — ожидание не определено контрактом; unavailable — нет полных данных.", ""]

    coverage = (payload.get("provenance") or {}).get("input_coverage")
    if coverage:
        lines += ["## Фактические входные условия", "", _text(coverage["scope"]), "",
                  "Статус: " + _text(coverage["status"]), ""]
        for row in coverage["operations"]:
            lines.append("- " + _text(row["operation"]) + ": " + _text(row["status"])
                         + ("; пропущены: " + _text(", ".join(row["missing"])) if row["missing"] else ""))
        lines += [_text(item) for item in coverage["limitations"]]
        lines.append("")

    story = (payload.get("provenance") or {}).get("user_story")
    if story:
        lines += ["## Цепочечный сценарий", "", _text(story["story"]), "",
                  "Ожидаемые шаги user story (фактический результат — в pytest):", ""]
        for index, step in enumerate(story["steps"], 1):
            lines.append(f"{index}. `{_text(step['endpoint'])}` — {_text(step['expected_result'])}")
        lines += ["", f"Конечная цель: {_text(story['final_goal'])}", ""]

    summary = (
        payload.get("pytest") or {}
    ).get("summary")

    if summary:
        lines += [
            "## Результаты pytest",
            "",
            (
                "| total | passed | failed | "
                "error | skipped |"
            ),
            "|---|---|---|---|---|",
            (
                "| "
                + " | ".join(
                    str(summary.get(key))
                    for key in (
                        "total",
                        "passed",
                        "failed",
                        "error",
                        "skipped",
                    )
                )
                + " |"
            ),
            "",
        ]

    # ------------------------------------------------------------
    # 4.1.5 — Объяснение и трассировка результатов
    # ------------------------------------------------------------

    provenance = payload.get("provenance") or {}
    result_trace = provenance.get(
        "result_trace"
    )

    if result_trace:
        trace_summary = (
            result_trace.get("summary")
            or {}
        )

        lines += [
            "## Объяснение и трассировка результатов",
            "",
            "### Покрытие API",
            "",
            "| Объект | Всего | Покрыто | Непокрыто |",
            "|---|---:|---:|---|",
        ]

        lines.append(
            f"| Операции | "
            f"{trace_summary.get('operations_total', 0)} | "
            f"{trace_summary.get('operations_covered', 0)} | "
            f"{_text(', '.join(trace_summary.get('operations_uncovered', [])) or '—')} |"
        )

        lines.append(
            f"| Пары операция + HTTP-код (статические проверки) | "
            f"{trace_summary.get('response_codes_total', 0)} | "
            f"{trace_summary.get('response_codes_covered', 0)} | "
            f"{_text(', '.join(trace_summary.get('response_codes_uncovered', [])) or '—')} |"
        )

        lines.append(
            f"| Параметры | "
            f"{trace_summary.get('parameters_total', 0)} | "
            f"{trace_summary.get('parameters_covered', 0)} | "
            f"{_text(', '.join(trace_summary.get('parameters_uncovered', [])) or '—')} |"
        )

        lines.append("")

        tests = result_trace.get(
            "tests"
        ) or []

        if tests:
            lines += [
                "### Трассировка тестов",
                "",
                (
                    "| Тест | Категория | Операции | "
                    "Ожидаемый результат | Результат |"
                ),
                "|---|---|---|---|---|",
            ]

            for test in tests:
                lines.append(
                    f"| `{_text(test.get('nodeid'))}` | "
                    f"{_text(test.get('category'))} | "
                    f"{_text(', '.join(test.get('operations', [])) or '—')} | "
                    f"{_text(test.get('expected_result'))} | "
                    f"{_text(test.get('outcome'))} |"
                )

            lines.append("")

        failed_tests = result_trace.get(
            "failed_tests"
        ) or []

        if failed_tests:
            lines += [
                "### Диагностика ошибок",
                "",
                (
                    "| Тест | Классификация | "
                    "Причина | Диагностика |"
                ),
                "|---|---|---|---|",
            ]

            for test in failed_tests:
                failure = (
                    test.get("failure")
                    or {}
                )

                diagnostics = failure.get(
                    "diagnostics"
                )

                if isinstance(
                    diagnostics,
                    dict,
                ):
                    diagnostics = json.dumps(
                        diagnostics,
                        ensure_ascii=False,
                    )

                lines.append(
                    f"| `{_text(test.get('nodeid'))}` | "
                    f"{_text(failure.get('classification'))} | "
                    f"{_text(failure.get('reason'))} | "
                    f"{_text(diagnostics or '—')} |"
                )

            lines.append("")

        if result_trace.get(
            "trace_error"
        ):
            lines += [
                (
                    "> Ошибка построения трассировки: "
                    + _text(
                        result_trace[
                            "trace_error"
                        ]
                    )
                ),
                "",
            ]

    if payload.get("repair_log"):
        lines += [
            "## Self-repair",
            "",
            (
                "| Попытка | Цель | Исход | "
                "Токены вывода |"
            ),
            "|---|---|---|---|",
        ]

        for attempt in payload[
            "repair_log"
        ]:
            lines.append(
                f"| {attempt['index']} | "
                f"{_text(attempt['target'])} | "
                f"{_text(attempt['outcome'])} | "
                f"{attempt['tokens_output']} |"
            )

        lines.append("")

    lines += [
        "## Проблемы, учитываемые отдельно",
        "",
    ]

    defects = payload[
        "suspected_defects"
    ]

    lines.append(
        "**Дефекты проверок API "
        f"(suspected_defects): {len(defects)}**"
    )

    for defect in defects:
        lines.append(
            _code(
                f"{defect.get('nodeid')}: "
                f"{defect.get('reason')}\n"
                f"{defect.get('message')}"
            )
        )

    lines.append("")

    environmental = payload[
        "environmental"
    ]

    lines.append(
        "**Ошибки окружения в теле тестов "
        f"(environmental): {len(environmental)}**"
    )

    for item in environmental:
        lines.append(
            _code(
                f"{item.get('nodeid')}: "
                f"{item.get('message')}"
            )
        )

    lines.append("")

    errors = payload[
        "generator_errors"
    ]

    lines.append(
        "**Ошибки генерации/ремонта "
        f"(generator_errors): {len(errors)}**"
    )

    for error in errors:
        lines.append(
            _code(str(error))
        )

    lines.append("")

    if payload.get(
        "saved_versions"
    ):
        lines += [
            "## Сохранённые версии",
            "",
            "| Попытка | run_id | Директория |",
            "|---|---|---|",
        ]

        for version in payload[
            "saved_versions"
        ]:
            lines.append(
                f"| {version.get('attempt')} | "
                f"{_text(version.get('run_id'))} | "
                f"`{_text(version.get('tests_dir'))}` |"
            )

        lines.append("")

    if payload.get(
        "provenance"
    ):
        lines += [
            "## Происхождение прогона",
            "",
            _code(
                json.dumps(
                    payload["provenance"],
                    ensure_ascii=False,
                    indent=2,
                )
            ),
            "",
        ]

    if payload.get(
        "recommendations"
    ):
        lines += [
            "## Рекомендации",
            "",
            _code(
                json.dumps(
                    payload["recommendations"],
                    ensure_ascii=False,
                    indent=2,
                )
            ),
            "",
        ]

    return "\n".join(lines) + "\n"


def _atomic_write(
    path: Path,
    content: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as stream:
        temporary = Path(
            stream.name
        )

        try:
            stream.write(content)
        except BaseException:
            stream.close()
            temporary.unlink(
                missing_ok=True
            )
            raise

    try:
        temporary.replace(path)
    finally:
        temporary.unlink(
            missing_ok=True
        )


def save_generation_report(
    run: GenerationRun,
    output_dir: Path,
) -> dict[str, str]:
    """
    Write JSON + Markdown for a generation run.

    Also builds TZ v3 section 4.1.5 result trace.
    """

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    runnability = run.runnability

    if runnability is None:
        runnability = compute_runnability(
            run.test_cases,
            run.run_result,
        ).to_dict()

    # Build 4.1.5 trace without mutating the frozen GenerationRun.
    provenance = dict(
        run.provenance or {}
    )

    try:
        provenance[
            "result_trace"
        ] = build_result_trace(run)

    except Exception as exc:
        provenance[
            "result_trace"
        ] = {
            "summary": {
                "operations_total": 0,
                "operations_covered": 0,
                "operations_uncovered": [],
                "response_codes_total": 0,
                "response_codes_covered": 0,
                "response_codes_uncovered": [],
                "parameters_total": 0,
                "parameters_covered": 0,
                "parameters_uncovered": [],
            },
            "tests": [],
            "failed_tests": [],
            "trace_error": str(exc),
        }

    paths = {
        "json": (
            output_dir
            / "generation-report.json"
        ),
        "markdown": (
            output_dir
            / "generation-report.md"
        ),
    }

    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "generation_run",
        "created_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "status": run.status,
        "recommendations": run.recommendations,
        "contract_path": str(
            run.contract_path
        ),
        "model": run.model,
        "attempts": run.attempts,
        "tokens": {
            "input": run.input_tokens,
            "output": run.output_tokens,
            "total": run.total_tokens,
        },
        "expected_test_cases": list(
            run.test_cases
        ),
        "runnability": runnability,
        "pytest": run.run_result,
        "repair_log": [
            attempt.to_dict()
            for attempt in run.repair_log
        ],
        "suspected_defects": list(
            run.suspected_defects
        ),
        "environmental": list(
            run.environmental
        ),
        "generator_errors": list(
            run.generator_errors
        ),
        "saved_versions": list(
            run.saved_versions
        ),
        "provenance": provenance,
        "category_evidence": run.category_evidence,
    }

    json_text = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )

    markdown = _markdown(
        payload
    )

    _atomic_write(
        paths["markdown"],
        markdown,
    )

    _atomic_write(
        paths["json"],
        json_text,
    )

    return {
        "json": str(
            paths["json"]
        ),
        "markdown": str(
            paths["markdown"]
        ),
    }
