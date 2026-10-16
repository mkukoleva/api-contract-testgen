"""Journal events from a generator result: generation.result, repair, tests.run."""

import sys
from typing import Any

from .core import Journal


def record_generation_result(journal: Journal, data: dict[str, Any]) -> None:
    """Never raises: an unexpected result shape must not break the run."""
    try:
        _record(journal, data)
    except Exception as exc:  # journal is best-effort
        print(f"[JOURNAL] Не удалось записать итог генератора: {type(exc).__name__}: {exc}", file=sys.stderr)


def _record(journal: Journal, data: dict[str, Any]) -> None:
    runnability = data.get("runnability") or {}
    journal.emit(
        "generation.result",
        status=data.get("pipeline_status") or data.get("status"),
        attempts=data.get("attempts"),
        tokens=data.get("tokens"),
        runnability_percent=runnability.get("runnability_percent"),
        expected_total=runnability.get("expected_total"),
        ran_total=runnability.get("ran_total"),
    )
    for entry in data.get("repair_log") or ():
        journal.emit("repair", index=entry.get("index"), target=entry.get("target"),
                     outcome=entry.get("outcome"), error_signature=entry.get("error_signature"))

    run_result = data.get("run_result")
    if not isinstance(run_result, dict):
        return
    summary = run_result.get("summary") or {}
    versions = data.get("saved_versions") or ()
    latest = max(versions, key=lambda version: version.get("attempt") or 0) if versions else {}
    runner_status = run_result.get("status")
    journal.emit(
        "tests.run",
        "ok" if runner_status == "completed" else "error",
        attempt=latest.get("attempt", data.get("attempts")),
        runner_status=runner_status,
        passed=summary.get("passed"),
        failed=summary.get("failed"),
        errors=summary.get("error"),
        skipped=summary.get("skipped"),
        run_id=latest.get("run_id"),
        tests_dir=latest.get("tests_dir"),
    )
