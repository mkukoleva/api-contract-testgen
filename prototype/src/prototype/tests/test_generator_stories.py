"""Story validation and real pytest execution with a scripted model/transport.

The offline control executes only the hardcoded code below. It uses the real
trusted client and worker with simulated HTTP responses, not a live API.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from prototype.generator import GenerationSettings, run_generation_pipeline
from prototype.generator.context import load_contract
from prototype.generator.generate import generate_suite
from prototype.generator.stories import load_user_story
from prototype.service_tools.runner.docker_runner import read_result

ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = ROOT / "benchmark/catalogue/catalogue.swagger.json"
STORY_PATH = ROOT / "benchmark/generation/catalogue-user-story.json"
CODE = '''def test_story_catalogue_item(base_url, api_client):
    """List products, open selected item, verify its ID and name."""
    response = api_client.get(base_url + '/catalogue')
    assert response.status_code == 200
    products = response.json()
    assert isinstance(products, list)
    assert products, 'Story requires at least one product'
    selected = products[0]
    assert isinstance(selected, dict)
    assert 'id' in selected and 'name' in selected
    assert isinstance(selected['id'], str)
    item_response = api_client.get(base_url + '/catalogue/' + selected['id'])
    assert item_response.status_code == 200
    item = item_response.json()
    assert isinstance(item, dict)
    for product in (selected, item):
        for field in ('id', 'name', 'description', 'imageUrl', 'price', 'count', 'tag'):
            assert field in product
        for field in ('id', 'name', 'description'):
            assert isinstance(product[field], str)
        assert type(product['price']) in (int, float)
        assert type(product['count']) is int
        for field in ('imageUrl', 'tag'):
            assert isinstance(product[field], list)
            assert all(isinstance(value, str) for value in product[field])
    assert item['id'] == selected['id'] and item['name'] == selected['name']
'''


class Model:
    def __init__(self, code=CODE):
        self.code, self.calls = code, []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps({"files": [{"name": "test_story.py", "code": self.code}]}),
                               usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})


@pytest.fixture
def story_data():
    return json.loads(STORY_PATH.read_text(encoding="utf-8"))


def test_story_preserves_order_response_bindings_and_source_fingerprint():
    story = load_user_story(STORY_PATH, load_contract(CONTRACT_PATH))
    assert story["steps"][1]["bindings"] == [{"from_step": 0, "pointer": "/0/id", "target": "path:id"}]
    assert story["steps"][0]["endpoint"] == "GET /catalogue"
    assert len(story["sha256"]) == 64 and story["final_goal"]


@pytest.mark.parametrize("location", ["query", "header"])
@pytest.mark.parametrize("shared", [False, True])
def test_story_bindings_resolve_local_parameter_references(tmp_path, location, shared):
    contract = {"openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
                "paths": {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}},
                "components": {"parameters": {"Limit": {
                    "in": location, "name": "limit", "schema": {"type": "integer"}}}}}
    owner = contract["paths"]["/items"]
    if not shared:
        owner = owner["get"]
    owner["parameters"] = [{"$ref": "#/components/parameters/Limit"}]
    original = json.dumps(contract, sort_keys=True)
    binding = {"from_step": 0, "pointer": "/limit", "target": f"{location}:limit"}
    data = {"story": "Reuse the returned limit", "final_goal": "The second request uses the returned limit",
            "steps": [{"endpoint": "GET /items", "expected_result": "A limit is returned"},
                      {"endpoint": "GET /items", "expected_result": "The limit is applied",
                       "bindings": [binding]}]}
    path = tmp_path / "story.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    story = load_user_story(path, contract)
    assert story["steps"][1]["bindings"] == [binding]
    assert json.dumps(contract, sort_keys=True) == original


@pytest.mark.parametrize("change", [
    lambda data: data.update(final_goal=""),
    lambda data: data.update(steps=data["steps"][:1]),
    lambda data: data["steps"][1].update(endpoint="GET /undocumented"),
    lambda data: data["steps"][1].update(endpoint="GET http://other/items"),
    lambda data: data["steps"][1].update(expected_status=404),
    lambda data: data["steps"][1].update(expected_status=True),
    lambda data: data["steps"][1]["bindings"][0].update(from_step=1),
    lambda data: data["steps"][1]["bindings"][0].update(pointer="not-a-pointer"),
    lambda data: data["steps"][1]["bindings"][0].update(target="path:missing"),
    lambda data: data["steps"][1]["bindings"][0].update(target="query:missing"),
    lambda data: data["steps"][1]["bindings"][0].update(target="body:/id"),
    lambda data: data["steps"][1].update(expected_result=""),
])
def test_invalid_story_stops_before_provider_initialization(tmp_path, monkeypatch, story_data, change):
    import prototype.llm.model as provider
    change(story_data)
    path = tmp_path / "story.json"
    path.write_text(json.dumps(story_data), encoding="utf-8")
    monkeypatch.setattr(provider, "build_model", lambda: pytest.fail("Must not initialize LLM"))
    result = generate_suite(load_contract(CONTRACT_PATH), GenerationSettings(CONTRACT_PATH, user_story_path=path))
    assert result["calls"] == 0 and not result["prepared"] and result["errors"]


@pytest.mark.parametrize("raw", [b"not JSON", b'"story"', b" " * (64 * 1024 + 1)])
def test_invalid_story_file_is_reported(tmp_path, raw):
    path = tmp_path / "story.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_user_story(path, load_contract(CONTRACT_PATH))


def test_legacy_story_tool_payload_is_accepted_without_machine_bindings(story_data, tmp_path):
    for step in story_data["steps"]:
        step.pop("expected_status", None)
        step.pop("bindings", None)
    story_data.update(tool="generate_user_story_tool", status="success")
    path = tmp_path / "story.json"
    path.write_text(json.dumps(story_data), encoding="utf-8")
    assert len(load_user_story(path, load_contract(CONTRACT_PATH))["steps"]) == 2


@pytest.mark.parametrize("code", [
    CODE.replace("test_story_catalogue_item", "test_normal_item"),
    CODE.replace("item_response = api_client.get", "item_response = api_client.post"),
    'def test_story_one(base_url, api_client):\n    assert api_client.get(base_url).status_code == 200\n',
])
def test_model_cannot_replace_chain_with_unrelated_or_incomplete_tests(code):
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert not result["prepared"] and "test_story_" in result["errors"][-1]


@pytest.mark.parametrize("wrong_name", [False, True])
def test_story_pipeline_runs_real_pytest_and_detects_failed_final_goal(tmp_path, wrong_name):
    def runner(config):
        # Trusted test transport: prepared HTTP requests must use the ID from
        # the earlier response. No service, Docker or model access occurs.
        (config.tests_dir / "conftest.py").write_text('''import json
import pytest
import requests
@pytest.fixture(autouse=True)
def scripted_transport(monkeypatch):
    def send(self, request, **kwargs):
        assert request.method == 'GET'
        product = {'id': 'runtime-id-9281', 'name': 'chair', 'description': 'demo',
                   'imageUrl': [], 'price': 10.0, 'count': 1, 'tag': []}
        if request.path_url == '/catalogue':
            body = [product]
        elif request.path_url == '/catalogue/runtime-id-9281':
            body = {**product, 'name': NAME}
        else:
            raise AssertionError('ID must come from previous response: ' + request.path_url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        response.request = request
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
'''.replace("NAME", repr("wrong" if wrong_name else "chair")), encoding="utf-8")
        policy = config.output_dir / "control-policy"
        policy.mkdir(parents=True)
        (policy / "test_control.py").write_text(
            "def test_trusted_client(api_client): assert api_client.trust_env is False\n", encoding="utf-8")
        events = config.output_dir / "events.jsonl"
        command = [sys.executable, "-I", "-B",
            str(ROOT / "prototype/src/prototype/service_tools/runner/pytest_worker.py"),
            str(policy), str(config.tests_dir), str(config.output_dir / "policy.jsonl"),
            str(events), str(config.output_dir / "collect.jsonl")]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=20,
            env={**os.environ, "RUNNER_BASE_URL": "http://control.invalid", "PYTHONDONTWRITEBYTECODE": "1"})
        assert events.is_file(), proc.stdout + proc.stderr
        return read_result(events, 0.1)
    model = Model()
    run = run_generation_pipeline(GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH,
        base_url="http://control.invalid", output_dir=tmp_path / "reports", output_root=tmp_path / "saved"),
        model=model, run_tests_fn=runner)
    assert len(model.calls) == 1 and run.attempts == 0
    assert '"from_step":0' in model.calls[0][1]["content"]
    assert "final_goal" in model.calls[0][1]["content"]
    assert run.status == ("completed_with_failures" if wrong_name else "incomplete_categories"), run.to_dict()
    assert run.run_result["summary"]["failed" if wrong_name else "passed"] == 1
    assert run.runnability["runnability_percent"] == 100
    saved = Path(run.saved_versions[0]["tests_dir"]) / "test_story.py"
    assert saved.read_text(encoding="utf-8") == CODE
    report = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
    assert report["provenance"]["user_story"]["final_goal"]
    markdown = Path(run.report_paths["markdown"]).read_text(encoding="utf-8")
    assert "Цепочечный сценарий" in markdown and "Конечная цель:" in markdown
    assert list((tmp_path / "reports").rglob("inputs/user-story.json"))


def test_invalid_pipeline_story_has_report_and_never_initializes_model(tmp_path, monkeypatch):
    import prototype.llm.model as provider
    monkeypatch.setattr(provider, "build_model", lambda: pytest.fail("Invalid story must not initialize LLM"))
    missing = tmp_path / "missing-story.json"
    run = run_generation_pipeline(GenerationSettings(CONTRACT_PATH, user_story_path=missing,
        output_dir=tmp_path / "reports"))
    assert run.status == "generation_error" and run.generator_errors
    assert Path(run.report_paths["json"]).is_file()


def test_story_snapshot_survives_provider_failure(tmp_path):
    class BrokenModel:
        def invoke(self, messages):
            raise RuntimeError("offline provider unavailable")
    run = run_generation_pipeline(GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH,
        output_dir=tmp_path / "reports"), model=BrokenModel())
    assert run.status == "generation_error"
    snapshots = list((tmp_path / "reports").rglob("inputs/user-story.json"))
    assert len(snapshots) == 1
    assert json.loads(snapshots[0].read_text())["sha256"] == run.provenance["user_story"]["sha256"]


def test_pipeline_uses_validated_story_even_if_original_changes(tmp_path, monkeypatch):
    import prototype.generator.pipeline as pipeline
    from prototype.tests.test_generator_pipeline import FakeSaver
    from prototype.service_tools.runner.contracts import RunResult
    story = tmp_path / "story.json"
    story.write_bytes(STORY_PATH.read_bytes())
    expected = load_user_story(story, load_contract(CONTRACT_PATH))
    real_generate = pipeline.generate_suite

    def mutate_original(*args, **kwargs):
        story.write_text("invalid replacement")
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(pipeline, "generate_suite", mutate_original)
    model = Model()
    run = pipeline.run_generation_pipeline(GenerationSettings(CONTRACT_PATH, user_story_path=story,
        output_dir=tmp_path / "reports"), model=model,
        run_tests_fn=lambda config: RunResult("no_tests", 5, 0.1), save_fn=FakeSaver())
    assert model.calls and not run.generator_errors
    assert run.provenance["user_story"] == expected
    snapshot = next((tmp_path / "reports").rglob("inputs/user-story.json"))
    assert json.loads(snapshot.read_text()) == expected


def test_primary_agent_validates_story_before_building_model(tmp_path, monkeypatch):
    import prototype
    from prototype.parser.contract import ContractInputError
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: pytest.fail("Must not build agent"))
    with pytest.raises(ContractInputError, match="user story"):
        prototype.run(str(CONTRACT_PATH), report_dir=tmp_path,
            generation_settings=GenerationSettings(CONTRACT_PATH, user_story_path=tmp_path / "missing.json"))


@pytest.mark.parametrize("code", [
    "def test_story_fake(base_url, api_client):\n    if False:\n        api_client.get(base_url + '/catalogue')\n        api_client.get(base_url + '/catalogue/' + 'id')\n    assert True\n    assert True\n    assert True\n",
    CODE.replace("'/catalogue/' + selected['id']", "'/wrong/' + selected['id']"),
    "def test_story_fake(base_url, api_client):\n    def helper():\n        api_client.get(base_url + '/catalogue')\n        api_client.get(base_url + '/catalogue/' + 'id')\n    assert base_url\n    assert base_url\n    assert base_url\n",
    "def test_story_fake(base_url, api_client):\n    return\n    api_client.get(base_url + '/catalogue')\n    api_client.get(base_url + '/catalogue/' + 'id')\n    assert base_url\n    assert base_url\n    assert base_url\n",
])
def test_story_gate_rejects_dead_helper_or_wrong_route_calls(code):
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert not result["prepared"] and result["errors"]


def test_story_gate_accepts_fstrings_and_explicit_request_method():
    code = CODE.replace("base_url + '/catalogue'", "f'{base_url}/catalogue'").replace(
        "api_client.get(base_url + '/catalogue/' + selected['id'])",
        "api_client.request(method='GET', url=f\"{base_url}/catalogue/{selected['id']}\")")
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert result["prepared"] and not result["errors"]


def test_story_gate_accepts_trusted_base_url_with_trailing_slash_removed():
    code = CODE.replace("base_url +", "base +").replace(
        "    response =", "    base = base_url.rstrip('/')\n    response =", 1)
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert result["prepared"] and not result["errors"]


@pytest.mark.parametrize("base", ["'http://other-service'.rstrip('/')",
                                 "base_url.rstrip('/catalogue')"])
def test_story_gate_does_not_treat_other_url_transformations_as_trusted_base(base):
    code = CODE.replace("base_url +", "base +").replace(
        "    response =", f"    base = {base}\n    response =", 1)
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert not result["prepared"] and result["errors"]


@pytest.mark.parametrize('code', [
    "def test_story_wrong(base_url, api_client):\n"
    "    first = api_client.get(base_url + '/catalogue')\n"
    "    last = api_client.get(base_url + '/catalogue/hardcoded-wrong-id')\n"
    "    assert base_url\n    assert base_url\n    assert base_url\n",
    CODE.replace("selected['id'])", "'hardcoded-id')"),
    CODE.replace("selected['id'])", "selected['name'])"),
    CODE.replace("assert item_response.status_code == 200", "assert base_url"),
    CODE.replace("assert response.status_code == 200", "assert response.status_code == 201"),
    CODE.replace("assert response.status_code == 200", "assert response.status_code == 200 or True"),
    CODE.replace("assert item['id'] == selected['id'] and item['name'] == selected['name']", "assert base_url"),
])
def test_story_rejects_unrelated_assertions_wrong_bindings_and_missing_goal(code):
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert not result['prepared'] and result['errors']


def test_story_accepts_direct_json_get_binding():
    code = CODE.replace("selected = products[0]", "selected = response.json()[0]").replace(
        "'/catalogue/' + selected['id']", "'/catalogue/' + selected.get('id')")
    result = generate_suite(load_contract(CONTRACT_PATH),
        GenerationSettings(CONTRACT_PATH, user_story_path=STORY_PATH), model=Model(code))
    assert result['prepared'] and not result['errors']


@pytest.mark.parametrize('target,route,arguments', [
    ('path:id', '/items/{id}', "base_url + '/items/' + value"),
    ('query:id', '/items', "base_url + '/items', params={'id': value}"),
    ('header:id', '/items', "base_url + '/items', headers={'id': value}"),
    ('body:/id', '/items', "base_url + '/items', json={'id': value}"),
])
def test_story_dataflow_checks_declared_request_location(target, route, arguments):
    from prototype.generator.stories import check_story_code
    story = {'steps': [{'endpoint': 'GET /items', 'expected_status': 200},
        {'endpoint': 'GET ' + route, 'expected_status': 200, 'bindings': [
            {'from_step': 0, 'pointer': '/id', 'target': target}]}]}
    code = f'''def test_story_values(base_url, api_client):
    first = api_client.get(base_url + '/items')
    assert first.status_code == 200
    body = first.json()
    value = body['id']
    last = api_client.get({arguments})
    assert last.status_code == 200
    final = last.json()
    assert final['id'] == value
'''
    assert check_story_code([SimpleNamespace(code=code)], story) is None
    assert check_story_code([SimpleNamespace(code=code.replace("value = body['id']", "value = 'constant'"))], story)
