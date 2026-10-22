"""TZ v3 4.1.7: bounded repair and recommendations from actual observations."""
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from prototype.generator.contracts import GenerationSettings
from prototype.generator.pipeline import run_generation_pipeline
from prototype.tests.test_generator_pipeline import FakeModel, FakeSaver, ScriptedRunner, contract
from prototype.service_tools.runner.contracts import RunResult, TestResult as CaseResult


def test_zero_attempts_disables_repair_without_an_extra_model_call(tmp_path):
    model = FakeModel()
    result = run_generation_pipeline(GenerationSettings(contract(tmp_path), max_repair_attempts=0,
        output_dir=tmp_path), model=model, run_tests_fn=ScriptedRunner(), save_fn=FakeSaver())
    assert result.attempts == 0 and len(model.calls) == 1
    assert result.status == "completed_with_failures"


def test_unknown_name_error_requires_review_without_llm_repair(tmp_path):
    model = FakeModel()
    runner = lambda config: RunResult("completed", 1, .1, tests=(CaseResult(
        "test_catalogue.py::test_good", "failed", message="NameError: name 'mystery' is not defined"),))
    result = run_generation_pipeline(GenerationSettings(contract(tmp_path), output_dir=tmp_path),
        model=model, run_tests_fn=runner, save_fn=FakeSaver())
    assert len(model.calls) == 1 and result.attempts == 0
    assert any(x["kind"] == "review_required" for x in result.recommendations["items"])


def test_recommendations_use_observed_requests_not_unexecuted_branch(tmp_path):
    from prototype.reports.recommendations import build_recommendations
    schema = {"openapi": "3.0.3", "paths": {"/items": {"get": {"responses": {"200": {}, "404": {}}}},
        "/unused": {"get": {"responses": {"200": {}}}}}}
    run = RunResult("completed", 0, .1, tests=(CaseResult("test_api.py::test_one", "passed"),),
        http_observations=({"nodeid": "test_api.py::test_one", "method": "GET", "path": "/items", "status_code": 200},))
    result = build_recommendations(schema, run.to_dict())
    assert result["uncovered_operations"] == ["GET /unused"]
    assert result["uncovered_responses"] == [{"operation": "GET /items", "status_code": "404"},
        {"operation": "GET /unused", "status_code": "200"}]
    assert result["basis"] == "observed_http"


def test_missing_observations_does_not_invent_coverage():
    from prototype.reports.recommendations import build_recommendations
    result = build_recommendations({"paths": {"/items": {"get": {"responses": {"200": {}}}}}},
        RunResult("completed", 0, .1, tests=(CaseResult("test_api.py::test_one", "passed"),)).to_dict())
    assert result["uncovered_operations"] is None and result["uncovered_responses"] is None
    assert result["basis"] == "unavailable"


def test_recommendations_are_saved_in_generation_json_and_markdown(tmp_path):
    model = FakeModel()
    result = run_generation_pipeline(GenerationSettings(contract(tmp_path), output_dir=tmp_path),
        model=model, run_tests_fn=ScriptedRunner(), save_fn=FakeSaver())
    payload = json.loads(Path(result.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["recommendations"] == result.recommendations
    assert "Рекомендации" in Path(result.report_paths["markdown"]).read_text(encoding="utf-8")


def test_trusted_client_records_status_and_path_without_query_or_headers(monkeypatch):
    from prototype.service_tools.runner import fixtures
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404); self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    monkeypatch.setattr(fixtures, "observe_http", lambda **event: seen.append(event))
    fixture = fixtures.api_client.__wrapped__(f"http://127.0.0.1:{server.server_port}")
    try:
        session = next(fixture)
        assert session.get(f"http://127.0.0.1:{server.server_port}/items?key=secret",
                           headers={"Authorization": "Bearer secret"}).status_code == 404
    finally:
        fixture.close(); server.shutdown(); server.server_close(); thread.join(2)
    assert seen == [{"method": "GET", "path": "/items", "status_code": 404, "request_body": {"kind": "absent"}}]


def test_worker_events_preserve_observations_without_changing_test_outcome(tmp_path):
    from prototype.service_tools.runner.docker_runner import read_result
    events = [{"kind": "start", "version": 1, "http_observation": "api_client"},
        {"kind": "http", "nodeid": "test_one.py::test_one", "method": "GET", "path": "/items", "status_code": 200},
        {"kind": "phase", "nodeid": "test_one.py::test_one", "phase": "call", "outcome": "passed", "duration_seconds": .1},
        {"kind": "phase", "nodeid": "test_one.py::test_one", "phase": "teardown", "outcome": "passed", "duration_seconds": .1},
        {"kind": "finish", "exit_code": 0}]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    result = read_result(path, .2)
    assert result.status == "completed" and result.to_dict()["summary"]["passed"] == 1
    assert result.to_dict()["http_observations"][0]["status_code"] == 200


def test_generation_truncation_has_clear_diagnostic_and_recommendations(tmp_path):
    from types import SimpleNamespace
    class Truncated:
        def invoke(self, messages):
            return SimpleNamespace(content='{"files": [', response_metadata={"finish_reason": "length"},
                                   usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
    result = run_generation_pipeline(GenerationSettings(contract(tmp_path), output_dir=tmp_path), model=Truncated())
    assert result.status == "generation_error"
    assert any("truncated" in message for message in result.generator_errors)
    assert result.recommendations["basis"] == "unavailable"


def test_recommendations_direct_import_works_in_a_fresh_process():
    import subprocess,sys
    result = subprocess.run([sys.executable, "-c", "from prototype.reports.recommendations import build_recommendations; print(build_recommendations({})['basis'])"],
                            capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr


def test_environment_failure_has_recovery_recommendation():
    from prototype.reports.recommendations import build_recommendations
    result = build_recommendations({}, {"status": "infrastructure_error"})
    assert any(x["kind"] == "environment" for x in result["items"])


def test_response_extensions_are_not_recommended_as_uncovered_status_codes():
    from prototype.reports.recommendations import build_recommendations
    schema = {"paths": {"/items": {"get": {"responses": {"200": {}, "x-note": "documentation"}}}}}
    result = build_recommendations(schema, {"status": "completed", "http_observations": [
        {"method": "GET", "path": "/items", "status_code": 200}]})
    assert result["uncovered_responses"] == []


def test_client_observes_only_target_and_normalizes_base_path(monkeypatch):
    import requests
    from prototype.service_tools.runner import fixtures
    seen = []
    monkeypatch.setattr(fixtures, "observe_http", lambda **event: seen.append(event))
    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    fixture = fixtures.api_client.__wrapped__("http://api:8080/v1")
    try:
        session = next(fixture)
        session.get("http://api:8080/v1/items?secret=hidden")
        session.get("http://other:8080/v1/items")
        session.get("http://api:9090/v1/items")
        session.get("http://api:8080/v10/items")
    finally:
        fixture.close()
    assert seen == [{"method": "GET", "path": "/items", "status_code": 200, "request_body": {"kind": "absent"}}]


def test_truncated_repair_response_is_not_accepted_as_a_complete_candidate():
    from types import SimpleNamespace
    from prototype.generator.repair import RepairTarget, repair_call
    class Model:
        def invoke(self, messages):
            return SimpleNamespace(content="def test_one():\n    assert True\n", response_metadata={"finish_reason": "length"},
                usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
    result = repair_call(Model(), GenerationSettings("api.yaml"), {},
        RepairTarget("file", "test_one.py"), "def test_one():\n    assert True\n")
    assert not result.ok and "truncated" in result.error
    assert result.tokens == (10, 20, 30)
