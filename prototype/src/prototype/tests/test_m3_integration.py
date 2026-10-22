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
def test_generated_story_uses_real_response_id_and_verifies_final_goal(tmp_path):
    from prototype.tests.test_generator_stories import CODE, STORY_PATH
    model = ScriptedModel([json.dumps({"files": [{"name": "test_story.py", "code": CODE}]})])
    run = run_generation_pipeline(settings(tmp_path, user_story_path=STORY_PATH), model=model)
    assert run.status == "incomplete_categories", run.to_dict()
    assert run.category_evidence['status'] == 'incomplete'
    assert any('Missing required test categories' in error for error in run.generator_errors)
    assert run.run_result['exit_code'] == 0
    assert run.run_result["summary"]["passed"] == 1
    assert run.attempts == 0 and len(model.calls) == 1
    assert run.provenance["user_story"]["steps"][1]["bindings"][0]["pointer"] == "/0/id"
    saved = Path(run.saved_versions[0]["tests_dir"]) / "test_story.py"
    assert saved.read_text(encoding="utf-8") == CODE


@CATALOGUE
def test_generated_http_suite_uses_trusted_fixtures_and_is_saved_unchanged(tmp_path):
    code = (
        "def test_normal_catalogue(base_url, api_client):\n"
        "    response = api_client.get(base_url + '/catalogue')\n"
        "    assert response.status_code == 200\n"
        "    products = response.json()\n"
        "    assert isinstance(products, list)\n"
        "    for product in products:\n"
        "        assert isinstance(product, dict)\n"
        "        for field in ('id', 'name', 'description', 'imageUrl', 'price', 'count', 'tag'):\n"
        "            assert field in product\n"
        "        assert type(product['price']) in (int, float)\n"
        "        assert type(product['count']) is int\n"
        "\ndef test_normal_size(base_url, api_client):\n"
        "    response = api_client.get(base_url + '/catalogue/size')\n"
        "    assert response.status_code == 200\n"
        "    assert isinstance(response.json(), dict)\n"
        "    assert type(response.json()['size']) is int\n"
    )
    model = ScriptedModel([json.dumps({'files': [{'name': 'test_http.py', 'code': code}]})])
    run = run_generation_pipeline(settings(tmp_path), model=model)
    assert run.status == 'incomplete_categories', run.to_dict()
    missing = {row['operation'] for row in run.category_evidence['operations']
               if row['categories']['normal']['status'] == 'missing'}
    assert missing == {'GET /catalogue/{id}', 'GET /tags'}
    assert run.run_result['exit_code'] == 0
    assert run.attempts == 0 and len(model.calls) == 1
    assert run.run_result['summary']['passed'] == 2
    assert run.runnability['runnability_percent'] == 100
    assert len(run.saved_versions) == 1
    saved = Path(run.saved_versions[0]['tests_dir']) / 'test_http.py'
    assert saved.read_text(encoding='utf-8') == code
    assert Path(run.report_paths['json']).is_file()


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
    assert run.status == "incomplete_categories", run.to_dict()
    assert run.category_evidence['status'] == 'incomplete'
    assert any('Missing required test categories' in error for error in run.generator_errors)
    assert run.run_result['exit_code'] == 0
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
