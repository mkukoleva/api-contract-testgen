"""Saved reports preserve execution facts; no quality metrics are calculated."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from prototype.service_tools.runner.contracts import RunConfig, RunResult, TestResult as CaseResult
from prototype.service_tools.runner import docker_runner


def test_syntax_failure_saves_versioned_reports_without_docker(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_bad.py").write_text("def test_bad(:", encoding="utf-8")
    monkeypatch.setattr(docker_runner.subprocess, "run",
                        lambda *a, **k: pytest.fail("syntax check must not call Docker"))
    config = RunConfig(suite, tmp_path / "out")
    first = docker_runner.run_tests(config)
    second = docker_runner.run_tests(config)
    first_path = Path(first.report_paths["json"])
    second_path = Path(second.report_paths["json"])
    assert first_path != second_path
    report = json.loads(first_path.read_text(encoding="utf-8"))
    assert report["result"]["status"] == "collection_error"
    assert report["result"]["exit_code"] is None
    assert report["result"]["tests"] == []
    assert report["input_files"] == ["test_bad.py"]
    assert report["metrics"] is None
    started = datetime.fromisoformat(report["started_at"])
    finished = datetime.fromisoformat(report["finished_at"])
    assert started.tzinfo is not None and started <= finished
    assert started.strftime("%Y%m%dT%H%M%S") in first_path.parent.name
    assert report["run_id"] == first_path.parent.name
    assert report["result"]["report_paths"]["json"] == str(first_path)
    markdown = Path(first.report_paths["markdown"]).read_text(encoding="utf-8")
    assert "test_bad.py" in markdown and "collection_error" in markdown
    assert "invalid syntax" in markdown
    assert "не предоставлены" in markdown


@pytest.mark.parametrize("missing_suite", [False, True])
def test_environment_failure_still_saves_reports(tmp_path, monkeypatch, missing_suite):
    suite = tmp_path / "suite"
    if not missing_suite:
        suite.mkdir()
        (suite / "test_ok.py").write_text("def test_ok(): pass", encoding="utf-8")
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(docker_runner.subprocess, "run", unavailable)
    result = docker_runner.run_tests(RunConfig(suite, tmp_path / "out"))
    assert result.status == "infrastructure_error"
    payload = json.loads(Path(result.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "infrastructure_error"
    assert payload["result"]["error_message"]
    assert payload["input_files"] == ([] if missing_suite else ["test_ok.py"])
    assert not missing_suite or not suite.exists()


def test_reports_keep_failed_tests_and_escape_diagnostics(tmp_path):
    from prototype.reports.runner_report import save_run_report

    result = RunResult("completed", 1, 0.5, tests=(
        CaseResult("test_api.py::test_missing[<script>|x]", "failed",
                   message="HTTP 500\n<script>alert('x')</script>\n```"),
        CaseResult("test_api.py::test_skip", "skipped"),
    ))
    saved = save_run_report(
        result, RunConfig(tmp_path / "suite", tmp_path / "out"), tmp_path / "run",
        started_at=datetime.fromisoformat("2026-10-05T12:00:00+00:00"),
        finished_at=datetime.fromisoformat("2026-10-05T12:00:01+00:00"),
        input_files=["test_api.py"], image="runner:test", network=None)
    payload = json.loads(Path(saved.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "completed"
    assert payload["result"]["summary"] == {
        "total": 2, "passed": 0, "failed": 1, "error": 0, "skipped": 1}
    assert payload["result"]["tests"][0]["message"].startswith("HTTP 500")
    assert payload["metrics"] is None
    markdown = Path(saved.report_paths["markdown"]).read_text(encoding="utf-8")
    assert "HTTP 500" in markdown and "&lt;script&gt;" in markdown
    assert "<script>" not in markdown
    assert "failed" in markdown and "skipped" in markdown


def test_report_failure_is_reported_without_losing_execution_result(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_bad.py").write_text("def test_bad(:", encoding="utf-8")
    original = Path.replace
    def reject_report(self, target):
        if Path(target).name == "report.json":
            raise OSError("report disk full")
        return original(self, target)
    monkeypatch.setattr(Path, "replace", reject_report)
    result = docker_runner.run_tests(RunConfig(suite, tmp_path / "out"))
    assert result.status == "infrastructure_error"
    assert "report disk full" in result.error_message
    assert result.compatibility_issues and result.collection_errors
    assert "json" not in result.report_paths


def test_invalid_nested_output_never_writes_into_test_suite(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    output = suite / "reports"
    result = docker_runner.run_tests(RunConfig(suite, output))
    assert result.status == "infrastructure_error"
    assert not output.exists()
