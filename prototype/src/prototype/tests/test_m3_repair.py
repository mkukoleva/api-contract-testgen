"""Acceptance regressions for self-repair under ARQA TZ v3, 4.1.7.1."""

import pytest

from prototype.generator.contracts import GenerationSettings
from prototype.generator.repair import RepairTarget, apply_repair, classify_result
from prototype.service_tools.runner.contracts import RunResult, TestResult as Result


def repair(original, candidate, **kwargs):
    target = RepairTarget(kind="file", file="test_api.py", **kwargs)
    return apply_repair({"test_api.py": original}, target, candidate)["test_api.py"]


def test_missing_header_colon_is_repaired_without_changing_checks():
    original = "def test_api(api_client)\n    assert api_client.get('/health').status_code == 200\n"
    candidate = original.replace("api_client)\n", "api_client):\n")
    assert repair(original, candidate, reason="syntax") == candidate


def test_missing_import_can_be_added_without_changing_test_body():
    original = "@pytest.mark.parametrize('x', [1])\ndef test_api(x):\n    assert x == 1\n"
    assert repair(original, "import pytest\n" + original, reason="import") == "import pytest\n" + original


def test_misspelled_pytest_import_can_be_corrected():
    original = "import pytes as pytest\ndef test_api():\n    assert pytest.approx(1) == 1\n"
    candidate = original.replace("pytes as pytest", "pytest")
    assert repair(original, candidate, reason="import") == candidate


def test_missing_fixture_alias_is_renamed_consistently():
    original = "def test_api(base_url, client):\n    response = client.get(base_url + '/health')\n    assert response.status_code == 200\n"
    target = RepairTarget(kind="function", file="test_api.py", function_path=("test_api",),
                          reason="fixture", message="fixture 'client' not found")
    candidate = original.replace("client", "api_client")
    assert apply_repair({"test_api.py": original}, target, candidate)["test_api.py"] == candidate


@pytest.mark.parametrize("candidate", [
    "import pytest as isinstance\ndef test_api():\n    assert isinstance(1, int)\n",
    "import pytest\ndef test_api():\n    assert True\n",
    "import pytest\ndef test_api():\n    pytest.skip('fixed')\n    assert isinstance(1, int)\n",
])
def test_import_repair_cannot_shadow_or_bypass_assertions(candidate):
    with pytest.raises(ValueError, match="review"):
        repair("def test_api():\n    assert isinstance(1, int)\n", candidate, reason="import")


def test_syntax_repair_cannot_change_assertions():
    with pytest.raises(ValueError, match="review"):
        repair("def test_api()\n    assert 1 == 2\n", "def test_api():\n    assert True\n", reason="syntax")


def test_fixture_repair_cannot_collide_with_existing_binding():
    original = "def test_api(client):\n    api_client = object()\n    assert client is not None\n"
    target = RepairTarget(kind="function", file="test_api.py", function_path=("test_api",),
                          reason="fixture", message="fixture 'client' not found")
    with pytest.raises(ValueError, match="review"):
        apply_repair({"test_api.py": original}, target, original.replace("client", "api_client"))


@pytest.mark.parametrize("message", ["assert 404 == 200", "assert 500 == 200", "AssertionError: schema differs"])
def test_api_assertion_alone_never_triggers_repair(message):
    contract = {"paths": {"/items": {"get": {"responses": {"200": {}, "404": {}, "500": {}}}}}}
    code = "def test_api(api_client):\n    response = api_client.get('/items')\n    assert response.status_code == 200\n"
    result = RunResult("completed", 1, .1, tests=(Result("test_api.py::test_api", "failed", message=message),))
    plan = classify_result(contract, {"test_api.py": code}, result, GenerationSettings(contract_path="unused"))
    assert not plan.targets
    assert plan.suspected_defects


def test_name_error_in_call_phase_is_a_file_import_repair():
    code = "def test_api():\n    with pytest.raises(ValueError):\n        int('bad')\n"
    result = RunResult("completed", 1, .1, tests=(Result("test_api.py::test_api", "failed", message="NameError: name 'pytest' is not defined"),))
    plan = classify_result({}, {"test_api.py": code}, result, GenerationSettings(contract_path="unused"))
    assert len(plan.targets) == 1
    assert plan.targets[0].kind == "file"
    assert plan.targets[0].reason == "import"


def test_windows_syntax_path_is_matched_to_generated_file():
    from prototype.service_tools.runner.contracts import CompatibilityIssue
    result = RunResult("collection_error", 2, .1, compatibility_issues=(
        CompatibilityIssue("syntax", r"C:\tmp\test_api.py", "expected ':'", line=1),))
    plan = classify_result({}, {"test_api.py": "def test_api()"}, result, GenerationSettings(contract_path="unused"))
    assert plan.targets[0].file == "test_api.py"


def test_service_unavailability_during_import_does_not_trigger_repair():
    result = RunResult("collection_error", 2, .1, collection_errors=(
        "test_api.py:3: in <module>\nE requests.exceptions.ConnectionError: HTTPConnectionPool(host='catalogue', port=8080): connection refused",))
    plan = classify_result({}, {"test_api.py": "pass"}, result,
                           GenerationSettings(contract_path="unused", base_url="http://catalogue:8080"))
    assert not plan.targets
    assert plan.environmental


def test_api_response_containing_exception_name_is_not_a_code_error():
    message = "E AssertionError: service response: NameError: backend failure"
    code = "def test_api(api_client):\n    assert api_client.get('/health').status_code == 200\n"
    result = RunResult("completed", 1, .1, tests=(Result("test_api.py::test_api", "failed", message=message),))
    plan = classify_result({}, {"test_api.py": code}, result, GenerationSettings(contract_path="unused"))
    assert not plan.targets


def test_legacy_target_cannot_change_api_scenario_without_code_error():
    code = "def test_api(api_client):\n    response = api_client.get('/negative')\n    assert response.status_code == 200\n"
    with pytest.raises(ValueError, match="review"):
        repair(code, code.replace("/negative", "/positive"))


def test_import_repair_cannot_reorder_binding_side_effects():
    original = "def pytest():\n    return True\nimport pytest\ndef test_api():\n    assert callable(pytest)\n"
    candidate = "import pytest\ndef pytest():\n    return True\ndef test_api():\n    assert callable(pytest)\n"
    with pytest.raises(ValueError, match="review"):
        repair(original, candidate, reason="import")


def test_import_repair_cannot_introduce_a_new_misspelled_module():
    original = "import pytest\ndef test_api():\n    assert pytest.approx(1) == 1\n"
    with pytest.raises(ValueError, match="review"):
        repair(original, original.replace("import pytest", "import pytes as pytest"), reason="import")


def test_import_repair_cannot_remove_module_docstring_semantics():
    original = '"""Contract tests."""\ndef test_api():\n    assert __doc__ is None\n    assert pytest.approx(1) == 1\n'
    with pytest.raises(ValueError, match="review"):
        repair(original, "import pytest\n" + original, reason="import")
