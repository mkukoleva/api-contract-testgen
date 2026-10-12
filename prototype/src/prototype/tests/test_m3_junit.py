"""M3 -> CI/report consumer: generated-suite JUnit, not prototype-test JUnit."""

from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from prototype.reports.runner_report import save_run_report
from prototype.service_tools.runner.contracts import RunConfig, RunResult, TestResult as Case


def save(tmp_path, run):
    return save_run_report(run, RunConfig(tmp_path / "tests", tmp_path / "results"), tmp_path / "run",
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
        input_files=["test_api.py"], image="runner", network=None)


def test_junit_preserves_each_pytest_outcome_and_escapes_diagnostics(tmp_path):
    run = RunResult("completed", 1, 1, tests=(
        Case("test_api.py::test_ok", "passed"),
        Case("test_api.py::test_bad", "failed", message="assert 500 == 200 <>&\x00"),
        Case("test_api.py::test_setup", "error", phase="setup", message="fixture missing"),
        Case("test_api.py::test_skip", "skipped", message="explicit skip")))
    saved = save(tmp_path, run)
    suite = ET.parse(saved.report_paths["junit"]).getroot().find("testsuite")
    assert {key: suite.get(key) for key in ("tests", "failures", "errors", "skipped")} == {
        "tests": "4", "failures": "1", "errors": "1", "skipped": "1"}
    assert "500 == 200 <>&" in suite.find("testcase/failure").text
    assert len(suite.findall("testcase")) == 4
    assert saved.tests == run.tests


@pytest.mark.parametrize("status,code", [("collection_error", 2), ("timeout", None),
    ("interrupted", 0), ("infrastructure_error", None), ("no_tests", 5)])
def test_junit_lifecycle_failure_never_looks_like_an_empty_success(tmp_path, status, code):
    saved = save(tmp_path, RunResult(status, code, .1, error_message="run did not complete"))
    suite = ET.parse(saved.report_paths["junit"]).getroot().find("testsuite")
    assert suite.get("tests") == "1" and suite.get("errors") == "1"
    assert suite.find("testcase").get("classname") == "runner"
    assert suite.find("testcase/error").get("type") == status


def test_junit_timeout_keeps_completed_test_and_adds_run_error(tmp_path):
    saved = save(tmp_path, RunResult("timeout", None, 1, tests=(Case("test_api.py::test_ok", "passed"),)))
    suite = ET.parse(saved.report_paths["junit"]).getroot().find("testsuite")
    assert suite.get("tests") == "2" and suite.get("errors") == "1"
    assert len(suite.findall("testcase")) == 2
