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


def test_generation_output_requires_non_empty_files():
    from pydantic import ValidationError

    from prototype.generator.prompts import GenerationOutput

    with pytest.raises(ValidationError):
        GenerationOutput.model_validate_json('{"files": []}')
