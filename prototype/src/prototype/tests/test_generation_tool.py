"""Thin agent tool wrapping the generation + self-repair pipeline."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from prototype.generator.contracts import GenerationRun, RepairAttempt


def _fake_run(status="success", **overrides):
    base = dict(
        status=status,
        contract_path=Path("demo.yaml"),
        model="deepseek",
        attempts=1,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        tests=({"nodeid": "test_a.py::test_x", "outcome": "passed"},),
        repair_log=(
            RepairAttempt(index=1, target="test_a.py::test_x", kind="function",
                          error_signature="assert 404 == 200", outcome="repaired"),
        ),
        suspected_defects=(
            {"nodeid": "test_a.py::test_defect", "message": "assert 500 == 200",
             "reason": "actual status not documented"},
        ),
        environmental=(),
        saved_versions=({"attempt": 0, "run_id": "run-1", "tests_dir": "generated/x"},),
        generator_errors=(),
        run_result={"status": "completed", "summary": {"total": 1, "passed": 1}},
        test_cases=("test_a.py::test_x",),
        runnability={
            "expected_total": 1, "ran_total": 1,
            "ran_nodeids": ["test_a.py::test_x"], "not_ran_nodeids": [],
            "runnability_percent": 100.0, "reason": None,
        },
        report_paths={"json": "out/generation-report.json",
                      "markdown": "out/generation-report.md"},
    )
    base.update(overrides)
    return GenerationRun(**base)


def _invoke(*, monkeypatch, fake=None, arguments=None, raises=None):
    pytest.importorskip("langchain")
    import prototype.generator.pipeline as pipeline_module
    from prototype.service_tools.runner.tools import generate_tests_tool

    if raises is not None:
        def boom(settings):
            raise raises

        monkeypatch.setattr(pipeline_module, "run_generation_pipeline", boom)
        captured = {}
    else:
        captured = {}

        def fake_pipeline(settings):
            captured["settings"] = settings
            return fake()

        monkeypatch.setattr(pipeline_module, "run_generation_pipeline", fake_pipeline)

    arguments = arguments or {
        "contract_path": "contract.yaml",
        "base_url": "http://catalogue:8080",
    }
    result = generate_tests_tool.invoke(arguments)
    return result, captured


def test_generate_tests_tool_returns_compact_result(monkeypatch):
    result, _ = _invoke(monkeypatch=monkeypatch, fake=lambda: _fake_run())

    assert result["tool"] == "generate_tests_tool"
    assert result["status"] == "success"
    assert result["pipeline_status"] == "success"
    assert result["attempts"] == 1
    assert result["tokens"] == {"input": 100, "output": 50, "total": 150}
    assert result["summary"] == {"total": 1, "passed": 1}
    assert result["repair_log"][0]["target"] == "test_a.py::test_x"
    assert result["suspected_defects"][0]["nodeid"] == "test_a.py::test_defect"
    assert result["saved_versions"][0]["run_id"] == "run-1"
    assert result["runnability"]["runnability_percent"] == 100.0
    assert result["report_paths"]["json"] == "out/generation-report.json"
    # The generated test code never reaches the agent.
    import json

    assert "def test_x" not in json.dumps(result)


def test_generate_tests_tool_passes_settings_through(monkeypatch):
    _, captured = _invoke(
        monkeypatch=monkeypatch,
        fake=lambda: _fake_run(),
        arguments={
            "contract_path": "contract.yaml",
            "base_url": "http://catalogue:8080",
            "network": "pytest-runner-catalogue_runner",
            "blocked_networks": "pytest-runner-catalogue_database, other-net",
            "max_repair_attempts": 2,
        },
    )
    settings = captured["settings"]
    assert settings.contract_path == Path("contract.yaml")
    assert settings.base_url == "http://catalogue:8080"
    assert settings.network == "pytest-runner-catalogue_runner"
    assert settings.blocked_networks == (
        "pytest-runner-catalogue_database", "other-net",
    )
    assert settings.max_repair_attempts == 2


def test_generate_tests_tool_reports_failed_pipeline_status(monkeypatch):
    result, _ = _invoke(
        monkeypatch=monkeypatch,
        fake=lambda: _fake_run(status="completed_with_failures",
                               suspected_defects=()),
    )
    assert result["pipeline_status"] == "completed_with_failures"
    assert result["status"] == "completed_with_failures"


def test_generate_tests_tool_reports_errors_instead_of_raising(monkeypatch):
    result, _ = _invoke(
        monkeypatch=monkeypatch,
        raises=ValueError("bad base_url"),
    )
    assert result["tool"] == "generate_tests_tool"
    assert result["status"] == "error"
    assert "Не удалось запустить" in result["message"]
