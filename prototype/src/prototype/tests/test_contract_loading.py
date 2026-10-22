"""4.1.1: actual HTTP input and static Python context; no paid model calls."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import os

import pytest

from prototype.parser.contract import read_contract_summary


SPEC = {"openapi": "3.1.0", "info": {"title": "Items API", "version": "2.0"},
        "paths": {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}}}


@pytest.mark.parametrize("responses", [None, [], "200"])
def test_invalid_response_mapping_is_rejected_before_generation(tmp_path, responses):
    from prototype.parser.contract import ContractInputError
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps({"openapi": "3.0.3", "paths": {
        "/items": {"get": {"responses": responses}}}}), encoding="utf-8")
    with pytest.raises(ContractInputError, match="responses"):
        read_contract_summary(source)


@pytest.mark.parametrize("example", ["!!binary aGVsbG8=", ".nan", ".inf", "!!set {a: null}"])
def test_non_json_yaml_values_have_a_readable_input_error(tmp_path, example):
    from prototype.parser.contract import ContractInputError
    source = tmp_path / "invalid.yaml"
    source.write_text("openapi: 3.0.3\npaths:\n  /items:\n    get: {}\nx-example: " + example,
                      encoding="utf-8")
    with pytest.raises(ContractInputError, match="JSON"):
        read_contract_summary(source)


@pytest.fixture
def schema_server(request):
    requests = []
    body = ((Path(__file__).resolve().parents[4] / "benchmark/catalogue/catalogue.swagger.json").read_bytes()
            if getattr(request, "param", None) == "catalogue" else json.dumps(SPEC).encode())
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/schema")
                self.end_headers()
                return
            if self.path in {"/loop", "/bad-redirect"}:
                self.send_response(302)
                self.send_header("Location", "/loop" if self.path == "/loop" else "file:///secret.json")
                self.end_headers()
                return
            if self.path == "/large":
                self.send_response(200)
                self.send_header("Content-Length", str(6 * 1024 * 1024))
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(.2)
            status = 404 if self.path == "/missing" else 200
            payload = b"paths: [" if self.path == "/bad" else (
                b"swagger: '2.0'\ninfo: {title: YAML API, version: '1'}\npaths:\n  /items:\n    get: {}\n"
                if self.path == "/yaml" else body)
            if self.path == "/ref":
                spec = SPEC | {"paths": {"/items": {"get": {"responses": {"200": {
                    "$ref": f"http://127.0.0.1:{self.server.server_port}/external"}}}}}}
                payload = json.dumps(spec).encode()
            if self.path == "/external":
                payload = json.dumps({"description": "External OK", "content": {
                    "application/json": {"schema": {"type": "object",
                                                     "required": ["external"],
                                                     "properties": {
                                                         "external": {"type": "string"}}}}}}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_url_contract_is_loaded_and_has_the_same_summary(schema_server):
    url, requests, _ = schema_server
    summary = read_contract_summary(url + "/schema")
    assert summary["file"] == url + "/schema"
    assert summary["title"] == "Items API" and summary["version"] == "2.0"
    assert summary["operations"] == ["GET /items"]
    assert requests == ["/schema"]


@pytest.mark.parametrize("path", ["/missing", "/bad"])
def test_http_or_format_failure_is_a_readable_input_error(schema_server, path):
    url, _, _ = schema_server
    from prototype.parser.contract import ContractInputError
    with pytest.raises(ContractInputError):
        read_contract_summary(url + path)


def test_inspect_cli_downloads_once_and_saves_contract_identity_without_agent(schema_server, tmp_path, monkeypatch):
    import prototype
    url, requests, body = schema_server
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("Inspection must not create an agent"))
    monkeypatch.setattr("sys.argv", ["tester", url + "/redirect", "--inspect-contract", "--report-dir", str(tmp_path)])
    prototype.main()
    data = json.loads(next(tmp_path.rglob("contract-report.json")).read_text(encoding="utf-8"))
    assert data["contract"]["file"] == url + "/redirect"
    assert data["contract"]["resolved_url"] == url + "/schema"
    assert data["contract"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert data["contract"]["operation_count"] == 1
    assert Path(data["contract"]["snapshot_path"]).read_bytes() == body
    assert requests == ["/redirect", "/schema"]


def test_python_fastapi_context_is_static_relevant_and_reaches_generation(tmp_path):
    from prototype.generator import GenerationSettings, run_generation_pipeline
    from prototype.tests.test_generator_pipeline import FakeModel, ScriptedRunner
    api = tmp_path / "api.py"
    marker = tmp_path / "executed.txt"
    api.write_text(f'''from pathlib import Path
from fastapi import FastAPI
Path({str(marker)!r}).write_text("must never execute")
app = FastAPI()
@app.get("/items")
def list_items():
    return [{{"id": 1}}]
@app.get("/other")
def unrelated():
    return "UNRELATED_CODE"
''', encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(SPEC), encoding="utf-8")
    model = FakeModel()
    model.generation_files = [{"name": "test_catalogue.py", "code": "def test_good():\n    assert True\n"}]
    result = run_generation_pipeline(GenerationSettings(contract_path=contract, source_code_path=api,
        output_dir=tmp_path / "reports", output_root=tmp_path / "saved"), model=model, run_tests_fn=ScriptedRunner())
    assert result.status == "success"
    message = model.calls[0][1][-1]["content"]
    assert "list_items" in message and 'app.get("/items")' in message
    assert "UNRELATED_CODE" not in message and "must never execute" not in message
    assert not marker.exists()
    assert result.provenance["source_code"]["provided"] is True
    assert result.provenance["source_code"]["files"][0]["sha256"] == hashlib.sha256(api.read_bytes()).hexdigest()


def test_pipeline_url_input_uses_one_download_and_a_persistent_snapshot(schema_server, tmp_path):
    from prototype.generator import GenerationSettings, run_generation_pipeline
    from prototype.tests.test_generator_pipeline import FakeModel, ScriptedRunner
    url, requests, body = schema_server
    model = FakeModel()
    model.generation_files = [{"name": "test_catalogue.py", "code": "def test_good():\n    assert True\n"}]
    result = run_generation_pipeline(GenerationSettings(contract_path=url + "/schema",
        output_dir=tmp_path / "reports", output_root=tmp_path / "saved"), model=model, run_tests_fn=ScriptedRunner())
    assert result.status == "success", result.to_dict()
    assert str(result.contract_path) == url + "/schema"
    assert requests == ["/schema"]
    assert Path(result.provenance["contract"]["snapshot_path"]).read_bytes() == body
    manifest = json.loads((Path(result.saved_versions[-1]["tests_dir"]).parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert manifest["generator"]["meta"]["provenance"]["contract"]["file"] == url + "/schema"


@pytest.mark.parametrize("location", ["ftp://localhost/schema", "file:///schema", "http://user:private@localhost/schema",
                                      "http://localhost:99999/schema", "http://localhost/schema#fragment", "https://"])
def test_invalid_url_is_rejected_without_network_or_credential_echo(location):
    from prototype.parser.contract_input import normalize_location, ContractInputError
    with pytest.raises(ContractInputError) as error:
        normalize_location(location)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("path", ["/loop", "/bad-redirect", "/large", "/slow"])
def test_bounded_download_reports_redirect_size_and_timeout_failures(schema_server, monkeypatch, path):
    from prototype.parser import contract
    url, requests, _ = schema_server
    monkeypatch.setattr(contract, "CONTRACT_DOWNLOAD_TIMEOUT", .05)
    with pytest.raises(contract.ContractInputError):
        contract.load_contract_document(url + path)
    assert len(requests) <= 5


def test_url_yaml_and_swagger_are_supported_and_external_ref_is_resolved(schema_server):
    url, requests, _ = schema_server
    assert read_contract_summary(url + "/yaml")["specification"] == "Swagger 2.0"
    assert read_contract_summary(url + "/ref")["operations"] == ["GET /items"]
    # The external $ref is fetched once at load time through the bounded path.
    assert requests == ["/yaml", "/ref", "/external"]


def test_source_directory_context_contains_router_models_and_helpers_not_venv_or_side_effects(tmp_path):
    from prototype.parser.source_context import read_source_context
    source = tmp_path / "source"
    source.mkdir()
    (source / ".venv").mkdir()
    (source / ".venv" / "poison.py").write_text("def bad(:", encoding="utf-8")
    (source / "api.py").write_text('''from fastapi import FastAPI, APIRouter
from pydantic import BaseModel
app = FastAPI()
router = APIRouter(prefix="/v1")
app.include_router(router, prefix="/api")
class Item(BaseModel):
    id: int
def get_item():
    return Item(id=1)
@router.get("/items", response_model=list[Item])
async def list_items():
    return [get_item()]
raise RuntimeError("MODULE_SIDE_EFFECT")
''', encoding="utf-8")
    spec = SPEC | {"paths": {"/api/v1/items": {"get": {}}}}
    context = read_source_context(source, spec)
    assert context["metadata"]["matched_handlers"] == 1
    assert context["metadata"]["scanned_files"] == 1
    assert context["metadata"]["warnings"] == []
    assert "class Item" in context["text"] and "def get_item" in context["text"]
    assert "MODULE_SIDE_EFFECT" not in context["text"]


def test_optional_source_without_routes_or_with_invalid_files_is_reported_and_bounded(tmp_path):
    from prototype.parser.source_context import read_source_context, MAX_CONTEXT_CHARS
    assert read_source_context(None, SPEC)["metadata"]["provided"] is False
    (tmp_path / "bad.py").write_text("def bad(:", encoding="utf-8")
    (tmp_path / "plain.py").write_text("\n".join(f"def helper_{i}():\n    pass" for i in range(1000)), encoding="utf-8")
    context = read_source_context(tmp_path, SPEC)
    assert len(context["text"]) <= MAX_CONTEXT_CHARS
    assert context["metadata"]["truncated"]
    assert context["metadata"]["warnings"]
    assert "FunctionDef helper_0" in context["text"]


def test_recursive_router_mount_is_reported_without_executing_or_looping(tmp_path):
    from prototype.parser.source_context import read_source_context
    source = tmp_path / "api.py"
    source.write_text('''from fastapi import APIRouter
router = APIRouter()
router.include_router(router, prefix="/loop")
@router.get("/items")
def items():
    return []
''', encoding="utf-8")
    context = read_source_context(source, SPEC)
    assert any("цикл" in warning for warning in context["metadata"]["warnings"])
    assert "def items" in context["text"]


@pytest.mark.parametrize("field", ["title", "version"])
def test_malformed_contract_identity_is_rejected_readably(tmp_path, field):
    from prototype.parser.contract import ContractInputError
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(SPEC | {"info": {field: []}}), encoding="utf-8")
    with pytest.raises(ContractInputError, match=f"info.{field}"):
        read_contract_summary(str(path))


def test_legacy_cli_url_passes_local_snapshot_to_tools_and_retains_source_in_report(schema_server, tmp_path, monkeypatch):
    import prototype
    from types import SimpleNamespace
    url, requests, body = schema_server
    captured = []
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(invoke=lambda request:
        captured.append(request["messages"][0]["content"]) or {"messages": []}))
    prototype.run(url + "/schema", report_dir=tmp_path)
    data = json.loads(next(tmp_path.rglob("run-metadata.json")).read_text(encoding="utf-8"))
    assert data["contract"]["file"] == url + "/schema"
    snapshot = data["contract"]["snapshot_path"]
    assert Path(snapshot).read_bytes() == body
    assert json.dumps(snapshot, ensure_ascii=False) in captured[0]
    assert requests == ["/schema"]


def test_inspect_errors_exit_two_before_agent(tmp_path, monkeypatch, capsys):
    import prototype
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("invalid input must not create an agent"))
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tester", str(path), "--inspect-contract", "--report-dir", str(tmp_path / "reports")])
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2
    assert "объект JSON/YAML" in capsys.readouterr().err
    assert len(list((tmp_path / "reports").rglob("input-error.json"))) == 1


@pytest.mark.parametrize("endpoint", ["/missing", "/bad", "/loop", "/large", "/slow"])
def test_http_input_errors_leave_cli_diagnostics(schema_server, tmp_path, monkeypatch, endpoint):
    import prototype
    from prototype.parser import contract as parser_module
    url, _, _ = schema_server
    monkeypatch.setattr(parser_module, "CONTRACT_DOWNLOAD_TIMEOUT", .05 if endpoint == "/slow" else 2)
    monkeypatch.setattr("sys.argv", ["tester", url + endpoint, "--inspect-contract", "--report-dir", str(tmp_path)])
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("Failed download must not reach LLM"))
    with pytest.raises(SystemExit) as error:
        prototype.main()
    assert error.value.code == 2
    report = json.loads(next(tmp_path.rglob("input-error.json")).read_text(encoding="utf-8"))
    assert report["status"] == "input_error" and report["error"]
    assert not list(tmp_path.rglob("contract-report.json"))


def test_missing_source_is_an_input_error_before_model_call(tmp_path):
    from prototype.generator import GenerationSettings, run_generation_pipeline
    from prototype.tests.test_generator_pipeline import FakeModel
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(SPEC), encoding="utf-8")
    model = FakeModel()
    result = run_generation_pipeline(GenerationSettings(contract, source_code_path=tmp_path / "missing",
        output_dir=tmp_path / "reports"), model=model)
    assert result.status == "generation_error" and model.calls == []
    assert "Исходники" in result.generator_errors[0]


def test_core_cli_url_is_fetched_once_and_source_reaches_fixed_tool_settings(schema_server, tmp_path, monkeypatch):
    import prototype
    import prototype.llm.model as llm
    from prototype.tests.test_m3_agent import ScriptedAgentModel
    from prototype.tests.test_generator_pipeline import ScriptedRunner
    import prototype.service_tools.runner.docker_runner as runner
    url, requests, body = schema_server
    source = tmp_path / "api.py"
    source.write_text('@app.get("/items")\ndef items():\n    return []', encoding="utf-8")
    model = ScriptedAgentModel(code="def test_good():\n    assert True\n")
    monkeypatch.setattr(llm, "build_model", lambda: model)
    monkeypatch.setattr(runner, "run_tests", lambda config, **kw: ScriptedRunner()(config))
    monkeypatch.setattr("sys.argv", ["tester", url + "/schema", "--generate", "--source-code", str(source),
        "--base-url", "http://catalogue:8080", "--network", "pytest-runner-catalogue_runner",
        "--report-dir", str(tmp_path / "reports"), "--tests-output-dir", str(tmp_path / "saved")])
    prototype.main()
    assert requests == ["/schema"]
    metadata = json.loads(next((tmp_path / "reports").rglob("run-metadata.json")).read_text(encoding="utf-8"))
    assert metadata["contract"]["file"] == url + "/schema"
    assert metadata["source_code"]["provided"] is True
    report = json.loads(next((tmp_path / "reports").rglob("generation-report.json")).read_text(encoding="utf-8"))
    assert report["provenance"]["source_code"] == metadata["source_code"]
    assert report["provenance"]["contract"]["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.skipif(os.getenv("RUN_RUNNER_CATALOGUE_TESTS") != "1", reason="requires Catalogue and Docker runner")
@pytest.mark.parametrize("schema_server", ["catalogue"], indirect=True)
def test_url_and_source_through_real_agent_docker_storage_report_and_replay(schema_server, tmp_path, monkeypatch):
    import prototype
    import prototype.llm.model as llm
    from prototype.tests.test_m3_agent import ScriptedAgentModel
    from prototype.service_tools.runner.contracts import RunConfig
    from prototype.service_tools.runner.docker_runner import run_tests
    url, requests, body = schema_server
    source = tmp_path / "service.py"
    source.write_text('@app.get("/catalogue")\ndef list_catalogue():\n    return []', encoding="utf-8")
    model = ScriptedAgentModel()
    monkeypatch.setattr(llm, "build_model", lambda: model)
    monkeypatch.setenv("DEEPCODE_API_KEY", "dummy-key-for-isolation-check")
    monkeypatch.setattr("sys.argv", ["tester", url + "/schema", "--generate", "--source-code", str(source),
        "--base-url", "http://catalogue:8080", "--network", "pytest-runner-catalogue_runner",
        "--blocked-network", "pytest-runner-catalogue_database",
        "--report-dir", str(tmp_path / "reports"), "--tests-output-dir", str(tmp_path / "saved")])
    # URL/source plumbing succeeds; the intentionally partial scripted suite
    # must still expose missing categories through the primary CLI.
    with pytest.raises(SystemExit) as incomplete:
        prototype.main()
    assert incomplete.value.code == 1
    report = json.loads(next((tmp_path / "reports").rglob("generation-report.json")).read_text(encoding="utf-8"))
    data = {**report, "pipeline_status": report["status"], "summary": report["pytest"]["summary"]}
    assert data["pipeline_status"] == "incomplete_categories", data
    assert report['category_evidence']['status'] == 'incomplete'
    assert report['pytest']['exit_code'] == 0
    assert any('Missing required test categories' in error for error in report['generator_errors'])
    assert data["attempts"] == 1 and data["summary"]["passed"] == 1
    assert requests == ["/schema"]
    identity = report["provenance"]["contract"]
    assert identity["operation_count"] == 4 and identity["file"] == url + "/schema"
    assert identity["sha256"] == hashlib.sha256(body).hexdigest()
    assert report["provenance"]["source_code"]["matched_handlers"] == 1
    saved = Path(data["saved_versions"][-1]["tests_dir"])
    replay = run_tests(RunConfig(saved, tmp_path / "replay", base_url="http://catalogue:8080"),
                       network="pytest-runner-catalogue_runner", blocked_networks=("pytest-runner-catalogue_database",))
    assert replay.status == "completed" and replay.exit_code == 0, replay.to_dict()
    assert model.calls == ["agent-tool", "generate", "repair"]
    assert requests == ["/schema"]  # Replay needs neither contract download nor model.
