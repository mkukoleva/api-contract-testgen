"""Generation-run report: JSON + Markdown, plain code, no LLM.

Merges generator facts (expected test cases, attempts, tokens, suspected
defects, environmental failures, generator errors) with runner facts (pytest
summary and per-test outcomes) and the computed Runnability (prototype.evaluate).
Only the standard library is used; test modules are never imported.

Files are ``generation-report.json`` / ``generation-report.md`` in an explicit
directory, written atomically. A failed write raises: the pipeline surfaces it
as a generator error instead of silently claiming the report was saved.
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


def _text(value) -> str:
    """Escape untrusted names and paths in Markdown prose/table cells."""
    text = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for char in "`*_[]|":
        text = text.replace(char, "\\" + char)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _code(value) -> str:
    return "<pre>" + html.escape(str(value), quote=False) + "</pre>"


def _markdown(payload: dict) -> str:
    runnability = payload["runnability"]
    percent = (
        f"{runnability['runnability_percent']:g}%"
        if runnability["runnability_percent"] is not None
        else "не рассчитана"
    )
    lines = [
        "# Отчёт прогона генерации", "",
        f"Контракт: `{_text(payload['contract_path'])}`  ",
        f"Модель: `{_text(payload['model'])}`  ",
        f"Статус: `{_text(payload['status'])}`  ",
        f"Попыток ремонта: {payload['attempts']}  ",
        f"Токены (вход/выход/всего): {payload['tokens']['input']} / "
        f"{payload['tokens']['output']} / {payload['tokens']['total']}", "",
        "## Запускаемость (Runnability)", "",
        "Доля ожидаемых тестов генератора, чьё тело действительно выполнилось.",
        "`failed` (в том числе по дефекту сервиса) считается запустившимся;",
        "`skipped` и `error` — нет. Процент не выдаётся при пустом знаменателе,",
        "отсутствии результатов прогона или неполных результатах (timeout/interrupted).", "",
        "| Ожидаемые тесты | Запустившиеся | Runnability |",
        "|---|---:|---:|",
        f"| {runnability['expected_total']} | {runnability['ran_total']} | {percent} |",
        "",
    ]
    if runnability["reason"]:
        lines.extend([f"> Причина: {_text(runnability['reason'])}.", ""])
    if runnability["ran_nodeids"]:
        lines.append("Запустившиеся тесты:")
        lines.extend(f"- `{_text(nodeid)}`" for nodeid in runnability["ran_nodeids"])
        lines.append("")
    if runnability["not_ran_nodeids"]:
        lines.append("Не запустившиеся тесты:")
        lines.extend(f"- `{_text(nodeid)}`" for nodeid in runnability["not_ran_nodeids"])
        lines.append("")

    summary = (payload.get("pytest") or {}).get("summary")
    if summary:
        lines += [
            "## Результаты pytest", "",
            "| total | passed | failed | error | skipped |", "|---|---|---|---|---|",
            "| " + " | ".join(str(summary.get(key)) for key in
                              ("total", "passed", "failed", "error", "skipped")) + " |", "",
        ]

    if payload.get("repair_log"):
        lines += ["## Self-repair", "",
                  "| Попытка | Цель | Исход | Токены вывода |", "|---|---|---|---|"]
        for attempt in payload["repair_log"]:
            lines.append(
                f"| {attempt['index']} | {_text(attempt['target'])} | "
                f"{_text(attempt['outcome'])} | {attempt['tokens_output']} |"
            )
        lines.append("")

    lines += ["## Проблемы, учитываемые отдельно", ""]
    defects = payload["suspected_defects"]
    lines.append(f"**Дефекты проверок API (suspected_defects): {len(defects)}**")
    for defect in defects:
        lines.append(_code(f"{defect.get('nodeid')}: {defect.get('reason')}\n{defect.get('message')}"))
    lines.append("")
    environmental = payload["environmental"]
    lines.append(f"**Ошибки окружения в теле тестов (environmental): {len(environmental)}**")
    for item in environmental:
        lines.append(_code(f"{item.get('nodeid')}: {item.get('message')}"))
    lines.append("")
    errors = payload["generator_errors"]
    lines.append(f"**Ошибки генерации/ремонта (generator_errors): {len(errors)}**")
    for error in errors:
        lines.append(_code(str(error)))
    lines.append("")

    if payload.get("saved_versions"):
        lines += ["## Сохранённые версии", ""]
        lines.extend(["| Попытка | run_id | Директория |", "|---|---|---|"])
        for version in payload["saved_versions"]:
            lines.append(
                f"| {version.get('attempt')} | {_text(version.get('run_id'))} | "
                f"`{_text(version.get('tests_dir'))}` |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, delete=False, suffix=".tmp") as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_generation_report(
    run: GenerationRun,
    output_dir: Path,
) -> dict[str, str]:
    """Write JSON + Markdown for a generation run; return resolved paths.

    The report is written from the structured ``GenerationRun`` only; test code
    is never copied and never imported. Runnability is recomputed from the
    generator's expected cases when the run object has none (defensive: the
    pipeline always provides it).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    runnability = run.runnability
    if runnability is None:
        runnability = compute_runnability(
            run.test_cases, run.run_result
        ).to_dict()

    paths = {
        "json": output_dir / "generation-report.json",
        "markdown": output_dir / "generation-report.md",
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "generation_run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": run.status,
        "contract_path": str(run.contract_path),
        "model": run.model,
        "attempts": run.attempts,
        "tokens": {
            "input": run.input_tokens,
            "output": run.output_tokens,
            "total": run.total_tokens,
        },
        "expected_test_cases": list(run.test_cases),
        "runnability": runnability,
        "pytest": run.run_result,
        "repair_log": [attempt.to_dict() for attempt in run.repair_log],
        "suspected_defects": list(run.suspected_defects),
        "environmental": list(run.environmental),
        "generator_errors": list(run.generator_errors),
        "saved_versions": list(run.saved_versions),
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    markdown = _markdown(payload)
    _atomic_write(paths["markdown"], markdown)
    _atomic_write(paths["json"], json_text)
    return {"json": str(paths["json"]), "markdown": str(paths["markdown"])}
