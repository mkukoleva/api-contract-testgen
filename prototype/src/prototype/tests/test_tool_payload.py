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
