"""Persist runner facts on the host after execution. Standard library only.

Quality metrics belong to evaluate; null means they have not been supplied.
Reports never import test modules or invoke the generator, evaluate or an LLM.
"""

from dataclasses import replace
from datetime import datetime
import html
import json
from pathlib import Path
import tempfile

from prototype.service_tools.runner.contracts import RunConfig, RunResult


def _text(value) -> str:
    """Escape untrusted test names and paths in Markdown prose/table cells."""
    text = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for char in "`*_[]|":
        text = text.replace(char, "\\" + char)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _diagnostic(value) -> str:
    return "<pre>" + html.escape(str(value), quote=False) + "</pre>"


def _markdown(payload: dict) -> str:
    result = payload["result"]
    lines = [
        "# Отчёт запуска pytest", "",
        f"Прогон: {_text(payload['run_id'])}", "",
        f"Начало (UTC): {_text(payload['started_at'])}  ",
        f"Завершение (UTC): {_text(payload['finished_at'])}", "",
        f"Статус: `{result['status']}`  ",
        f"Код pytest: {_text(result['exit_code'] if result['exit_code'] is not None else 'не получен')}  ",
        f"Длительность запуска: {result['duration_seconds']:.3f} с", "",
        "`completed` означает завершение прогона; отдельные проверки могли упасть.",
        "Падение assert само по себе не определяет, ошибочен тест или сервис.", "",
        "## Входные данные", "",
        "| Параметр | Значение |", "|---|---|",
    ]
    lines.extend(f"| {_text(key)} | {_text(value)} |" for key, value in payload["input"].items())
    lines += ["", "Python-файлы входного набора (без выполнения и копирования содержимого):", ""]
    lines.extend(f"- {_text(path)}" for path in payload["input_files"])
    if not payload["input_files"]:
        lines.append("Python-файлы не найдены.")
    lines += ["", "## Результаты выполнения", "",
              "Показаны только завершённые тестовые случаи. При ошибке сбора или таймауте",
              "этот список не является полным перечнем сгенерированных тестов.", "",
              "| total | passed | failed | error | skipped |", "|---|---|---|---|---|",
              "| " + " | ".join(str(result["summary"][key]) for key in
                                    ("total", "passed", "failed", "error", "skipped")) + " |", "",
              "| Тест (nodeid) | Результат | Фаза | Время, с |", "|---|---|---|---|"]
    for test in result["tests"]:
        lines.append(f"| {_text(test['nodeid'])} | {_text(test['outcome'])} | "
                     f"{_text(test['phase'])} | {test['duration_seconds']:.6f} |")
    lines += ["", "## Диагностика", ""]
    if result["error_message"]:
        lines += ["Ошибка/прерывание запуска:", _diagnostic(result["error_message"]), ""]
    for issue in result["compatibility_issues"]:
        location = issue["path"]
        if issue["line"] is not None:
            location += f":{issue['line']}:{issue['column']}"
        lines += [_diagnostic(f"{issue['category']}: {location}\n{issue['message']}"), ""]
    for error in result["collection_errors"]:
        lines += ["Ошибка сбора:", _diagnostic(error), ""]
    for test in result["tests"]:
        if test["message"]:
            lines += [_diagnostic(f"{test['nodeid']} ({test['phase']}):\n{test['message']}"), ""]
    lines += ["## Метрики качества", "",
              "Метрики не предоставлены. Их расчёт выполняет отдельный модуль evaluate.",
              "Runnability, проценты качества и стоимость здесь не рассчитываются.", "",
              "## Файлы прогона", ""]
    lines.extend(f"- {_text(key)}: {_text(path)}" for key, path in result["report_paths"].items())
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    # Replace the directory entry rather than following a symlink that test
    # code could have left in its writable results mount. Container is stopped.
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


def save_run_report(
    result: RunResult, config: RunConfig, run_dir: Path, *,
    started_at: datetime, finished_at: datetime, input_files: list[str],
    image: str, network: str | None,
) -> RunResult:
    """Write JSON + Markdown; return the same execution facts with report paths.

    The caller owns the unique run directory. I/O errors propagate so the
    runner cannot silently claim that required reports were saved.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {**result.report_paths, "json": run_dir / "report.json",
             "markdown": run_dir / "report.md"}
    saved = replace(result, report_paths=paths)
    payload = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "input": {"tests_dir": str(config.tests_dir.resolve()),
                  "base_url": config.base_url, "timeout_seconds": config.timeout_seconds,
                  "image": image, "network": network},
        "input_files": input_files,
        "result": saved.to_dict(),
        "metrics": None,
    }
    # Serialize both before writing; each destination is published atomically.
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    markdown = _markdown(payload)
    _atomic_write(paths["markdown"], markdown)
    _atomic_write(paths["json"], json_text)
    return saved
