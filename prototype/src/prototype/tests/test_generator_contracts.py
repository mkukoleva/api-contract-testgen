"""Generation settings and result contracts; no LLM, Docker or network."""

from pathlib import Path

import pytest


def test_settings_defaults_are_three_attempts_and_token_budget(monkeypatch):
    from prototype.generator.contracts import (
        DEFAULT_MAX_REPAIR_ATTEMPTS,
        DEFAULT_REPAIR_TOKEN_BUDGET,
        GenerationSettings,
    )

    monkeypatch.delenv("TESTGEN_MAX_REPAIR_ATTEMPTS", raising=False)
    monkeypatch.delenv("TESTGEN_REPAIR_TOKEN_BUDGET", raising=False)
    settings = GenerationSettings(contract_path="demo.yaml")

    assert DEFAULT_MAX_REPAIR_ATTEMPTS == 3
    assert DEFAULT_REPAIR_TOKEN_BUDGET == 30_000
    assert settings.max_repair_attempts == 3
    assert settings.repair_token_budget == 30_000
    assert settings.contract_path == Path("demo.yaml")
    assert settings.blocked_networks == ()


def test_settings_read_env_overrides(monkeypatch):
    from prototype.generator.contracts import GenerationSettings

    monkeypatch.setenv("TESTGEN_MAX_REPAIR_ATTEMPTS", "5")
    monkeypatch.setenv("TESTGEN_REPAIR_TOKEN_BUDGET", "1000")
    settings = GenerationSettings(
        contract_path="demo.yaml",
        base_url="http://catalogue:8080",
        network="pytest-runner-catalogue_runner",
        blocked_networks=["pytest-runner-catalogue_database"],
    )
    assert settings.max_repair_attempts == 5
    assert settings.repair_token_budget == 1000
    assert settings.network == "pytest-runner-catalogue_runner"
    assert settings.blocked_networks == ("pytest-runner-catalogue_database",)


def test_settings_ignore_invalid_env_and_keep_default(monkeypatch):
    from prototype.generator.contracts import GenerationSettings

    monkeypatch.setenv("TESTGEN_MAX_REPAIR_ATTEMPTS", "abc")
    monkeypatch.setenv("TESTGEN_REPAIR_TOKEN_BUDGET", "-3")
    settings = GenerationSettings(contract_path="demo.yaml")
    assert settings.max_repair_attempts == 3
    assert settings.repair_token_budget == 30_000


@pytest.mark.parametrize("field, value, match", [
    ("contract_path", " ", "contract_path"),
    ("contract_path", 123, "contract_path"),
    ("model", 1, "model"),
    ("base_url", 1, "base_url"),
    ("network", 1, "network"),
    ("blocked_networks", ["", "x"], "blocked_networks"),
    ("output_dir", " ", "output_dir"),
    ("output_root", " ", "output_root"),
    ("runner_timeout_seconds", 0, "runner_timeout_seconds"),
    ("runner_timeout_seconds", True, "runner_timeout_seconds"),
    ("max_repair_attempts", 0, "max_repair_attempts"),
    ("max_repair_attempts", True, "max_repair_attempts"),
    ("max_repair_attempts", 2.5, "max_repair_attempts"),
    ("repair_token_budget", -1, "repair_token_budget"),
    ("test_fragment_lines", -1, "test_fragment_lines"),
    ("contract_fragment_chars", -1, "contract_fragment_chars"),
    ("diagnostic_chars", -1, "diagnostic_chars"),
])
def test_settings_reject_invalid_values(monkeypatch, field, value, match):
    monkeypatch.delenv("TESTGEN_MAX_REPAIR_ATTEMPTS", raising=False)
    monkeypatch.delenv("TESTGEN_REPAIR_TOKEN_BUDGET", raising=False)
    from prototype.generator.contracts import GenerationSettings

    args = {"contract_path": "demo.yaml", field: value}
    with pytest.raises(ValueError, match=match):
        GenerationSettings(**args)


def test_repair_attempt_and_generation_run_to_dict():
    from prototype.generator.contracts import GenerationRun, RepairAttempt

    attempt = RepairAttempt(
        index=1,
        target="test_catalogue.py::test_item",
        kind="function",
        error_signature="assert 500 == 200",
        outcome="repaired",
        payload_chars=1200,
        tokens_output=210,
    )
    assert attempt.to_dict() == {
        "index": 1,
        "target": "test_catalogue.py::test_item",
        "kind": "function",
        "error_signature": "assert 500 == 200",
        "outcome": "repaired",
        "payload_chars": 1200,
        "tokens_output": 210,
        "message": None,
    }

    run = GenerationRun(
        status="success",
        contract_path=Path("demo.yaml"),
        model="deepseek",
        attempts=1,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        tests=({"nodeid": "test_a.py::test_x", "outcome": "passed"},),
        repair_log=(attempt,),
        suspected_defects=({"nodeid": "test_a.py::test_y", "message": "500"},),
        saved_versions=({"attempt": 0, "run_id": "2026-10-05_000000", "tests_dir": "x"},),
        run_result={"status": "completed"},
    )
    payload = run.to_dict()
    assert payload["status"] == "success"
    assert payload["attempts"] == 1
    assert payload["tokens"] == {"input": 100, "output": 50, "total": 150}
    assert payload["repair_log"] == [attempt.to_dict()]
    assert payload["suspected_defects"] == [{"nodeid": "test_a.py::test_y", "message": "500"}]
    assert payload["run_result"] == {"status": "completed"}
    assert payload["saved_versions"][0]["attempt"] == 0
