"""4.1.7.3: trusted selection, real client protocols and compact tool messages."""
import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize("intent,backend", [("quick", "schemathesis"), ("deep", "evomaster"),
                                           ("mock", "microcks"), ("multi", "microcks")])
def test_tool_selection_matches_intent(intent, backend):
    from prototype.service_tools.selection import select_backend
    assert select_backend(intent) == backend


def test_bound_tool_runs_once_and_rejects_model_configuration(tmp_path):
    from prototype.service_tools.selection import ToolSettings, create_selected_tool
    settings = ToolSettings("contract.json", "http://catalogue:8080", output_dir=tmp_path)
    calls = []
    def execute(settings):
        calls.append(settings)
        return {"backend": "schemathesis", "status": "completed", "summary": {"passed": 1}}
    tool = create_selected_tool(settings, execute_fn=execute)
    first = tool.invoke({})
    assert tool.invoke({}) == first and len(calls) == 1
    with pytest.raises(ValueError):
        tool.invoke({"base_url": "http://outside:80"})


def test_compact_result_preserves_counts_and_report_path(tmp_path):
    from prototype.service_tools.selection import compact_result
    result = compact_result({"backend": "schemathesis", "status": "completed", "summary": {"failed": 123},
        "failures": [{"message": "X" * 5000, "operation": "GET /items"}] * 100,
        "report_paths": {"json": str(tmp_path / "tool-report.json")}})
    assert len(json.dumps(result, ensure_ascii=False)) <= 6000
    assert result["summary"]["failed"] == 123
    assert result["report_paths"]["json"].endswith("tool-report.json")


def test_microcks_import_then_async_test_uses_documented_http_api(tmp_path):
    from prototype.service_tools.selection import ToolSettings
    from prototype.service_tools.backends import run_microcks
    artifact = tmp_path / "api.yaml"
    artifact.write_text("openapi: 3.0.3\ninfo: {title: Demo, version: '1'}\npaths: { /items: { get: {} } }", encoding="utf-8")
    calls = []
    replies = iter([SimpleNamespace(status_code=201, json=lambda: {}),
        SimpleNamespace(status_code=200, json=lambda: [{"id": "service-id", "name": "Demo", "version": "1", "type": "REST"}]),
        SimpleNamespace(status_code=201, json=lambda: {"id": "run-id", "inProgress": True}),
        SimpleNamespace(status_code=200, json=lambda: {"id": "run-id", "inProgress": False, "success": True,
            "testCaseResults": [{"success": True, "testStepResults": [{"success": True}]}]})])
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs)); return next(replies)
    result = run_microcks(ToolSettings(artifact, "http://catalogue:8080", intent="multi", output_dir=tmp_path),
                          request_fn=request, sleep_fn=lambda _: None)
    assert result["status"] == "completed" and result["success"] is True
    assert calls[0][1].endswith("/api/artifact/upload")
    assert calls[2][2]["json"]["runnerType"] == "OPEN_API_SCHEMA"
    assert calls[3][1].endswith("/api/tests/run-id")


def test_evomaster_command_has_fixed_policy_and_no_shell_or_llm_keys(tmp_path):
    from prototype.service_tools.selection import ToolSettings
    from prototype.service_tools.backends import evomaster_command
    settings = ToolSettings("contract.json", "http://catalogue:8080", intent="deep", output_dir=tmp_path,
                            network="pytest-runner-catalogue_runner", timeout_seconds=10)
    command = evomaster_command(settings, "test-run")
    assert "--pull=never" in command and "--blackBox" in command and "--maxTime" in command
    assert command[command.index("--maxTime") + 1] == "10s"
    assert not any("API_KEY" in part for part in command)
    assert "--network" in command and command[command.index("--network")+1] == settings.network


def test_grpc_microcks_target_uses_http_uri_syntax(tmp_path):
    from prototype.service_tools.selection import ToolSettings
    settings = ToolSettings("service.proto", "http://grpc:9090", intent="multi", output_dir=tmp_path,
                            microcks_service_id="id", microcks_runner="GRPC_PROTOBUF")
    assert settings.base_url == "http://grpc:9090"
    with pytest.raises(ValueError):
        ToolSettings("service.proto", "grpc:9090", intent="multi", output_dir=tmp_path,
                     microcks_service_id="id", microcks_runner="GRPC_PROTOBUF")


def test_empty_microcks_run_is_not_success(tmp_path):
    from prototype.service_tools.selection import ToolSettings
    from prototype.service_tools.backends import run_microcks
    response = SimpleNamespace(status_code=201, json=lambda: {"id": "id", "inProgress": False,
                                                             "success": True, "testCaseResults": []})
    result = run_microcks(ToolSettings("service.proto", "http://grpc:9090", intent="multi", output_dir=tmp_path,
        microcks_service_id="id", microcks_runner="GRPC_PROTOBUF"), request_fn=lambda *a, **kw: response)
    assert result["status"] == "no_tests" and result["success"] is False


def test_evomaster_input_is_normalized_json_even_if_cli_snapshot_is_yaml(tmp_path, monkeypatch):
    from prototype.service_tools.selection import ToolSettings
    from prototype.service_tools.backends import run_evomaster
    source = tmp_path / "snapshot.yaml"
    source.write_text('{\n\t"openapi":"3.0.3", "paths":{"/items":{"get":{}}}\n}',encoding="utf-8")
    output = tmp_path / "out"
    def execute(command, **kwargs):
        mount = next(part for part in command if "dst=/inputs/contract.json" in part)
        normalized = json.loads((output / "evomaster-contract.json").read_text(encoding="utf-8"))
        assert normalized["openapi"] == "3.0.3" and "readonly" in mount
        (output / "report.json").write_text(json.dumps({"totalTests": 1, "faults": {"totalNumber": 0},
            "problemDetails": {"rest": {"coveredHttpStatus": [{"endpointId": "GET:/items", "httpStatus": [200]}]}}}),encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("prototype.service_tools.backends.subprocess.run",execute)
    result = run_evomaster(ToolSettings(source,"http://catalogue:8080",intent="deep",output_dir=output,network="api"))
    assert result["status"] == "completed" and result["http_observations"][0]["status_code"] == 200


def test_cli_intent_reaches_trusted_agent_settings(tmp_path, monkeypatch):
    import prototype
    captured = []
    monkeypatch.setattr(prototype,"run",lambda path,**kwargs: captured.append(kwargs) or 0)
    monkeypatch.setattr("sys.argv",["tester","api.yaml","--test-intent","deep","--base-url","http://api:8080",
        "--network","api", "--report-dir",str(tmp_path)])
    prototype.main()
    assert captured[0]["testing_settings"].intent == "deep"


def test_tool_options_without_intent_are_not_silently_ignored(monkeypatch):
    import prototype
    monkeypatch.setattr(prototype,"run",lambda *a,**kw: pytest.fail("invalid configuration must not run"))
    monkeypatch.setattr("sys.argv",["tester","api.yaml","--microcks-runner","GRPC_PROTOBUF"])
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2


def test_missing_microcks_contract_version_recommends_fixing_input(tmp_path):
    from prototype.service_tools.selection import ToolSettings,execute_selected
    path=tmp_path/'api.yaml'
    path.write_text("openapi: 3.0.3\ninfo: {title: Demo}\npaths:\n  /items:\n    get: {}",encoding='utf-8')
    result=execute_selected(ToolSettings(path,intent='mock',output_dir=tmp_path/'reports'))
    assert result['status']=='input_error'
    assert any(item['kind']=='contract_clarification' for item in result['recommendations']['items'])
