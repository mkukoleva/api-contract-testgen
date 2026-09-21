"""Runner boundary checks; no Docker, LLM credentials or network required."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


SOURCE_DIR = Path(__file__).resolve().parents[2]


def isolated_python(code):
    # -I -S excludes installed dependencies and ignores PYTHONPATH.
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(SOURCE_DIR)!r}); " + code],
        capture_output=True, text=True, timeout=10,
        env={key: value for key, value in os.environ.items()
             if not key.startswith(("DEEPCODE_", "OPENAI_"))},
    )


def test_runner_import_works_without_llm_or_third_party_packages():
    result = isolated_python(
        "import prototype.runner.contracts; "
        "assert 'prototype.llm.model' not in sys.modules; "
        "assert 'prototype.runner.tools' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


def test_existing_cli_help_works_without_loading_llm():
    result = isolated_python(
        "import prototype; sys.argv = ['tester', '--help']; prototype.main()"
    )
    assert result.returncode == 0, result.stderr
    assert "contract" in result.stdout


def test_existing_agent_keeps_its_model_prompt_and_four_tools(monkeypatch):
    import prototype

    model = object()
    tool_names = (
        "schemathesis_tool", "demo_api_test_tool",
        "generate_user_story_tool", "verify_user_story_tool",
    )
    agents = ModuleType("langchain.agents")
    agents.create_agent = lambda **kwargs: SimpleNamespace(**kwargs)
    llm = ModuleType("prototype.llm.model")
    llm.build_model = lambda: model
    tools = ModuleType("prototype.runner.tools")
    for name in tool_names:
        setattr(tools, name, object())
    for name, module in (("langchain.agents", agents),
                         ("prototype.llm.model", llm),
                         ("prototype.runner.tools", tools)):
        monkeypatch.setitem(sys.modules, name, module)

    agent = prototype.build_agent()
    assert agent.model is model
    assert agent.system_prompt == prototype.SYSTEM_PROMPT
    assert agent.tools == [getattr(tools, name) for name in tool_names]


def test_cli_still_passes_contract_to_run(monkeypatch):
    import prototype

    received = []
    monkeypatch.setattr(prototype, "run", received.append)
    monkeypatch.setattr(sys, "argv", ["tester", "example.yaml"])
    prototype.main()
    assert received == ["example.yaml"]


def test_config_accepts_paths_without_creating_directories(tmp_path):
    from prototype.runner.contracts import RunConfig

    config = RunConfig(
        tests_dir=str(tmp_path / "tests"), output_dir=tmp_path / "reports",
        base_url="http://catalogue:80/api", timeout_seconds=30,
    )
    assert config.tests_dir == tmp_path / "tests"
    assert config.output_dir == tmp_path / "reports"
    assert config.base_url == "http://catalogue:80/api"
    assert not config.output_dir.exists()
    assert not config.tests_dir.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "30"])
def test_config_rejects_invalid_time_budget(timeout):
    from prototype.runner.contracts import RunConfig

    with pytest.raises(ValueError, match="timeout_seconds"):
        RunConfig("tests", "reports", timeout_seconds=timeout)


@pytest.mark.parametrize("url", [
    "", "catalogue:80", "ftp://catalogue", "http://", "http://catalogue:0",
    "http://catalogue:99999", "http://catalogue:abc", "http://user:pass@catalogue",
    "http://catalogue?redirect=elsewhere", "http://catalogue#fragment",
    "http://cat alogue", "\nhttp://catalogue", "http://catalogue\\other",
])
def test_config_rejects_invalid_or_ambiguous_target(url):
    from prototype.runner.contracts import RunConfig

    with pytest.raises(ValueError, match="base_url"):
        RunConfig("tests", "reports", base_url=url)


@pytest.mark.parametrize("url", [None, "http://catalogue:80", "https://localhost/api", "http://[::1]:8080"])
def test_config_supports_unit_mode_and_http_targets(url):
    from prototype.runner.contracts import RunConfig

    assert RunConfig("tests", "reports", base_url=url).base_url == url


@pytest.mark.parametrize("field", ["tests_dir", "output_dir"])
def test_config_rejects_empty_paths(field):
    from prototype.runner.contracts import RunConfig

    args = {"tests_dir": "tests", "output_dir": "reports", field: " "}
    with pytest.raises(ValueError, match=field):
        RunConfig(**args)


def test_result_json_distinguishes_assertion_failure_from_infrastructure_error():
    from prototype.runner.contracts import RunResult, RunStatus, TestResult

    completed = RunResult(
        status=RunStatus.COMPLETED, exit_code=1, duration_seconds=0.5,
        tests=(TestResult("test_api.py::test_ok", "passed"),
               TestResult("test_api.py::test_missing", "failed", message="Expected 200, got 500"),
               TestResult("test_api.py::test_setup", "error", phase="setup"),
               TestResult("test_api.py::test_optional", "skipped")),
        report_paths={"junit": Path("reports/junit.xml")},
    )
    payload = json.loads(json.dumps(completed.to_dict(), allow_nan=False))
    assert payload["status"] == "completed"
    assert payload["summary"] == {"total": 4, "passed": 1, "failed": 1, "error": 1, "skipped": 1}
    assert payload["tests"][1]["message"] == "Expected 200, got 500"
    assert payload["report_paths"]["junit"] == str(Path("reports/junit.xml"))

    broken = RunResult("infrastructure_error", None, 0, error_message="Docker unavailable")
    assert broken.to_dict()["status"] == "infrastructure_error"
    assert broken.to_dict()["summary"]["total"] == 0
    assert broken.to_dict()["exit_code"] is None


@pytest.mark.parametrize("status", ["collection_error", "timeout", "no_tests", "interrupted"])
def test_incomplete_runs_keep_diagnostics(status):
    from prototype.runner.contracts import RunResult

    result = RunResult(status, None, 1, collection_errors=("test_bad.py: SyntaxError",))
    assert result.to_dict()["status"] == status
    assert result.to_dict()["collection_errors"] == ["test_bad.py: SyntaxError"]


def test_result_rejects_unknown_status_and_invalid_duration():
    from prototype.runner.contracts import RunResult, TestResult

    with pytest.raises(ValueError):
        RunResult("success-ish", 0, 1)
    with pytest.raises(ValueError):
        TestResult("test_api.py::test_one", "success-ish")
    with pytest.raises(ValueError):
        TestResult("test_api.py::test_one", "passed", phase="unknown")
    for duration in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="duration_seconds"):
            RunResult("completed", 0, duration)
        with pytest.raises(ValueError, match="duration_seconds"):
            TestResult("test_api.py::test_one", "passed", duration_seconds=duration)


def test_results_reject_empty_and_duplicate_test_identifiers():
    from prototype.runner.contracts import RunResult, TestResult

    with pytest.raises(ValueError, match="nodeid"):
        TestResult(" ", "passed")
    case = TestResult("test_api.py::test_one", "passed")
    with pytest.raises(ValueError, match="nodeid"):
        RunResult("completed", 0, 1, tests=(case, case))
