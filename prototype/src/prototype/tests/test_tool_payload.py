"""The model sees a bounded payload; trusted consumers retain all run facts."""
import json
from prototype.generator.contracts import GenerationSettings
from prototype.tests.test_generation_tool import _fake_run


def test_large_generation_result_is_compact_in_content_and_complete_in_artifact(monkeypatch):
    from prototype.service_tools.runner.tools import create_generation_tool
    defects = tuple({"nodeid": f"test_api.py::test_{i}", "reason": "API assertion failed", "message": "X" * 6000}
                    for i in range(100))
    run = _fake_run(suspected_defects=defects)
    monkeypatch.setattr("prototype.generator.pipeline.run_generation_pipeline", lambda *a, **kw: run)
    tool = create_generation_tool(GenerationSettings("contract.yaml"))
    message = tool.invoke({"type": "tool_call", "name": "generate_tests_tool", "args": {}, "id": "one"})
    assert len(message.content) <= 6000
    compact = json.loads(message.content)
    assert compact["summary"] == run.run_result["summary"]
    assert len(message.artifact["suspected_defects"]) == 100
    assert message.artifact["suspected_defects"][0]["message"] == "X" * 6000


def test_primary_cli_passes_complete_tool_facts_to_existing_metrics(tmp_path,monkeypatch):
    import prototype
    from types import SimpleNamespace
    from prototype.service_tools.runner.tools import create_generation_tool
    import prototype.evaluate.metrics as metrics
    source=tmp_path/'api.yaml'
    source.write_text("openapi: 3.0.3\npaths:\n  /items:\n    get: {}",encoding='utf-8')
    run=_fake_run(suspected_defects=tuple({"nodeid":str(i),"message":"X"*5000} for i in range(100)))
    monkeypatch.setattr('prototype.generator.pipeline.run_generation_pipeline',lambda *a,**kw:run)
    settings=GenerationSettings(source)
    message=create_generation_tool(settings).invoke({"type":"tool_call","name":"generate_tests_tool","args":{},"id":"one"})
    monkeypatch.setattr(prototype,'build_agent',lambda **kw:SimpleNamespace(stream=lambda *a,**kw:iter([{'messages':[message]}])))
    original=metrics.calculate_metrics
    captured=[]
    def calculate(**kwargs):
        captured.append(json.loads(kwargs['agent_result']['messages'][0].content))
        return original(**kwargs)
    monkeypatch.setattr(metrics,'calculate_metrics',calculate)
    prototype.run(str(source),report_dir=tmp_path/'reports',generation_settings=settings)
    assert len(captured[0]['suspected_defects'])==100
    assert len(message.content)<=6000


def test_selected_tool_keeps_full_facts_in_artifact_and_compact_content(tmp_path):
    from prototype.service_tools.selection import ToolSettings, create_selected_tool
    failures = [{"operation": "GET /items", "message": "X" * 5000}] * 100
    tool = create_selected_tool(ToolSettings("api.json", "http://api:8080", output_dir=tmp_path),
        execute_fn=lambda settings: {"backend": "schemathesis", "status": "completed", "success": False,
            "summary": {"failed": 100}, "failures": failures, "raw": {"failures": failures}})
    message = tool.invoke({"type": "tool_call", "name": "schemathesis_tool", "args": {}, "id": "one"})
    assert len(message.content) <= 6000
    assert message.artifact["failures"] == failures
    assert json.loads(message.content)["summary"]["failed"] == 100


def test_default_schemathesis_tool_also_keeps_large_details_out_of_model_content(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from prototype.service_tools.runner.tools import schemathesis_tool
    source = tmp_path / "api.yaml"
    source.write_text("openapi: 3.0.3\npaths: {/items: {get: {}}}", encoding="utf-8")
    failures = [{"operation": "GET /items", "message": "X" * 5000}] * 100
    payload = {"status": "success", "operations": 1, "scenarios": {
        "total": 100, "passed": 0, "failed": 100, "errors": 0, "skipped": 0}, "failures": failures}
    monkeypatch.setattr("prototype.service_tools.runner.tools.requests.post", lambda *a, **kw:
                        SimpleNamespace(status_code=200, json=lambda: payload))
    message = schemathesis_tool.invoke({"type": "tool_call", "name": "schemathesis_tool",
        "args": {"contract_path": str(source), "base_url": "http://api:8080"}, "id": "one"})
    assert len(message.content) <= 6000
    assert message.artifact["failures"] == failures
    assert json.loads(message.content)["scenarios"]["failed"] == 100


def test_selected_tool_agent_records_its_actual_prompt_hash(tmp_path, monkeypatch):
    import hashlib
    import prototype
    from types import SimpleNamespace
    from prototype.service_tools.selection import ToolSettings
    source = tmp_path / "api.yaml"
    source.write_text("openapi: 3.0.3\npaths: {/items: {get: {}}}", encoding="utf-8")
    settings = ToolSettings(source, intent="mock", output_dir=tmp_path)
    agent = SimpleNamespace(stream=lambda *a, **kw: iter([{"messages": []}]))
    prompts = []
    def create(**kwargs):
        prompts.append(kwargs["system_prompt"])
        return agent
    monkeypatch.setattr("langchain.agents.create_agent", create)
    monkeypatch.setattr("prototype.llm.model.build_model", lambda: SimpleNamespace(model_name="offline"))
    prototype.run(str(source), report_dir=tmp_path / "reports", testing_settings=settings)
    metadata = json.loads(next((tmp_path / "reports").rglob("run-metadata.json")).read_text(encoding="utf-8"))
    expected = hashlib.sha256(prompts[0].encode("utf-8")).hexdigest()
    assert metadata["prompts"]["agent_sha256"] == expected
