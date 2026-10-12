"""TZ v3 M3: scripted generation/repair, actual Docker runner and API.

No provider calls: this verifies execution and repairs, not LLM quality (M2).
Every attempt uses the real immutable storage and isolated runner.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from prototype.generator import GenerationSettings, run_generation_pipeline


CATALOGUE = pytest.mark.skipif(
    os.getenv("RUN_RUNNER_CATALOGUE_TESTS") != "1", reason="requires Catalogue and runner image")


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=next(self.replies), usage_metadata={
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2})


def settings(tmp_path, **kwargs):
    return GenerationSettings(
        contract_path=Path(__file__).resolve().parents[4] / "benchmark/catalogue/catalogue.swagger.json",
        base_url="http://catalogue:8080", network="pytest-runner-catalogue_runner",
        blocked_networks=("pytest-runner-catalogue_database",),
        output_dir=tmp_path / "reports", output_root=tmp_path / "saved", **kwargs)


@CATALOGUE
def test_generated_suite_repairs_syntax_import_and_fixture_inside_isolation(tmp_path):
    broken = (
        "@pytest.mark.parametrize('limit', [1])\n"
        "def test_catalogue(base_url, client, limit)\n"
        "    response = client.get(base_url + '/catalogue')\n"
        "    assert response.status_code == 200\n"
        "    assert isinstance(response.json(), list)\n"
    )
    syntax_fixed = broken.replace("client, limit)\n", "client, limit):\n")
    import_fixed = "import pytest\n" + syntax_fixed
    fixture_fixed = import_fixed.replace("client", "api_client")
    model = ScriptedModel([
        json.dumps({"files": [{"name": "test_catalogue.py", "code": broken}]}),
        syntax_fixed, import_fixed, fixture_fixed])
    run = run_generation_pipeline(settings(tmp_path), model=model)
    assert run.status == "success", run.to_dict()
    assert run.attempts == 3
    assert len(model.calls) == 4
    assert len(run.saved_versions) == 4
    assert all(entry.outcome == "repaired" for entry in run.repair_log)
    assert run.run_result["summary"]["passed"] == 1
    assert len(list((tmp_path / "reports").rglob("policy_events.jsonl"))) == 3
    final_code = (Path(run.saved_versions[-1]["tests_dir"]) / "test_catalogue.py").read_text(encoding="utf-8")
    assert final_code == fixture_fixed
    # The original failed generation and every candidate remain available.
    manifest = json.loads((Path(run.saved_versions[0]["tests_dir"]).parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["files_rejected"] == 1


@CATALOGUE
def test_live_api_assertion_failure_is_not_repaired(tmp_path):
    code = (
        "def test_catalogue(base_url, api_client):\n"
        "    response = api_client.get(base_url + '/catalogue')\n"
        "    assert response.status_code == 404\n"
    )
    model = ScriptedModel([json.dumps({"files": [{"name": "test_api.py", "code": code}]})])
    run = run_generation_pipeline(settings(tmp_path), model=model)
    assert run.status == "completed_with_failures"
    assert run.run_result["summary"]["failed"] == 1
    assert run.attempts == 0 and len(model.calls) == 1
    assert run.suspected_defects
    assert (Path(run.saved_versions[0]["tests_dir"]) / "test_api.py").read_text(encoding="utf-8") == code
