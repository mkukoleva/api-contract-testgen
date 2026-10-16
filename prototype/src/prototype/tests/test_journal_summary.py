"""Launch parameters (ТЗ v3 4.1.8.3) and generator results in the run journal."""

import pytest


CONTRACT = "openapi: 3.0.3\ninfo: {title: Demo, version: '1.0'}\npaths:\n  /items:\n    get: {}\n"


def test_launch_for_generate_mode(tmp_path):
    from prototype.generator.contracts import GenerationSettings
    from prototype.journal.launch import launch_parameters

    settings = GenerationSettings(
        contract_path="c.yaml", base_url="http://catalogue:8080", network="n",
        blocked_networks=("db",), runner_timeout_seconds=30, output_root="out",
        max_repair_attempts=2, repair_token_budget=1000,
    )
    launch = launch_parameters(mode="generate", contract="c.yaml", report_dir=tmp_path,
                               source_code=None, generation_settings=settings)
    assert launch == {
        "mode": "generate", "contract": "c.yaml", "report_dir": str(tmp_path), "source_code": None,
        "base_url": "http://catalogue:8080", "network": "n", "blocked_networks": ["db"],
        "runner_timeout_seconds": 30, "max_repair_attempts": 2, "repair_token_budget": 1000,
        "output_root": "out",
    }


def test_launch_for_testing_mode(tmp_path):
    from prototype.journal.launch import launch_parameters
    from prototype.service_tools.selection import ToolSettings

    contract = tmp_path / "c.yaml"
    contract.write_text(CONTRACT, encoding="utf-8")
    settings = ToolSettings(contract, "http://catalogue:8080", intent="quick", timeout_seconds=12)
    launch = launch_parameters(mode="testing", contract=str(contract), report_dir=None,
                               source_code=None, testing_settings=settings)
    assert launch["intent"] == "quick"
    assert launch["timeout_seconds"] == 12
    assert launch["base_url"] == "http://catalogue:8080"
    assert launch["schemathesis_url"] == "http://127.0.0.1:8080"
    assert launch["evomaster_image"] == "webfuzzing/evomaster:v6.1.1"
    assert launch["report_dir"] is None


@pytest.mark.parametrize("url, expected", [
    ("http://user:pass@host:8080/api", "http://host:8080/api"),
    ("https://token@host/api?x=1", "https://host/api?x=1"),
    ("http://host:8080", "http://host:8080"),
])
def test_credentials_are_stripped_from_urls(url, expected):
    from prototype.journal.launch import strip_credentials

    assert strip_credentials(url) == expected


def test_launch_strips_credentials_and_has_no_secret_like_keys():
    # Settings classes already reject credentials in URLs; this is defence in depth.
    from types import SimpleNamespace
    from prototype.journal.launch import launch_parameters

    settings = SimpleNamespace(base_url="http://u:p@catalogue:8080", network="n", api_key="never")
    launch = launch_parameters(mode="generate", contract="c.yaml", report_dir=None,
                               source_code=None, generation_settings=settings)
    assert launch["base_url"] == "http://catalogue:8080"
    assert not [key for key in launch if any(word in key for word in ("key", "token", "password"))]


def generation_data(runner_status, **extra):
    return {
        "tool": "generate_tests_tool", "pipeline_status": "success", "status": "success", "attempts": 2,
        "tokens": {"input": 100, "output": 20, "total": 120},
        "run_result": {"status": runner_status,
                       "summary": {"total": 4, "passed": 3, "failed": 1, "error": 0, "skipped": 0}},
        "saved_versions": [{"attempt": 1, "run_id": "r1", "tests_dir": "t1"},
                           {"attempt": 2, "run_id": "r2", "tests_dir": "t2"}],
        "repair_log": [],
        "runnability": {"expected_total": 4, "ran_total": 4, "runnability_percent": 100.0},
        **extra,
    }


@pytest.mark.parametrize("status", ["completed", "collection_error", "infrastructure_error",
                                    "timeout", "no_tests", "interrupted"])
def test_runner_status_becomes_tests_run_event(status):
    from prototype.journal import Journal
    from prototype.journal.summary import record_generation_result

    journal = Journal()
    record_generation_result(journal, generation_data(status))
    runs = [event for event in journal.events if event["event"] == "tests.run"]
    assert len(runs) == 1
    data = runs[0]["data"]
    assert data["runner_status"] == status
    assert (data["passed"], data["failed"], data["errors"], data["skipped"]) == (3, 1, 0, 0)
    assert (data["attempt"], data["run_id"], data["tests_dir"]) == (2, "r2", "t2")
    assert runs[0]["status"] == ("ok" if status == "completed" else "error")


def test_generation_result_event():
    from prototype.journal import Journal
    from prototype.journal.summary import record_generation_result

    journal = Journal()
    record_generation_result(journal, generation_data("completed"))
    result = next(event for event in journal.events if event["event"] == "generation.result")
    assert result["data"]["status"] == "success"
    assert result["data"]["attempts"] == 2
    assert result["data"]["tokens"] == {"input": 100, "output": 20, "total": 120}
    assert result["data"]["runnability_percent"] == 100.0


def test_repair_log_becomes_repair_events():
    from prototype.journal import Journal
    from prototype.journal.summary import record_generation_result

    log = [{"index": 1, "target": "test_a.py", "kind": "file", "error_signature": "SyntaxError",
            "outcome": "repaired"},
           {"index": 2, "target": "test_b.py::test_x", "kind": "function", "error_signature": "fixture",
            "outcome": "stuck"}]
    journal = Journal()
    record_generation_result(journal, generation_data("completed", repair_log=log))
    repairs = [event["data"] for event in journal.events if event["event"] == "repair"]
    assert [(r["index"], r["target"], r["outcome"], r["error_signature"]) for r in repairs] == [
        (1, "test_a.py", "repaired", "SyntaxError"), (2, "test_b.py::test_x", "stuck", "fixture")]


def test_result_without_run_result_has_no_tests_run():
    from prototype.journal import Journal
    from prototype.journal.summary import record_generation_result

    journal = Journal()
    record_generation_result(journal, generation_data("completed", run_result=None, saved_versions=[]))
    assert not [event for event in journal.events if event["event"] == "tests.run"]
    assert [event["event"] for event in journal.events] == ["generation.result"]
