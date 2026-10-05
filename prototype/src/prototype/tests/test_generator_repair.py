"""Self-repair classification, fragments and repair calls; no LLM or Docker."""

from types import SimpleNamespace

from prototype.generator.repair import (
    RepairTarget,
    apply_repair,
    classify_result,
    error_signature,
    extract_function_source,
    extract_request_site,
    is_environmental_failure,
    parse_actual_status,
    repair_call,
    replace_function,
    split_nodeid,
)
from prototype.generator.contracts import GenerationSettings
from prototype.generator.context import load_contract
from prototype.service_tools.runner.contracts import (
    RunResult,
    RunStatus,
    TestResult as RunnerTestResult,
)

CONTRACT = {
    "openapi": "3.0.3",
    "info": {"title": "Demo Catalogue API", "version": "1.0.0"},
    "paths": {
        "/catalogue": {
            "get": {
                "responses": {"200": {"description": "OK"}, "500": {"description": "Err"}},
            }
        },
        "/catalogue/{id}": {
            "get": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {"description": "Товар"},
                    "404": {"description": "Нет товара"},
                },
            }
        },
    },
}

CODE = (
    "import requests\n\n"
    "def test_list(base_url, api_client):\n"
    "    response = api_client.get(f\"{base_url}/catalogue\")\n"
    "    assert response.status_code == 200\n"
    "    assert isinstance(response.json(), list)\n\n\n"
    "@pytest.mark.parametrize('item_id', ['1', '999999'])\n"
    "def test_item_by_id(base_url, api_client, item_id):\n"
    "    response = api_client.get(f\"{base_url}/catalogue/{item_id}\")\n"
    "    assert response.status_code == 200\n"
)


def settings(**overrides):
    base = {"contract_path": "demo.yaml", "diagnostic_chars": 2000}
    base.update(overrides)
    return GenerationSettings(**base)


def test_error_signature_normalizes_paths_and_lines():
    a = "test_a.py:12: in test_x\nE   assert 500 == 200"
    b = "test_a.py:34: in test_x\nE   assert 500 == 200"
    c = "test_b.py:1: in test_y\nE   assert 404 == 200"
    assert error_signature(a) == error_signature(b)
    assert error_signature(a) != error_signature(c)
    assert error_signature("") == ""


def test_split_nodeid_handles_classes_and_parametrization():
    assert split_nodeid("test_a.py::test_p[1]") == ("test_a.py", ("test_p",))
    assert split_nodeid("test_a.py::TestTags::test_tags") == (
        "test_a.py", ("TestTags", "test_tags"),
    )
    assert split_nodeid("test_a.py") == ("test_a.py", ())


def test_extract_function_source_keeps_decorators():
    source = extract_function_source(CODE, ("test_item_by_id",))
    assert source is not None
    assert source.startswith("@pytest.mark.parametrize")
    assert "def test_item_by_id" in source
    assert extract_function_source(CODE, ("missing",)) is None


def test_replace_function_replaces_only_the_function():
    fixed = (
        "def test_item_by_id(base_url, api_client, item_id):\n"
        "    response = api_client.get(f\"{base_url}/catalogue/{item_id}\")\n"
        "    assert response.status_code in (200, 404)\n"
    )
    new_code = replace_function(CODE, ("test_item_by_id",), fixed)
    assert "def test_list" in new_code
    assert "in (200, 404)" in new_code
    assert new_code.count("def test_item_by_id") == 1
    assert new_code.count("def test_list") == 1


def test_replace_function_rejects_broken_output():
    import pytest

    with pytest.raises(ValueError):
        replace_function(CODE, ("test_item_by_id",), "def test_item_by_id(:\n")
    with pytest.raises(ValueError):
        replace_function(CODE, ("test_item_by_id",), "def test_other():\n    pass\n")


def test_extract_request_site_reads_method_and_path():
    assert extract_request_site(CODE) == ("GET", "/catalogue")
    assert extract_request_site("x = 1\n") is None


def test_parse_actual_status():
    assert parse_actual_status("assert 500 == 200\nE   assert 500 == 200") == 500
    assert parse_actual_status("assert response.status_code == 200") is None
    assert parse_actual_status("") is None


def test_is_environmental_failure_only_for_api_host():
    message = "requests.exceptions.ConnectionError: Max retries exceeded with url: http://catalogue:8080/catalogue"
    assert is_environmental_failure(message, "http://catalogue:8080")
    assert not is_environmental_failure(message, None)
    assert not is_environmental_failure(
        "requests.exceptions.ConnectTimeout with url: http://other:8080/x",
        "http://catalogue:8080",
    )
    assert not is_environmental_failure("assert 500 == 200", "http://catalogue:8080")


def _completed_with(tests):
    return RunResult(RunStatus.COMPLETED, 1, 0.5, tests=tuple(tests))


def test_classify_ignores_passed_and_skipped():
    plan = classify_result(
        CONTRACT,
        {"test_catalogue.py": CODE},
        _completed_with((
            RunnerTestResult("test_catalogue.py::test_list", "passed"),
            RunnerTestResult("test_catalogue.py::test_item_by_id", "skipped"),
        )),
        settings(),
    )
    assert plan.targets == ()
    assert plan.suspected_defects == ()
    assert plan.environmental == ()


def test_classify_marks_undocumented_status_as_suspected_defect():
    message = "assert 500 == 200\nE   assert response.status_code == 200\nE   assert 500 == 200"
    plan = classify_result(
        CONTRACT,
        {"test_catalogue.py": CODE},
        _completed_with((RunnerTestResult("test_catalogue.py::test_item_by_id", "failed", message=message),)),
        settings(),
    )
    assert plan.targets == ()
    assert len(plan.suspected_defects) == 1
    assert plan.suspected_defects[0]["nodeid"] == "test_catalogue.py::test_item_by_id"
    assert "not documented" in plan.suspected_defects[0]["reason"]


def test_classify_repairs_documented_alternative_status():
    # 404 IS documented for /catalogue/{id}: the test picked bad input.
    message = "assert 404 == 200\nE   assert response.status_code == 200\nE   assert 404 == 200"
    plan = classify_result(
        CONTRACT,
        {"test_catalogue.py": CODE},
        _completed_with((RunnerTestResult("test_catalogue.py::test_item_by_id", "failed", message=message),)),
        settings(),
    )
    assert len(plan.targets) == 1
    target = plan.targets[0]
    assert target.kind == "function"
    assert target.file == "test_catalogue.py"
    assert target.function_path == ("test_item_by_id",)
    assert plan.suspected_defects == ()


def test_classify_without_request_site_is_conservative():
    code_without_http = "def test_x():\n    assert 1 == 1\n    assert 2 == 1\n"
    plan = classify_result(
        CONTRACT,
        {"test_x.py": code_without_http},
        _completed_with((RunnerTestResult("test_x.py::test_x", "failed", message="assert 2 == 1"),)),
        settings(),
    )
    assert plan.targets == ()
    assert len(plan.suspected_defects) == 1


def test_classify_error_outcome_is_repairable_unless_environmental():
    file_code = "def test_x():\n    raise KeyError('missing')\n"
    plan = classify_result(
        CONTRACT,
        {"test_x.py": file_code},
        _completed_with((RunnerTestResult("test_x.py::test_x", "error", message="KeyError: 'missing'"),)),
        settings(),
    )
    assert len(plan.targets) == 1 and plan.targets[0].kind == "function"

    env_plan = classify_result(
        CONTRACT,
        {"test_x.py": file_code},
        _completed_with((
            RunnerTestResult("test_x.py::test_x", "error",
                       message="ConnectionError: Max retries exceeded with url: http://catalogue:8080/x"),
        )),
        settings(base_url="http://catalogue:8080"),
    )
    assert env_plan.targets == ()
    assert len(env_plan.environmental) == 1


def test_classify_collection_error_repairs_files():
    result = RunResult(
        RunStatus.COLLECTION_ERROR, 2, 1.0,
        collection_errors=("test_bad.py: line 1: invalid syntax",),
    )
    plan = classify_result(CONTRACT, {}, result, settings())
    assert len(plan.targets) == 1
    assert plan.targets[0].kind == "file"
    assert plan.targets[0].file == "test_bad.py"


def test_classify_non_completed_statuses_do_not_repair():
    for status in (RunStatus.INFRASTRUCTURE_ERROR, RunStatus.TIMEOUT,
                   RunStatus.NO_TESTS, RunStatus.INTERRUPTED):
        result = RunResult(status, None, 0.5)
        plan = classify_result(CONTRACT, {"test_a.py": CODE}, result, settings())
        assert plan.targets == ()
        assert plan.suspected_defects == ()


def test_apply_repair_function_and_file():
    updated = apply_repair(
        {"test_catalogue.py": CODE},
        RepairTarget(kind="function", file="test_catalogue.py",
                     function_path=("test_item_by_id",)),
        "def test_item_by_id(base_url, api_client, item_id):\n    assert True\n",
    )
    assert "def test_list" in updated["test_catalogue.py"]
    assert "assert True" in updated["test_catalogue.py"]

    updated_file = apply_repair(
        {"test_a.py": "old"},
        RepairTarget(kind="file", file="test_a.py"),
        "new code",
    )
    assert updated_file["test_a.py"] == "new code\n"
    assert "test_a.py" in updated_file


def test_repair_call_builds_compact_payload_and_reads_usage():
    class FakeModel:
        def __init__(self):
            self.calls = []

        def invoke(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(
                content='{"code": "def test_item_by_id(base_url, api_client, item_id):\\n    assert True\\n"}',
                usage_metadata={"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
            )

    fake = FakeModel()
    target = RepairTarget(kind="function", file="test_catalogue.py",
                          function_path=("test_item_by_id",),
                          message="assert 404 == 200\nE   assert 404 == 200", reason="assert_failed")
    result = repair_call(
        fake, settings(test_fragment_lines=30), CONTRACT, target, CODE
    )
    assert result.ok
    assert result.code is not None and "def test_item_by_id" in result.code
    assert result.tokens == (100, 40, 140)
    assert result.payload_chars > 0
    # Payload reaches the model as system + user messages.
    assert len(fake.calls) == 1
    user = next(msg["content"] for msg in fake.calls[0] if msg["role"] == "user")
    assert "/catalogue/{id}" in user
    # The whole file is not dumped: only the failing function fragment.
    assert "def test_list" not in user
    assert "def test_item_by_id" in user


def test_repair_call_reports_invalid_output_without_raising():
    class BrokenModel:
        def invoke(self, messages):
            return SimpleNamespace(content="not json", usage_metadata={})

    target = RepairTarget(kind="function", file="test_catalogue.py",
                          function_path=("test_item_by_id",), message="x", reason="assert_failed")
    result = repair_call(BrokenModel(), settings(), CONTRACT, target, CODE)
    assert not result.ok
    assert "not valid" in result.error


def test_repair_call_accepts_fenced_bare_code():
    """The repair prompt asks for code, so a fenced ```python reply is valid."""

    class FencedModel:
        def invoke(self, messages):
            return SimpleNamespace(
                content=(
                    "```python\n"
                    "def test_item_by_id(base_url, api_client, item_id):\n"
                    '    response = api_client.get(f"{base_url}/catalogue/{item_id}")\n'
                    "    assert response.status_code == 200\n"
                    "```"
                ),
                usage_metadata={"input_tokens": 10, "output_tokens": 9, "total_tokens": 19},
            )

    target = RepairTarget(kind="function", file="test_catalogue.py",
                          function_path=("test_item_by_id",), message="x", reason="assert_failed")
    result = repair_call(FencedModel(), settings(), CONTRACT, target, CODE)
    assert result.ok
    assert "def test_item_by_id" in result.code
    assert "assert response.status_code == 200" in result.code
    assert result.tokens == (10, 9, 19)


def test_repair_call_accepts_plain_code_without_fence():
    class PlainModel:
        def invoke(self, messages):
            return SimpleNamespace(
                content="def test_item_by_id(base_url, api_client, item_id):\n    assert item_id is not None\n",
                usage_metadata={},
            )

    target = RepairTarget(kind="function", file="test_catalogue.py",
                          function_path=("test_item_by_id",), message="x", reason="assert_failed")
    result = repair_call(PlainModel(), settings(), CONTRACT, target, CODE)
    assert result.ok
    assert "def test_item_by_id" in result.code
    assert "assert item_id is not None" in result.code


def test_repair_call_rejects_junk_that_parses_but_is_not_code():
    class JunkModel:
        def invoke(self, messages):
            # "True" is valid Python but not a test module.
            return SimpleNamespace(content="True", usage_metadata={})

    target = RepairTarget(kind="function", file="test_catalogue.py",
                          function_path=("test_item_by_id",), message="x", reason="assert_failed")
    result = repair_call(JunkModel(), settings(), CONTRACT, target, CODE)
    assert not result.ok
    assert "not valid" in result.error
