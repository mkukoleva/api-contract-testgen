"""Regression checks for input, verdict and tool boundaries (no real LLM)."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from prototype.generator.contracts import GenerationSettings, GenerationRun
from prototype.generator.context import match_contract_path
from prototype.generator.pipeline import _derive_status
from prototype.parser.contract import ContractInputError, normalize_contract, load_contract_document
from prototype.reports.recommendations import build_recommendations
from prototype.service_tools.runner.contracts import RunResult, RunStatus, TestResult as Case


@pytest.mark.parametrize("url", ["http://user:fake-secret@api:8080", "ftp://api", "http://api:0",
    "http://api:bad", "http://api?token=fake-secret", "http://api/#fragment", "http://api/ space", ""])
def test_generation_rejects_invalid_url_before_io(tmp_path, url):
    with pytest.raises(ValueError, match="base_url") as error:
        GenerationSettings(tmp_path / "missing.yaml", base_url=url, output_dir=tmp_path / "output")
    assert "fake-secret" not in str(error.value)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("exit_code,outcomes,expected", [
    (0, ["passed"], "success"), (0, ["passed", "skipped"], "success"),
    (1, ["passed"], "completed_with_failures"), (3, ["passed"], "completed_with_failures"),
    (None, ["passed"], "completed_with_failures"), (0, ["failed"], "completed_with_failures"),
    (0, ["error"], "completed_with_failures"), (0, ["skipped"], "completed_without_execution"),
    (0, [], "completed_without_execution"),
])
def test_pipeline_verdict_requires_exit_zero_and_executed_tests(exit_code, outcomes, expected):
    run = RunResult(RunStatus.COMPLETED, exit_code, 0.1,
                    tests=tuple(Case(f"test_a.py::test_{i}", outcome) for i, outcome in enumerate(outcomes)))
    assert _derive_status(run) == expected


@pytest.mark.parametrize("status", [status for status in RunStatus if status != RunStatus.COMPLETED])
def test_pipeline_preserves_non_completed_status(status):
    assert _derive_status(RunResult(status, None, 0)) == status.value


@pytest.mark.parametrize("key", [date(2026, 10, 14), True, None, 1.5, float("inf"), b"key", ("x",)])
def test_contract_rejects_non_json_mapping_keys(key):
    with pytest.raises(ContractInputError, match="ключ|Ключ"):
        normalize_contract({"default": {key: "value"}})


def test_numeric_yaml_response_keys_normalized_without_collision(tmp_path):
    path = tmp_path / "api.yaml"
    path.write_text("openapi: 3.1.0\npaths: { /items: {get: {responses: {200: {description: OK}}}}}", encoding="utf-8")
    data = load_contract_document(path).data
    assert data["paths"]["/items"]["get"]["responses"] == {"200": {"description": "OK"}}
    with pytest.raises(ContractInputError, match="ключ|Ключ"):
        normalize_contract({"responses": {200: {}, "200": {}}})


@pytest.mark.parametrize("template,path,expected", [
    ("/files/{name}.json", "/files/report.json", True),
    ("/files/{name}.json", "http://api/files/report.json?x=1", True),
    ("/files/{name}.json", "/files/reportXjson", False),
    ("/files/{name}.json", "/files/a/b.json", False),
    ("/files/{name}.json", "/files/.json", False),
    ("/v{version}/files/{name}.{ext}", "/v1/files/a.json", True),
    ("/a+b/{id}", "/aaab/1", False),
    ("/files/{name}.json", "/files/a%2Fb.json", True),
])
def test_embedded_path_parameters(template, path, expected):
    assert match_contract_path(template, path) is expected


def test_many_embedded_parameters_do_not_hang_on_mismatch():
    # Bound the regression itself: a backtracking implementation can hang.
    code = "from prototype.generator.context import match_contract_path; print(match_contract_path('/' + ''.join('{p%d}' % i for i in range(12)) + 'Z', '/' + 'a' * 60 + 'Y'))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "False"


def response_contract(codes):
    return {"openapi": "3.1.0", "paths": {"/files/{name}.json": {"get": {
        "responses": {code: {"description": "response"} for code in codes}}}}}


@pytest.mark.parametrize("codes,status,missing,clarification", [
    (["200", "2XX", "default"], 200, {"2XX", "DEFAULT"}, False),
    (["200", "2XX", "default"], 201, {"200", "DEFAULT"}, False),
    (["200", "2XX", "default"], 404, {"200", "2XX"}, False),
    (["200", "4XX"], 404, {"200"}, False),
    (["200"], 404, {"200"}, True),
])
def test_recommendations_match_one_response_definition(codes, status, missing, clarification):
    result = build_recommendations(response_contract(codes), {"status": "completed",
        "http_observations": [{"method": "GET", "path": "/files/report.json", "status_code": status}]})
    assert result["uncovered_operations"] == []
    assert {item["status_code"] for item in result["uncovered_responses"]} == missing
    assert any(item["kind"] == "contract_clarification" for item in result["items"]) is clarification


@pytest.mark.parametrize("codes,undocumented", [(["200", "4XX"], False), (["200", "default"], False), (["200"], True)])
def test_repair_does_not_label_range_or_default_as_undocumented(codes, undocumented):
    from prototype.generator.repair import classify_result
    code = 'def test_file(api_client):\n    response = api_client.get("/files/report.json")\n    assert response.status_code == 200\n'
    run = RunResult(RunStatus.COMPLETED, 1, 0.1, tests=(Case("test_file.py::test_file", "failed", message="assert 404 == 200"),))
    contract = response_contract(codes)
    contract["paths"]["/files/report.json"] = contract["paths"].pop("/files/{name}.json")
    plan = classify_result(contract, {"test_file.py": code}, run, GenerationSettings("api.yaml"))
    assert plan.targets == ()
    assert ("not documented" in plan.suspected_defects[0]["reason"]) is undocumented


@pytest.mark.parametrize("fails", [False, True])
def test_bound_generation_replays_success_or_error_once(tmp_path, monkeypatch, fails):
    from prototype.service_tools.runner.tools import create_generation_tool
    import prototype.generator.pipeline as pipeline
    calls = []
    def execute(*args, **kwargs):
        calls.append(1)
        if fails:
            raise RuntimeError("synthetic failure")
        return GenerationRun("success", "api.yaml", None, 0, 0, 0, 0)
    monkeypatch.setattr(pipeline, "run_generation_pipeline", execute)
    settings = GenerationSettings("api.yaml", output_dir=tmp_path)
    tool = create_generation_tool(settings)
    with pytest.raises(ValueError):
        tool.invoke({"base_url": "http://other"})
    assert not calls
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tool.invoke({}), range(8)))
    assert all(result == results[0] for result in results)
    assert len(calls) == 1
    create_generation_tool(settings).invoke({})
    assert len(calls) == 2


@pytest.mark.parametrize("exception", [requests.ReadTimeout, requests.ConnectTimeout, TimeoutError])
def test_microcks_timeout_preserves_verdict_and_closes_session(tmp_path, monkeypatch, exception):
    from prototype.service_tools import backends
    from prototype.service_tools.selection import ToolSettings, execute_selected
    closed = []
    def request(*args, **kwargs):
        raise exception("synthetic timeout")
    monkeypatch.setattr(backends, "_session", lambda: SimpleNamespace(request=request, close=lambda: closed.append(True)))
    result = execute_selected(ToolSettings("service.proto", "http://grpc:9090", intent="multi",
        output_dir=tmp_path, microcks_service_id="service", microcks_runner="GRPC_PROTOBUF"))
    assert result["status"] == "timeout"
    assert closed == [True]
    assert result["duration_seconds"] >= 0


@pytest.mark.parametrize("mode", [[], ["--inspect-contract"], ["--generate", "--base-url", "http://api:8080", "--network", "api"]])
@pytest.mark.parametrize("content", [None, "paths: [", "openapi: 3.1.0\npaths: []"])
def test_cli_input_failure_has_report_without_agent(tmp_path, monkeypatch, mode, content):
    import prototype
    path = tmp_path / "bad.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    output = tmp_path / "reports"
    monkeypatch.setattr("sys.argv", ["tester", str(path), "--report-dir", str(output), *mode])
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("Invalid input must not reach LLM"))
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2
    reports = list(output.rglob("input-error.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["status"] == "input_error" and data["stage"] == "contract_input"
    assert data["error"] and reports[0].with_suffix(".md").exists()


def test_cli_unwritable_report_destination_keeps_primary_error(tmp_path, monkeypatch, capsys):
    import prototype
    destination = tmp_path / "not-a-directory"
    destination.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tester", str(tmp_path / "missing.yaml"), "--inspect-contract",
                                      "--report-dir", str(destination)])
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2 and destination.read_text(encoding="utf-8") == "keep me"
    diagnostic = capsys.readouterr().err
    assert "отчёт" in diagnostic.lower() and "FileNotFoundError" in diagnostic
    assert "Traceback" not in diagnostic


@pytest.mark.parametrize("mode", [[], ["--inspect-contract"], ["--generate", "--base-url", "http://api:8080", "--network", "api"]])
def test_cli_invalid_url_report_does_not_record_credentials(tmp_path, monkeypatch, mode, capsys):
    import prototype
    monkeypatch.setattr("sys.argv", ["tester", "http://user:FAKE_PASSWORD@api/schema", "--report-dir", str(tmp_path), *mode])
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2
    reports = list(tmp_path.rglob("input-error.*"))
    assert len(reports) == 2
    assert all("FAKE_PASSWORD" not in path.read_text(encoding="utf-8") for path in reports)
    assert "FAKE_PASSWORD" not in capsys.readouterr().err


def test_repeated_tool_facts_remain_one_run_in_cli_and_metrics(tmp_path, monkeypatch):
    import prototype
    from langchain_core.messages import ToolMessage
    from prototype.evaluate.metrics import calculate_metrics
    contract = tmp_path / "api.yaml"
    contract.write_text("openapi: 3.1.0\npaths: { /items: {get: {}} }", encoding="utf-8")
    full = {"tool": "generate_tests_tool", "pipeline_status": "success", "status": "success"}
    messages = [ToolMessage(content=json.dumps(full), artifact=full, tool_call_id=str(i)) for i in range(2)]
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: SimpleNamespace(
        stream=lambda *a, **kw: iter([{"messages": messages}])))
    received = []
    def metrics(**kwargs):
        received.extend(kwargs["agent_result"]["messages"])
        return calculate_metrics(**kwargs)
    monkeypatch.setattr("prototype.evaluate.metrics.calculate_metrics", metrics)
    code = prototype.run(str(contract), report_dir=tmp_path / "reports", generation_settings=GenerationSettings(contract))
    assert code == 0
    assert len(received) == 1
    metadata = json.loads(next((tmp_path / "reports").rglob("run-metadata.json")).read_text(encoding="utf-8"))
    assert len(metadata["generation_reports"]) == 1


@pytest.mark.skipif(os.getenv("RUN_RUNNER_DOCKER_TESTS") != "1", reason="requires runner image")
@pytest.mark.parametrize("code,expected,exit_code,passed,skipped", [
    ("def test_ok():\n    assert True\n", "success", 0, 1, 0),
    ("import pytest\n@pytest.mark.skip(reason='controlled skip')\ndef test_skip():\n    assert True\n", "completed_without_execution", 0, 0, 1),
    ("class ExitPlugin:\n    def pytest_sessionfinish(self, session, exitstatus):\n        session.exitstatus = 1\ndef test_ok(request):\n    request.config.pluginmanager.register(ExitPlugin())\n    assert True\n", "completed_with_failures", 1, 1, 0),
])
def test_real_isolated_pipeline_verdict_and_saved_report(tmp_path, code, expected, exit_code, passed, skipped):
    from prototype.generator.pipeline import run_generation_pipeline
    from prototype.tests.test_m3_integration import ScriptedModel
    from prototype.service_tools.runner.docker_runner import run_tests
    from prototype.service_tools.runner.contracts import RunConfig
    path = tmp_path / "api.yaml"
    path.write_text("openapi: 3.1.0\npaths: { /items: {get: {}} }", encoding="utf-8")
    model = ScriptedModel([json.dumps({"files": [{"name": "test_cases.py", "code": code}]})])
    run = run_generation_pipeline(GenerationSettings(path, output_dir=tmp_path / "reports", output_root=tmp_path / "saved"), model=model)
    assert run.status == expected and run.attempts == 0 and len(model.calls) == 1
    assert run.run_result["exit_code"] == exit_code
    assert run.run_result["summary"]["passed"] == passed and run.run_result["summary"]["skipped"] == skipped
    report = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
    assert report["status"] == expected
    replay = run_tests(RunConfig(run.saved_versions[0]["tests_dir"], tmp_path / "replay"))
    assert replay.exit_code == exit_code and len(model.calls) == 1


def test_yaml_date_key_is_reported_before_model_call(tmp_path):
    from prototype.generator.pipeline import run_generation_pipeline
    path = tmp_path / "api.yaml"
    path.write_text("openapi: 3.1.0\npaths:\n  /items:\n    get:\n      responses:\n        '200':\n          description: OK\n          content:\n            application/json:\n              schema:\n                default:\n                  2026-10-14: value\n", encoding="utf-8")
    class NoModel:
        def invoke(self, messages):
            pytest.fail("Invalid contract must not reach the model")
    run = run_generation_pipeline(GenerationSettings(path, output_dir=tmp_path / "reports"), model=NoModel())
    assert run.status == "generation_error" and run.run_result is None
    assert "Ключи" in run.generator_errors[0]
    assert run.total_tokens == 0 and run.saved_versions == ()


@pytest.mark.parametrize("failure,status", [
    (requests.ConnectionError("connect failed"), "infrastructure_error"),
    (ValueError("invalid JSON"), "input_error"),
    (RuntimeError("Microcks HTTP 500"), "infrastructure_error"),
])
def test_microcks_other_errors_are_not_timeouts(tmp_path, monkeypatch, failure, status):
    from prototype.service_tools import backends
    from prototype.service_tools.selection import ToolSettings, execute_selected
    closed = []
    def request(*args, **kwargs):
        raise failure
    monkeypatch.setattr(backends, "_session", lambda: SimpleNamespace(request=request, close=lambda: closed.append(True)))
    result = execute_selected(ToolSettings("service.proto", "http://grpc:9090", intent="multi",
        output_dir=tmp_path, microcks_service_id="service", microcks_runner="GRPC_PROTOBUF"))
    assert result["status"] == status and closed == [True]


def test_microcks_poll_timeout_keeps_last_received_facts(tmp_path, monkeypatch):
    from prototype.service_tools import backends
    from prototype.service_tools.selection import ToolSettings, execute_selected
    partial = {"id": "test-id", "inProgress": True, "testCaseResults": []}
    calls, closed = [], []
    def request(method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            return SimpleNamespace(status_code=201, json=lambda: partial)
        raise requests.ReadTimeout("synthetic poll timeout")
    monkeypatch.setattr(backends, "_session", lambda: SimpleNamespace(request=request, close=lambda: closed.append(True)))
    result = execute_selected(ToolSettings("service.proto", "http://grpc:9090", intent="multi",
        output_dir=tmp_path, microcks_service_id="service", microcks_runner="GRPC_PROTOBUF"))
    assert calls == ["POST", "GET"] and closed == [True]
    assert result["status"] == "timeout" and result["partial"] is True
    assert result["raw"] == partial and "success" not in result and "summary" not in result
    report = json.loads(Path(result["report_paths"]["json"]).read_text(encoding="utf-8"))
    assert report["raw"] == partial


def test_yaml_integer_pointer_and_date_value_stay_json_compatible():
    contract = {"responses": {200: {"description": "OK"}}, "x-target": {"$ref": "#/responses/200"},
                "default": {"day": date(2026, 10, 14)}}
    normalized = normalize_contract(contract)
    assert normalized["x-target"] == {"description": "OK"}
    assert normalized["default"]["day"] == "2026-10-14"
    json.dumps(normalized, allow_nan=False)
    assert 200 in contract["responses"]


@pytest.mark.parametrize("status", ["completed_with_failures", "completed_without_execution"])
def test_cli_rejects_non_success_generation_status(tmp_path, monkeypatch, status):
    import prototype
    from langchain_core.messages import ToolMessage
    contract = tmp_path / "api.yaml"
    contract.write_text("openapi: 3.1.0\npaths: { /items: {get: {}} }", encoding="utf-8")
    message = ToolMessage(content=json.dumps({"tool": "generate_tests_tool", "pipeline_status": status}), tool_call_id="one")
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: SimpleNamespace(stream=lambda *a, **kw: iter([{"messages": [message]}])))
    assert prototype.run(str(contract), report_dir=tmp_path / "reports", generation_settings=GenerationSettings(contract)) == 1


@pytest.mark.parametrize("family", [1, 2, 3, 4, 5])
def test_response_range_handles_each_http_family(family):
    from prototype.generator.context import match_response_definition
    assert match_response_definition({f"{family}XX": {}, "default": {}, "x-note": {}}, family * 100 + 99) == f"{family}XX"


def test_optional_source_error_has_cli_report_without_model(tmp_path, monkeypatch):
    import prototype
    contract = tmp_path / "api.yaml"
    contract.write_text("openapi: 3.1.0\npaths: { /items: {get: {}} }", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tester", str(contract), "--source-code", str(tmp_path / "absent.py"),
                                      "--report-dir", str(tmp_path / "reports")])
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("Invalid source must not reach LLM"))
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2
    report = json.loads(next((tmp_path / "reports").rglob("input-error.json")).read_text(encoding="utf-8"))
    assert "Исходники" in report["error"]
