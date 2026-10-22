"""Initial generation call: structured output parsing and file preparation."""

import json
from types import SimpleNamespace

import pytest

from prototype.generator.contracts import GenerationSettings
from prototype.generator.generate import generate_suite, request_usage

CONTRACT = {"info": {"title": "Demo", "version": "1"}, "paths": {}}

GOOD = "def test_x(base_url, api_client):\n    assert 1 == 1\n"


class FakeModel:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.content, usage_metadata=self.usage)


def settings():
    return GenerationSettings(contract_path="demo.yaml")


@pytest.mark.parametrize('declaration', [
    'import pytest\n@pytest.fixture\ndef api_client(): return object()\n',
    'import pytest as pt\n@pt.fixture()\ndef base_url(): return "http://other"\n',
    'from pytest import fixture as fx\n@fx(name="api_client")\ndef replacement(): return object()\n',
    'import pytest\nclass TestApi:\n    @pytest.fixture\n    def base_url(self): return "http://other"\n',
])
def test_generated_modules_cannot_define_custom_fixtures(declaration):
    code = declaration + '\ndef test_http(base_url, api_client):\n    assert True\n'
    model = FakeModel(json.dumps({'files': [{'name': 'test_http.py', 'code': code}]}))
    result = generate_suite(CONTRACT, settings(), model=model)
    assert not result['prepared']
    assert 'custom pytest fixtures' in result['errors'][0]


def test_helpers_and_parametrization_preserve_trusted_fixture_use():
    code = ('import pytest\ndef assert_body(body):\n    assert isinstance(body, list)\n'
            '@pytest.mark.parametrize("path", ["/catalogue", "/tags"])\n'
            'def test_http(base_url, api_client, path):\n'
            '    response = api_client.get(base_url + path)\n'
            '    assert response.status_code == 200\n')
    result = generate_suite(CONTRACT, settings(), model=FakeModel(json.dumps({
        'files': [{'name': 'test_http.py', 'code': code}]})))
    assert not result['errors'] and result['prepared'][0].code == code


@pytest.mark.parametrize("code", [
    GOOD + GOOD,
    "class TestApi:\n    def test_x(self): assert True\n    def test_x(self): assert False\n",
    "class TestApi:\n    def test_x(self): assert True\nclass TestApi:\n    def test_y(self): assert True\n",
])
def test_duplicate_definitions_cannot_silently_remove_tests(code):
    result = generate_suite(CONTRACT, settings(), model=FakeModel(json.dumps({
        "files": [{"name": "test_api.py", "code": code}]})))
    assert not result["prepared"]
    assert "duplicate test definitions" in result["errors"][0]


def test_same_test_name_in_different_classes_is_valid():
    code = "class TestA:\n    def test_x(self): assert True\nclass TestB:\n    def test_x(self): assert True\n"
    result = generate_suite(CONTRACT, settings(), model=FakeModel(json.dumps({
        "files": [{"name": "test_api.py", "code": code}]})))
    assert not result["errors"] and result["prepared"]


@pytest.mark.parametrize("code", [
    'def test_boundary_empty(base_url, api_client):\n    "Not applicable"\n    pass\n',
    'def test_placeholder():\n    ...\n',
    'class TestApi:\n    def test_placeholder(self):\n        "Only explanation"\n',
])
def test_empty_placeholders_cannot_masquerade_as_generated_scenarios(code):
    result = generate_suite(CONTRACT, settings(), model=FakeModel(json.dumps({
        "files": [{"name": "test_api.py", "code": code}]})))
    assert not result["prepared"]
    assert "empty test placeholders" in result["errors"][0]


def test_request_usage_reads_usage_metadata():
    assert request_usage(SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 2})) == (1, 2, 3)
    assert request_usage(SimpleNamespace(usage_metadata=None)) == (0, 0, 0)
    assert request_usage(SimpleNamespace(usage_metadata={"total_tokens": 9})) == (0, 0, 9)


def test_generate_suite_returns_prepared_files_and_usage():
    model = FakeModel(json.dumps({"files": [{"name": "test_a.py", "code": GOOD}]}))
    result = generate_suite(CONTRACT, settings(), model=model)
    assert result["prepared"]
    assert result["prepared"][0].name == "test_a.py"
    assert result["prepared"][0].status == "ok"
    assert result["errors"] == ()
    assert result["tokens"] == (10, 20, 30)
    assert result["calls"] == 1


def test_generate_suite_tolerates_markdown_fences():
    content = "```json\n" + json.dumps({"files": [{"name": "test_a.py", "code": GOOD}]}) + "\n```"
    result = generate_suite(CONTRACT, settings(), model=FakeModel(content))
    assert len(result["prepared"]) == 1


def test_generate_suite_tolerates_prose_around_fenced_json():
    # A real model may explain first; the parser must not mistake {base_url}
    # prose for the JSON object.
    payload = {"files": [{"name": "test_a.py", "code": GOOD}]}
    content = (
        "Вот код: используйте f\"{base_url}/catalogue\" в тестах.\n\n"
        "```json\n" + json.dumps(payload) + "\n```\nУспехов!"
    )
    result = generate_suite(CONTRACT, settings(), model=FakeModel(content))
    assert len(result["prepared"]) == 1
    assert result["errors"] == ()


def test_parse_model_content_skips_brace_prose_and_finds_json():
    from prototype.generator.generate import parse_model_content
    from prototype.generator.prompts import GenerationOutput

    payload = {"files": [{"name": "test_a.py", "code": GOOD}]}
    obj = parse_model_content(
        "f\"{base_url}/x\" а потом " + json.dumps(payload, ensure_ascii=False),
        GenerationOutput,
    )
    assert [file.name for file in obj.files] == ["test_a.py"]

    import pytest

    with pytest.raises(ValueError, match="no JSON object"):
        parse_model_content('просто f"{base_url}/x" и "abc"', GenerationOutput)


def test_generate_suite_drops_unsafe_names_and_reports_them():
    payload = {"files": [
        {"name": "test_a.py", "code": GOOD},
        {"name": "conftest.py", "code": "import os"},
        {"name": "../escape.py", "code": "x = 1"},
        {"name": "not_a_test.py", "code": "y = 2"},
    ]}
    result = generate_suite(CONTRACT, settings(), model=FakeModel(json.dumps(payload)))
    assert [item.name for item in result["prepared"]] == ["test_a.py"]
    assert len(result["errors"]) == 3
    assert any("conftest.py" in error for error in result["errors"])


def test_generate_suite_invalid_output_is_reported_not_raised():
    result = generate_suite(CONTRACT, settings(), model=FakeModel("def not json at all"))
    assert result["prepared"] == ()
    assert result["calls"] == 1
    assert any("not valid" in error for error in result["errors"])


def test_generate_suite_model_crash_is_reported_not_raised():
    class Boom:
        def invoke(self, messages):
            raise RuntimeError("connection reset")

    result = generate_suite(CONTRACT, settings(), model=Boom())
    assert result["prepared"] == ()
    assert result["calls"] == 0
    assert any("LLM generation failed" in error for error in result["errors"])


def test_generate_suite_sends_system_and_user_messages():
    model = FakeModel(json.dumps({"files": [{"name": "test_a.py", "code": GOOD}]}))
    generate_suite(CONTRACT, settings(), model=model)
    assert [msg["role"] for msg in model.messages] == ["system", "user"]
    assert "Операции:" in model.messages[1]["content"]

def test_generate_suite_passes_response_contract_to_model():
    contract = {'paths': {'/items': {'get': {'responses': {'200': {
        'schema': {'$ref': '#/definitions/Item'}}}}}}, 'definitions': {
        'Item': {'type': 'object', 'required': ['price'], 'properties': {
            'price': {'type': 'number'}, 'labels': {'type': 'array', 'items': {'type': 'string'}}}}}}
    model = FakeModel(json.dumps({'files': [{'name': 'test_a.py', 'code': GOOD}]}))
    result = generate_suite(contract, settings(), model=model)
    assert result['errors'] == ()
    context = model.messages[1]['content']
    assert 'price*:number' in context
    assert 'required=["price"]' in context
    assert 'labels:array[string]' in context

def test_invalid_generation_context_does_not_call_model():
    model = FakeModel('unused')
    contract = {'paths': {'/items': {'get': {'responses': {'200': {
        'schema': {'$ref': '#/definitions/Missing'}}}}}}}
    result = generate_suite(contract, settings(), model=model)
    assert result['prepared'] == ()
    assert result['calls'] == 0
    assert model.messages is None
    assert '$ref' in result['errors'][0]


def test_generation_output_requires_non_empty_files():
    from pydantic import ValidationError

    from prototype.generator.prompts import GenerationOutput

    with pytest.raises(ValidationError):
        GenerationOutput.model_validate_json('{"files": []}')


def test_invalid_context_is_rejected_before_provider_initialization(monkeypatch):
    import prototype.llm.model as provider
    monkeypatch.setattr(provider, "build_model", lambda: pytest.fail("Invalid context must not initialize LLM"))
    contract = {"paths": {"/items": {"get": {"responses": {"200": {
        "schema": {"$ref": "#/definitions/Missing"}}}}}}}
    result = generate_suite(contract, settings())
    assert result["calls"] == 0 and not result["prepared"]
    assert "$ref" in result["errors"][0]
