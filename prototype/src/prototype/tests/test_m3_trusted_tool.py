"""The LLM-facing generation tool cannot select network policy or limits."""

from prototype.generator.contracts import GenerationRun, GenerationSettings


def test_tool_binds_trusted_settings_without_llm_parameters(tmp_path, monkeypatch):
    from prototype.service_tools.runner.tools import create_generation_tool
    from prototype.generator import pipeline

    settings = GenerationSettings(contract_path=tmp_path / "contract.yaml",
        base_url="http://catalogue:8080", network="trusted-api", blocked_networks=("database",),
        output_dir=tmp_path, runner_timeout_seconds=20, max_repair_attempts=2)
    captured = []
    def run(config):
        captured.append(config)
        return GenerationRun("success", config.contract_path, None, 0, 0, 0, 0)
    monkeypatch.setattr(pipeline, "run_generation_pipeline", run)
    bound = create_generation_tool(settings)
    assert bound.args_schema.model_json_schema().get("properties", {}) == {}
    assert bound.invoke({})["pipeline_status"] == "success"
    assert captured == [settings]


def test_tool_rejects_attempt_to_override_trusted_network(tmp_path, monkeypatch):
    import pytest
    from prototype.service_tools.runner.tools import create_generation_tool
    from prototype.generator import pipeline
    settings = GenerationSettings(contract_path=tmp_path / "contract.yaml", network="trusted-api")
    captured = []
    monkeypatch.setattr(pipeline, "run_generation_pipeline", lambda config: captured.append(config))
    with pytest.raises((ValueError, TypeError)):
        create_generation_tool(settings).invoke({"network": "host", "max_repair_attempts": 999})
    assert not captured
