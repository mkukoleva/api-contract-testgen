"""Actual LangChain -> M3 -> Docker -> storage/reports -> repeat without LLM."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import prototype
from prototype.generator.contracts import GenerationSettings
from prototype.service_tools.runner.contracts import RunConfig
from prototype.service_tools.runner.docker_runner import run_tests


class RepeatingToolModel(BaseChatModel):
    calls: int = 0
    tool_name: str = "generate_tests_tool"

    @property
    def _llm_type(self):
        return "repeating-tool-regression"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"name": self.tool_name, "args": {}, "id": f"repeat-{self.calls}"}]))])


class InvalidArgumentsModel(RepeatingToolModel):
    correct_on_retry: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 2:
            assert isinstance(messages[-1], ToolMessage)
            assert messages[-1].status == "error"
            assert "no tool executed" in messages[-1].content
        args = {} if self.calls == 2 and self.correct_on_retry else {"network": "host"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"name": "generate_tests_tool", "args": args, "id": f"call-{self.calls}"}]))])


@pytest.mark.parametrize("correct", [True, False])
def test_invalid_configuration_gets_one_correction_without_execution(tmp_path, monkeypatch, correct):
    from prototype.generator.contracts import GenerationRun
    model = InvalidArgumentsModel(correct_on_retry=correct)
    monkeypatch.setattr("prototype.llm.model.build_model", lambda: model)
    executions = []
    def pipeline(settings, **kwargs):
        executions.append(settings)
        return GenerationRun("success", settings.contract_path, None, 0, 0, 0, 0)
    monkeypatch.setattr("prototype.generator.pipeline.run_generation_pipeline", pipeline)
    settings = GenerationSettings(tmp_path / "contract.json", network="trusted-network")
    agent = prototype.build_agent(generation_settings=settings)
    request = {"messages": [{"role": "user", "content": "Generate tests"}]}
    if correct:
        result = agent.invoke(request)
        assert result["messages"][-1].artifact["pipeline_status"] == "success"
        assert executions == [settings] and executions[0].network == "trusted-network"
    else:
        with pytest.raises(ValueError, match="after one correction"):
            agent.invoke(request)
        assert executions == []
    assert model.calls == 2


@pytest.mark.parametrize("status", ["success", "generation_error", "infrastructure_error"])
def test_generation_agent_stops_at_actual_pipeline_result(tmp_path, monkeypatch, status):
    from prototype.generator.contracts import GenerationRun
    model = RepeatingToolModel()
    monkeypatch.setattr("prototype.llm.model.build_model", lambda: model)
    executions = []
    def pipeline(settings, **kwargs):
        executions.append(settings)
        return GenerationRun(status, settings.contract_path, None, 0, 0, 0, 0)
    monkeypatch.setattr("prototype.generator.pipeline.run_generation_pipeline", pipeline)
    settings = GenerationSettings(tmp_path / "contract.json")
    result = prototype.build_agent(generation_settings=settings).invoke(
        {"messages": [{"role": "user", "content": "Generate tests"}]})
    assert model.calls == 1 and executions == [settings]
    last = result["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.artifact["pipeline_status"] == status
    assert json.loads(last.content)["pipeline_status"] == status


def test_generation_agent_bounds_invalid_tool_loop(tmp_path, monkeypatch):
    from langgraph.errors import GraphRecursionError
    model = RepeatingToolModel(tool_name="unknown_tool")
    monkeypatch.setattr("prototype.llm.model.build_model", lambda: model)
    monkeypatch.setattr("prototype.generator.pipeline.run_generation_pipeline",
                        lambda *a, **kw: pytest.fail("Invalid tool must not run generation"))
    agent = prototype.build_agent(generation_settings=GenerationSettings(tmp_path / "contract.json"))
    with pytest.raises(GraphRecursionError):
        agent.invoke({"messages": [{"role": "user", "content": "Generate tests"}]})
    assert 1 < model.calls <= 4


class ScriptedAgentModel(BaseChatModel):
    model_name: str = "scripted-provider-model"
    calls: list[str] = Field(default_factory=list)
    tool_results: list[dict] = Field(default_factory=list)
    code: str = (
        "import os\n"
        "def test_catalogue(base_url, client):\n"
        "    assert 'DEEPCODE_API_KEY' not in os.environ\n"
        "    response = client.get(base_url + '/catalogue')\n"
        "    assert response.status_code == 200\n"
        "    assert isinstance(response.json(), list)\n")

    @property
    def _llm_type(self):
        return "m3-scripted-agent"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = messages[0].content
        if "генератор pytest-тестов" in system:
            self.calls.append("generate")
            message = AIMessage(content=json.dumps({"files": [{"name": "test_catalogue.py", "code": self.code}]}))
        elif "самовосстановления" in system:
            self.calls.append("repair")
            message = AIMessage(content=self.code.replace("client", "api_client"))
        elif isinstance(messages[-1], ToolMessage):
            self.calls.append("agent-final")
            self.tool_results.append(json.loads(messages[-1].content))
            message = AIMessage(content="Прогон завершён; результаты получены от инструмента.")
        else:
            self.calls.append("agent-tool")
            message = AIMessage(content="", tool_calls=[
                {"name": "generate_tests_tool", "args": {}, "id": "m3-call"}])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.skipif(os.getenv("RUN_RUNNER_CATALOGUE_TESTS") != "1", reason="requires Catalogue and runner")
def test_core_agent_receives_real_m3_results_and_saved_suite_replays_without_model(tmp_path, monkeypatch):
    import prototype.llm.model as llm
    monkeypatch.setenv("DEEPCODE_API_KEY", "dummy-key-for-isolation-check")
    model = ScriptedAgentModel()
    monkeypatch.setattr(llm, "build_model", lambda: model)
    settings = GenerationSettings(
        contract_path=Path(__file__).resolve().parents[4] / "benchmark/catalogue/catalogue.swagger.json",
        base_url="http://catalogue:8080", network="pytest-runner-catalogue_runner",
        blocked_networks=("pytest-runner-catalogue_database",),
        output_dir=tmp_path / "reports", output_root=tmp_path / "saved")
    monkeypatch.setattr("sys.argv", ["tester", str(settings.contract_path), "--generate",
        "--base-url", settings.base_url, "--network", settings.network,
        "--blocked-network", settings.blocked_networks[0],
        "--report-dir", str(settings.output_dir), "--tests-output-dir", str(settings.output_root)])
    # This scripted suite deliberately covers one operation only. Execution
    # and repair succeed, while the CLI reports incomplete contract categories.
    with pytest.raises(SystemExit) as incomplete:
        prototype.main()
    assert incomplete.value.code == 1
    assert model.tool_results == []  # No extra model call after the pipeline.
    report = json.loads(next(settings.output_dir.rglob("generation-report.json")).read_text(encoding="utf-8"))
    data = {**report, "pipeline_status": report["status"], "summary": report["pytest"]["summary"]}
    assert data["pipeline_status"] == "incomplete_categories", data
    assert report['category_evidence']['status'] == 'incomplete'
    assert report['pytest']['exit_code'] == 0
    assert any('Missing required test categories' in error for error in report['generator_errors'])
    assert data["attempts"] == 1 and data["summary"]["passed"] == 1
    assert model.calls == ["agent-tool", "generate", "repair"]
    assert report["pytest"]["summary"] == data["summary"]
    assert report["runnability"] == data["runnability"]
    assert report["provenance"]["llm"]["model"] == model.model_name
    metadata = json.loads(next(settings.output_dir.rglob("run-metadata.json")).read_text(encoding="utf-8"))
    assert metadata["llm"]["model"] == report["provenance"]["llm"]["model"]
    cli_report = next(settings.output_dir.rglob("report_*.md")).read_text(encoding="utf-8")
    assert "Статус: incomplete_categories" in cli_report
    saved = Path(data["saved_versions"][-1]["tests_dir"])
    original = (saved / "test_catalogue.py").read_bytes()
    replay = run_tests(RunConfig(saved, tmp_path / "replay", base_url=settings.base_url),
                       network=settings.network, blocked_networks=settings.blocked_networks)
    assert replay.status == "completed" and replay.exit_code == 0, replay.to_dict()
    assert replay.to_dict()["summary"]["passed"] == 1
    assert (saved / "test_catalogue.py").read_bytes() == original
    assert model.calls == ["agent-tool", "generate", "repair"]
    command = [sys.executable, "-m", "prototype.service_tools.runner", str(saved),
               "--output-dir", str(tmp_path / "cli-replay"), "--base-url", settings.base_url,
               "--network", settings.network, "--blocked-network", settings.blocked_networks[0]]
    cli = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert json.loads(cli.stdout)["summary"] == replay.to_dict()["summary"]
    assert (saved / "test_catalogue.py").read_bytes() == original
    assert model.calls == ["agent-tool", "generate", "repair"]
