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
