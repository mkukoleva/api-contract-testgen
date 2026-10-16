"""journal.md: a human-readable view of journal events (ТЗ v3 4.1.8.2)."""

from typing import Any


_BRIEF_LIMIT = 160


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("|", "\\|").split())


def _brief(event: dict) -> str:
    data = event.get("data") or {}
    if event.get("event") == "stage" and "name" in data:
        rest = {key: value for key, value in data.items() if key != "name"}
        text = str(data["name"]) + (" " + ", ".join(f"{k}={v}" for k, v in rest.items()) if rest else "")
    else:
        text = ", ".join(f"{key}={value}" for key, value in data.items()
                         if not isinstance(value, (dict, list)))
    text = _cell(text)
    return text if len(text) <= _BRIEF_LIMIT else text[:_BRIEF_LIMIT] + "…"


def _first(events: list[dict], name: str) -> dict:
    return next((event.get("data") or {} for event in events if event.get("event") == name), {})


def render_markdown(events: list[dict]) -> str:
    start = _first(events, "run.start")
    end = _first(events, "run.end")
    lines = ["# Журнал прогона", ""]
    if start.get("mode"):
        lines.append(f"- Режим: {_cell(start['mode'])}")
    if start.get("contract"):
        lines.append(f"- Контракт: {_cell(start['contract'])}")
    if events:
        lines.append(f"- Начало: {events[0].get('ts')}")
    if end:
        lines.append(f"- Длительность: {end.get('duration_s')} с")
        lines.append(f"- Итог: {_cell(end.get('outcome'))}, код выхода {_cell(end.get('exit_code'))}")
    lines.append("")

    llm = [event for event in events if event.get("event") == "llm.call"]
    if llm:
        def total(field):
            return sum((event["data"].get(field) or 0) for event in llm)
        lines += [
            "## LLM", "",
            f"- Вызовов LLM: {len(llm)}",
            f"- Ошибок: {sum(event['status'] == 'error' for event in llm)}",
            f"- Таймаутов: {sum(event['status'] == 'timeout' for event in llm)}",
            f"- Токены: {total('input_tokens')} / {total('output_tokens')} (вход / выход)",
            f"- Время: {round(total('duration_s'), 3)} с", "",
        ]

    runs = [event for event in events if event.get("event") == "tests.run"]
    if runs:
        lines += ["## Тесты", "", "| Попытка | Статус runner | Passed | Failed |", "|---|---|---|---|"]
        for event in runs:
            data = event["data"]
            lines.append(f"| {_cell(data.get('attempt'))} | {_cell(data.get('runner_status'))} | "
                         f"{_cell(data.get('passed'))} | {_cell(data.get('failed'))} |")
        lines.append("")

    problems = [event for event in events if event.get("status") in {"error", "timeout"}]
    if problems:
        lines += ["## Ошибки", ""]
        for event in problems:
            data = event.get("data") or {}
            detail = data.get("error") or data.get("runner_status") or _brief(event)
            lines.append(f"- #{event['seq']} {event['event']} ({event['status']}): {_cell(detail)}")
        lines.append("")

    lines += ["## Хронология", "", "| # | +сек | Событие | Статус | Кратко |", "|---|---|---|---|---|"]
    for event in events:
        lines.append(f"| {event.get('seq')} | {event.get('elapsed_s')} | {_cell(event.get('event'))} | "
                     f"{_cell(event.get('status'))} | {_brief(event)} |")
    return "\n".join(lines) + "\n"
