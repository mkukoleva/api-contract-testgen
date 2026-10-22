"""Contract-derived category planning and delivery to the generation model."""

import copy
import json
from types import SimpleNamespace

import pytest

from prototype.generator.contracts import GenerationSettings
from prototype.generator.generate import generate_suite
from prototype.generator.scenarios import build_scenario_plan
from prototype.generator.scenarios import category_evidence


def test_allof_duplicate_conditions_are_requested_once():
    schema = {"type": "integer", "minimum": 1, "maximum": 5}
    spec = {"paths": {"/items": {"get": {"parameters": [{"in": "query", "name": "n",
        "schema": {"allOf": [schema, schema]}}], "responses": {"200": {}, "400": {}}}}}}
    plan = build_scenario_plan(spec)["operations"][0]
    assert len([case for case in plan["boundary"] if case.get("constraint") == "minimum"]) == 1
    assert len([case for case in plan["error"]["input_variations"] if case["check"] == "invalid_type"]) == 1


@pytest.mark.parametrize("schema,allowed", [
    ({"type": "string", "enum": ["standard", "gift", "express"]}, False),
    ({"type": "string", "enum": ["", "gift"]}, True),
    ({"type": "string", "pattern": "^[a-z]+$"}, False),
    ({"type": "string", "pattern": "^[a-z]*$"}, True),
    ({"allOf": [{"type": "string"}, {"enum": ["gift"]}]}, False),
    ({"type": "array", "enum": [["gift"]]}, False),
    ({"type": "array", "items": {"type": "string", "enum": ["gift"]}}, True),
])
def test_empty_boundary_respects_enum_pattern_and_enclosing_composition(schema, allowed):
    spec = {"paths": {"/items": {"post": {"requestBody": {"content": {"application/json": {
        "schema": schema}}}, "responses": {"200": {}, "422": {}}}}}}
    operation = build_scenario_plan(spec)["operations"][0]
    empty_root = [case for case in operation["boundary"]
                  if case["target"] == "body:application/json" and case["check"] == "empty_if_allowed"]
    assert bool(empty_root) is allowed
    assert not any(case["check"] == "empty_if_allowed" and case["target"].endswith("[]")
                   for case in operation["boundary"])


def observed_run():
    tests, events = [], []
    for category, status in (("normal", 200), ("boundary", 200), ("error", 400)):
        nodeid = f"test_api.py::test_{category}_items[value::label]"
        tests.append({"nodeid": nodeid, "outcome": "passed"})
        events.append({"nodeid": nodeid, "method": "GET", "path": "/items", "status_code": status})
    return {"status": "completed", "tests": tests, "http_observations": events,
            "http_observations_complete": True}


def test_category_evidence_requires_both_outcome_and_observed_operation(contract):
    plan = build_scenario_plan(contract)
    result = category_evidence(plan, observed_run())
    assert result["status"] == "incomplete"  # POST was not exercised.
    assert all(value["status"] == "observed" for value in result["operations"][0]["categories"].values())
    assert all(value["status"] == "missing" for value in result["operations"][1]["categories"].values())


@pytest.mark.parametrize("change", ["skip", "failure", "wrong_method", "wrong_status", "no_http"])
def test_names_alone_cannot_claim_a_category(contract, change):
    run = observed_run()
    if change == "skip":
        run["tests"][2]["outcome"] = "skipped"
    elif change == "failure":
        run["tests"][2]["outcome"] = "failed"
    elif change == "wrong_method":
        run["http_observations"][2]["method"] = "DELETE"
    elif change == "wrong_status":
        run["http_observations"][2]["status_code"] = 200
    else:
        run["http_observations"] = []
    result = category_evidence(build_scenario_plan(contract), run)
    assert result["operations"][0]["categories"]["error"]["status"] == "missing"


@pytest.mark.parametrize("change", ["timeout", "partial", "absent"])
def test_incomplete_observations_do_not_claim_missing_or_complete_categories(contract, change):
    run = observed_run()
    if change == "timeout":
        run["status"] = "timeout"
    elif change == "partial":
        run["http_observations_complete"] = False
    else:
        run.pop("http_observations")
    assert category_evidence(build_scenario_plan(contract), run)["status"] == "unavailable"


def test_exact_route_wins_and_unspecified_categories_need_clarification():
    spec = {"paths": {route: {"get": {"responses": {"200": {}}}}
                       for route in ("/items/{id}", "/items/size")}}
    run = observed_run()
    run["http_observations"] = [{**run["http_observations"][0], "path": "/items/size"}]
    result = category_evidence(build_scenario_plan(spec), run)
    assert result["operations"][0]["categories"]["normal"]["status"] == "missing"
    assert result["operations"][1]["categories"]["normal"]["status"] == "observed"
    assert result["operations"][1]["categories"]["error"]["status"] == "needs_contract_clarification"


@pytest.fixture
def contract():
    return {"openapi": "3.0.3", "paths": {"/items": {
        "parameters": [{"name": "limit", "in": "query", "schema": {
            "type": "integer", "minimum": 1, "maximum": 100}}],
        "get": {"parameters": [{"name": "limit", "in": "query", "schema": {
            "type": "integer", "minimum": 2, "maximum": 10}}],
            "responses": {"200": {"description": "OK"}, "400": {"description": "Invalid limit"}}},
        "post": {"requestBody": {"required": True, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"}}}},
            "responses": {"201": {"description": "Created"}, "422": {"description": "Invalid body"}}}}},
        "components": {"schemas": {"Item": {"type": "object", "required": ["name", "id"],
            "properties": {"id": {"type": "string", "readOnly": True},
                "name": {"type": "string", "minLength": 1, "maxLength": 30},
                "tags": {"type": "array", "minItems": 0, "maxItems": 3,
                         "items": {"type": "string", "enum": ["red", "blue"]}}}}}}}


def test_boundaries_are_derived_from_input_and_operation_overrides_path(contract):
    original = copy.deepcopy(contract)
    plan = build_scenario_plan(contract)
    get = plan["operations"][0]
    bounds = [case["value"] for case in get["boundary"] if case["check"] == "valid_boundary"]
    assert bounds == [2, 10]
    assert {"target": "query:limit", "check": "omit_optional"} in get["boundary"]
    assert get["normal"]["documented_statuses"] == ["200"]
    assert get["error"]["documented_statuses"] == ["400"]
    assert contract == original


def test_body_required_optional_and_nested_inputs_are_planned(contract):
    post = build_scenario_plan(contract)["operations"][1]
    cases = post["error"]["input_variations"]
    assert {"target": "body", "check": "omit_required"} in cases
    assert {"target": "body:application/json.name", "check": "omit_required"} in cases
    assert not any(case["target"].endswith(".id") for case in cases)
    assert any(case["target"].endswith("tags[]") and case["check"] == "outside_enum" for case in cases)
    assert any(case["target"].endswith(".tags") and case["check"] == "omit_optional"
               for case in post["boundary"])


def test_response_constraints_are_not_mistaken_for_input_boundaries():
    contract = {"paths": {"/items": {"get": {"responses": {200: {"schema": {
        "type": "array", "minItems": 1, "maxItems": 10}}}}}}}
    operation = build_scenario_plan(contract)["operations"][0]
    assert operation["boundary"] == []
    assert not operation["error"]["available"]
    assert operation["limitations"]


def test_undocumented_errors_and_path_omission_are_not_assumed():
    contract = {"paths": {"/items/{id}": {"get": {
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}], "responses": {"200": {}}}}}}
    operation = build_scenario_plan(contract)["operations"][0]
    assert not operation["error"]["available"]
    assert operation["error"]["documented_statuses"] == []
    assert operation["boundary"] == []  # An empty path segment changes the route.
    assert not any(case["check"] in ("omit_required", "nonexistent_resource")
                   for case in operation["error"]["input_variations"])


def test_swagger_body_and_documented_not_found():
    contract = {"swagger": "2.0", "paths": {"/items/{id}": {"post": {
        "parameters": [{"name": "body", "in": "body", "required": True, "schema": {
            "type": "object", "required": ["count"], "properties": {"count": {
                "type": "integer", "minimum": 0, "exclusiveMaximum": True, "maximum": 5}}}}],
        "responses": {"200": {}, "404": {}}}}}}
    operation = build_scenario_plan(contract)["operations"][0]
    assert operation["error"]["available"]
    assert {"target": "resource", "check": "nonexistent_resource"} in operation["error"]["input_variations"]
    assert any(case.get("constraint") == "maximum" and case["exclusive"]
               and case["value"] == 5 for case in operation["boundary"])


def test_generation_passes_category_plan_to_model_and_preserves_output(contract):
    seen = []
    class Model:
        def invoke(self, messages):
            seen.extend(messages)
            return SimpleNamespace(content=json.dumps({"files": [{"name": "test_items.py",
                "code": "def test_normal_items(base_url, api_client):\n"
                        "    assert api_client.get(f'{base_url}/items').status_code == 200\n"}]}))
    result = generate_suite(contract, GenerationSettings("unused.yaml",
        source_context={"text": "STATIC_SOURCE_CONTEXT_MARKER"}), model=Model())
    assert not result["errors"] and len(result["prepared"]) == 1
    assert result["scenario_plan"] == build_scenario_plan(contract)
    assert '"boundary"' in seen[1]["content"] and '"outside_boundary"' in seen[1]["content"]
    assert "Граничный случай не обязательно возвращает ошибку" in seen[0]["content"]
    assert "STATIC_SOURCE_CONTEXT_MARKER" in seen[1]["content"]


def test_allof_required_is_not_planned_as_optional_input():
    contract = {"paths": {"/items": {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"allOf": [{"type": "object", "properties": {"id": {"type": "string"}}},
                              {"required": ["id"]}]}}}}, "responses": {"200": {}, "400": {}}}}}}
    plan = build_scenario_plan(contract)["operations"][0]
    assert {"target": "body:application/json.id", "check": "omit_required"} in plan["error"]["input_variations"]
    assert not any(case["target"].endswith(".id") and case["check"] == "omit_optional" for case in plan["boundary"])
    assert not any("allOf[" in case["target"] for case in plan["error"]["input_variations"])


@pytest.mark.parametrize("field,value", [("parameters", None), ("parameters", {}),
                                       ("requestBody", {"content": None})])
def test_malformed_input_structure_is_reported_before_model_call(field, value):
    contract = {"paths": {"/items": {"post": {field: value, "responses": {"200": {}}}}}}
    class NoModel:
        def invoke(self, messages):
            pytest.fail("Invalid structure must not reach LLM")
    result = generate_suite(contract, GenerationSettings("unused.yaml"), model=NoModel())
    assert result["errors"] and not result["prepared"] and result["calls"] == 0


@pytest.mark.parametrize("constraint", [{"minLength": None}, {"minItems": -1}, {"maximum": "bad"}])
def test_malformed_schema_bounds_do_not_crash_generation(constraint):
    contract = {"paths": {"/items": {"get": {"parameters": [{"name": "q", "in": "query",
        "schema": {"type": "string", **constraint}}], "responses": {"200": {}}}}}}
    result = generate_suite(contract, GenerationSettings("unused.yaml"), model=SimpleNamespace(
        invoke=lambda _: pytest.fail("No LLM call for malformed bounds")))
    assert result["errors"] and not result["prepared"] and result["calls"] == 0
