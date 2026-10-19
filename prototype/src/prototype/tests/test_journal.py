"""Run journal (ТЗ v3 4.1.8.2); no LLM, network or Docker required."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE_DIR = Path(__file__).resolve().parents[2]


def lines(directory):
    path = directory / "journal.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_events_are_numbered_and_timed(tmp_path):
    from prototype.journal import Journal

    journal = Journal()
    for name in ("a", "b", "c"):
        journal.emit(name)
    events = journal.events
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["elapsed_s"] for event in events] == sorted(event["elapsed_s"] for event in events)
    assert all(event["ts"].endswith("+00:00") for event in events)
    assert events[0]["status"] == "info" and events[0]["data"] == {}


def test_events_before_attach_are_buffered_then_written(tmp_path):
    from prototype.journal import Journal

    journal = Journal()
    journal.emit("run.start", mode="agent")
    journal.emit("stage", name="contract_loaded")
    journal.attach(tmp_path)
    assert [event["event"] for event in lines(tmp_path)] == ["run.start", "stage"]
    journal.emit("run.end", exit_code=0)
    assert len(lines(tmp_path)) == 3  # written before close()
    journal.close()


def test_second_attach_to_another_directory_raises(tmp_path):
    from prototype.journal import Journal

    journal = Journal()
    journal.attach(tmp_path / "one")
    with pytest.raises(ValueError):
        journal.attach(tmp_path / "two")
    journal.close()


def test_secrets_from_environment_are_redacted(tmp_path, monkeypatch):
    from prototype.journal import Journal

    monkeypatch.setenv("DEEPCODE_API_KEY", "sk-private")
    journal = Journal()
    journal.attach(tmp_path)
    journal.emit("llm.call", "error", error="failed with sk-private", nested={"a": ["sk-private"]})
    journal.close()
    text = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "sk-private" not in text
    assert "[REDACTED]" in text


def test_long_strings_are_truncated():
    from prototype.journal import Journal
    from prototype.journal.core import MAX_TEXT

    journal = Journal()
    journal.emit("tool.call", summary="x" * 5000)
    assert journal.events[0]["data"]["summary"] == "x" * MAX_TEXT + "…[обрезано]"
    assert MAX_TEXT == 2000


def test_write_failure_disables_journal_without_raising(tmp_path, monkeypatch, capsys):
    from prototype.journal import Journal

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    journal = Journal()
    journal.emit("run.start")
    monkeypatch.setattr(Path, "open", broken_open)
    journal.attach(tmp_path)
    journal.emit("stage", name="after_failure")
    journal.close()
    assert journal.disabled is True
    assert capsys.readouterr().err.count("[JOURNAL]") == 1


def test_close_is_idempotent_and_close_without_attach_writes_nothing(tmp_path, monkeypatch):
    from prototype.journal import Journal

    monkeypatch.chdir(tmp_path)
    detached = Journal()
    detached.emit("run.start")
    detached.close()
    assert list(tmp_path.iterdir()) == []

    journal = Journal()
    journal.attach(tmp_path / "run")
    journal.emit("run.start")
    journal.close()
    journal.close()
    assert len(lines(tmp_path / "run")) == 1


def test_works_under_non_ascii_directory(tmp_path):
    from prototype.journal import Journal

    directory = tmp_path / "журнал"
    journal = Journal()
    journal.attach(directory)
    journal.emit("run.start", contract="Каталог")
    journal.close()
    assert lines(directory)[0]["data"]["contract"] == "Каталог"


def test_import_does_not_load_langchain():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(SOURCE_DIR)!r}); import prototype.journal; "
         "assert 'langchain_core' not in sys.modules; "
         "assert 'prototype.llm.model' not in sys.modules"],
        capture_output=True, text=True, timeout=10,
        env={key: value for key, value in os.environ.items()
             if not key.startswith(("DEEPCODE_", "OPENAI_"))},
    )
    assert result.returncode == 0, result.stderr


def event(seq, name, status="info", **data):
    return {"seq": seq, "ts": "2026-10-16T12:00:00.000+00:00", "elapsed_s": float(seq),
            "event": name, "status": status, "data": data}


def test_markdown_has_sections_from_spec():
    from prototype.journal.render import render_markdown

    text = render_markdown([
        event(1, "run.start", mode="generate", contract="demo.yaml"),
        event(2, "llm.call", "ok", model="m", duration_s=1.5, input_tokens=10, output_tokens=1, total_tokens=11),
        event(3, "llm.call", "timeout", model="m", duration_s=60.0, input_tokens=20, output_tokens=2,
              total_tokens=22, error="APITimeoutError: Request timed out."),
        event(4, "tests.run", "ok", attempt=1, runner_status="completed", passed=3, failed=1, errors=0, skipped=0),
        event(5, "run.end", exit_code=0, duration_s=5.0, outcome="ok"),
    ])
    assert "# Журнал прогона" in text
    assert "Вызовов LLM: 2" in text
    assert "Токены: 30 / 3" in text
    assert "Таймаутов: 1" in text
    assert "| 1 | completed | 3 | 1 |" in text
    assert "## Ошибки" in text and "Request timed out" in text
    assert "## Хронология" in text


def test_empty_sections_are_omitted():
    from prototype.journal.render import render_markdown

    text = render_markdown([event(1, "run.start", mode="agent"), event(2, "run.end", exit_code=0, outcome="ok")])
    assert "## LLM" not in text
    assert "## Тесты" not in text
    assert "## Ошибки" not in text
    assert "## Хронология" in text


def test_table_cells_are_escaped():
    from prototype.journal.render import render_markdown

    text = render_markdown([event(1, "contract.error", "error", error="a|b\nc")])
    assert r"a\|b c" in text


def test_close_writes_markdown_by_default(tmp_path):
    from prototype.journal import Journal

    journal = Journal()
    journal.attach(tmp_path)
    journal.emit("run.start", mode="agent")
    journal.close()
    assert "# Журнал прогона" in (tmp_path / "journal.md").read_text(encoding="utf-8")


def test_concurrent_emits_have_unique_sequence_numbers(tmp_path):
    import threading
    from prototype.journal import Journal

    journal = Journal()
    journal.attach(tmp_path)
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=lambda: [journal.emit("tick") for _ in range(3000)]) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    journal.close()
    seqs = [event["seq"] for event in lines(tmp_path)]
    assert len(seqs) == 24000
    assert sorted(seqs) == list(range(1, 24001))
