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
    prototype.main()
    assert len(model.tool_results) == 1
    data = model.tool_results[0]
    assert data["pipeline_status"] == "success", data
    assert data["attempts"] == 1 and data["summary"]["passed"] == 1
    assert model.calls == ["agent-tool", "generate", "repair", "agent-final"]
    report = json.loads(Path(data["report_paths"]["json"]).read_text(encoding="utf-8"))
    assert report["pytest"]["summary"] == data["summary"]
    assert report["runnability"] == data["runnability"]
    assert report["provenance"]["llm"]["model"] == model.model_name
    metadata = json.loads(next(settings.output_dir.rglob("run-metadata.json")).read_text(encoding="utf-8"))
    assert metadata["llm"]["model"] == report["provenance"]["llm"]["model"]
    cli_report = next(settings.output_dir.rglob("report_*.md")).read_text(encoding="utf-8")
    assert "Статус: success" in cli_report
    saved = Path(data["saved_versions"][-1]["tests_dir"])
    original = (saved / "test_catalogue.py").read_bytes()
    replay = run_tests(RunConfig(saved, tmp_path / "replay", base_url=settings.base_url),
                       network=settings.network, blocked_networks=settings.blocked_networks)
    assert replay.status == "completed" and replay.exit_code == 0, replay.to_dict()
    assert replay.to_dict()["summary"]["passed"] == 1
    assert (saved / "test_catalogue.py").read_bytes() == original
    assert model.calls == ["agent-tool", "generate", "repair", "agent-final"]
    command = [sys.executable, "-m", "prototype.service_tools.runner", str(saved),
               "--output-dir", str(tmp_path / "cli-replay"), "--base-url", settings.base_url,
               "--network", settings.network, "--blocked-network", settings.blocked_networks[0]]
    cli = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert json.loads(cli.stdout)["summary"] == replay.to_dict()["summary"]
    assert (saved / "test_catalogue.py").read_bytes() == original
    assert model.calls == ["agent-tool", "generate", "repair", "agent-final"]
