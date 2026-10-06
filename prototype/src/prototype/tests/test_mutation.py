"""Regression tests of the mutation generator (2.2.9), not API suite scores."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from prototype.evaluate.mutation import (
    MutationType, calculate_mutation_score, count_mutants_by_operator,
    generate_mutants, generate_response_plans, json_pointer,
    mutate_remove_required, mutate_status_code_swap, mutate_type_change,
)


@pytest.fixture
def contract():
    return {
        "openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
        "paths": {"/items/{id}": {"get": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
            ],
            "responses": {"200": {"description": "OK", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
            }}, "500": {"description": "Existing error"}}
        }}},
        "components": {"schemas": {"Item": {
            "type": "object", "required": ["id", "name"], "properties": {
                "id": {"type": "integer"}, "name": {"type": "string"},
                "price": {"type": "number"}, "active": {"type": "boolean"},
                "tags": {"type": "array", "items": {"type": "string"}},
            }
        }}}
    }


def read_pointer(value, pointer):
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            key = int(key)
        elif key not in value and key.isdigit() and int(key) in value:
            key = int(key)
        value = value[key]
    return value


def differences(left, right, path=()):
    """Independent structural diff for verifying one mutation per export."""
    if isinstance(left, dict) and isinstance(right, dict):
        changed = []
        for key in left.keys() | right.keys():
            if key not in left or key not in right:
                changed.append((*path, key))
            else:
                changed.extend(differences(left[key], right[key], (*path, key)))
        return changed
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [change for i, (a, b) in enumerate(zip(left, right))
                for change in differences(a, b, (*path, i))]
    if left != right:
        return [path]
    return []


def test_all_operators_do_not_modify_input_and_change_only_the_target(contract):
    before = copy.deepcopy(contract)
    mutants = generate_mutants(contract)
    assert contract == before
    assert set(count_mutants_by_operator(mutants)) == {o.value for o in MutationType}
    assert len({m.mutant_id for m in mutants}) == len(mutants)
    for m in mutants:
        changed = differences(before, m.mutated_contract)
        if m.operator == MutationType.STATUS_CODE_SWAP:
            assert len(changed) == 2  # one response key renamed: remove + insert
            old = read_pointer(before, m.target_path)
            responses = read_pointer(m.mutated_contract, m.target_path.rsplit("/", 1)[0])
            assert responses[m.mutated_value] == old
        else:
            assert [json_pointer(path) for path in changed] == [m.target_path]
            assert read_pointer(before, m.target_path) == m.original_value
            assert read_pointer(m.mutated_contract, m.target_path) == m.mutated_value
    # Mutants own independent copies, including payload metadata.
    mutants[0].mutated_contract["info"]["title"] = "changed"
    assert mutants[1].mutated_contract["info"]["title"] == "Items"
    assert contract == before


def test_types_are_changed_only_in_schemas(contract):
    decoy = {"type": "string", "required": ["id"]}
    contract["info"]["x-test"] = copy.deepcopy(decoy)
    contract["components"]["schemas"]["Item"]["example"] = copy.deepcopy(decoy)
    contract["components"]["schemas"]["Item"]["x-extension"] = copy.deepcopy(decoy)
    mutants = generate_mutants(contract)
    assert not any("example" in m.target_path or "x-extension" in m.target_path
                   or m.target_path.startswith("/info") for m in mutants)
    assert {m.original_value for m in mutants if m.operator == MutationType.TYPE_CHANGE} == {
        "object", "integer", "string", "number", "boolean", "array"
    }


def test_pointer_names_with_slash_tilde_dot_and_brackets(contract):
    props = contract["components"]["schemas"]["Item"]["properties"]
    props["a/b~c.d[0]"] = {"type": "integer"}
    mutant = next(m for m in mutate_type_change(contract) if "a~1b~0c.d[0]" in m.target_path)
    assert read_pointer(mutant.mutated_contract, mutant.target_path) == "string"
    assert json_pointer(["a/b", "~", "", 0]) == "/a~1b/~0//0"


def test_path_parameter_cannot_become_optional(contract):
    mutants = mutate_remove_required(contract)
    assert not any("/parameters/0/" in m.target_path for m in mutants)
    query_mutant = next(m for m in mutants if "/parameters/1/" in m.target_path)
    assert query_mutant.original_value is True and query_mutant.mutated_value is False


def test_required_exports_are_explicit_schema_weakening(contract):
    mutants = mutate_remove_required(contract)
    schema_mutants = [m for m in mutants if isinstance(m.original_value, list)]
    assert len(schema_mutants) == 2
    assert {tuple(m.mutated_value) for m in schema_mutants} == {("id",), ("name",)}
    assert all(m.mutated_contract["components"]["schemas"]["Item"]["properties"] ==
               contract["components"]["schemas"]["Item"]["properties"] for m in schema_mutants)


def test_status_collision_preserves_existing_response(contract):
    mutants = mutate_status_code_swap(contract)
    mutant = next(m for m in mutants if m.original_value == "200")
    responses = mutant.mutated_contract["paths"]["/items/{id}"]["get"]["responses"]
    assert mutant.mutated_value == "502"
    assert responses["500"] == {"description": "Existing error"}
    assert "200" not in responses
    assert len(responses) == 2


@pytest.mark.parametrize("code", ["default", "2XX", "x-note"])
def test_ambiguous_status_maps_are_not_renamed(code):
    c = {"paths": {"/x": {"get": {"responses": {
        code: {"description": "Any"}, "200": {"description": "OK"}
    }}}}}
    mutants = mutate_status_code_swap(c)
    assert not any(m.original_value == code for m in mutants)
    if code != "x-note":
        assert mutants == []


@pytest.mark.parametrize("code", ["204", "205", "304", "099", "777", "", "abc"])
def test_unsupported_status_codes_are_skipped(code):
    c = {"paths": {"/x": {"get": {"responses": {code: {"description": "x"}}}}}}
    assert mutate_status_code_swap(c) == []


def test_integer_yaml_status_key_and_schema_target():
    c = yaml.safe_load('''openapi: 3.0.3
paths:
  /x:
    get:
      responses:
        200:
          description: OK
          content:
            application/json:
              schema:
                type: integer
''')
    mutant = mutate_type_change(c)[0]
    assert read_pointer(mutant.mutated_contract, mutant.target_path) == "string"
    assert mutate_status_code_swap(c)[0].mutated_value == "500"
    assert len(generate_response_plans(c).plans) == 2


def test_filter_and_deduplication(contract):
    assert generate_mutants(contract, []) == []
    only = generate_mutants(contract, [MutationType.TYPE_CHANGE, MutationType.TYPE_CHANGE])
    assert len(only) == len(mutate_type_change(contract))
    with pytest.raises(ValueError, match="Неизвестный оператор"):
        generate_mutants(contract, ["typo"])


def test_exports_round_trip(contract, tmp_path):
    for mutant in generate_mutants(contract):
        assert json.loads(mutant.dump_json()) == mutant.mutated_contract
        assert yaml.safe_load(mutant.dump_yaml()) == mutant.mutated_contract
        for extension in ("json", "yaml"):
            path = mutant.save_to_file(tmp_path / extension / f"{mutant.mutant_id}.{extension}")
            assert yaml.safe_load(path.read_text()) == mutant.mutated_contract
        assert "mutated_contract" not in mutant.to_dict()


def test_plans_resolve_shared_schema_per_operation_without_mutating_it(contract):
    contract["paths"]["/other"] = {"get": copy.deepcopy(contract["paths"]["/items/{id}"]["get"])}
    before = copy.deepcopy(contract)
    result = generate_response_plans(contract)
    assert contract == before
    assert result.diagnostics == []
    for route in ("/items/{id}", "/other"):
        targets = [p for p in result.plans if p.api_path == route and p.status_code == 200]
        assert {p.operator for p in targets} == set(MutationType)
        price = next(p for p in targets if p.value_path == ("price",))
        assert price.schema_pointer == "/components/schemas/Item/properties/price"
        assert price.original_value == "number" and price.mutated_value == "string"
        missing_name = next(p for p in targets if p.operator == MutationType.REMOVE_REQUIRED
                            and p.value_path == ("name",))
        assert missing_name.original_value == "present" and missing_name.mutated_value == "absent"
    assert len({p.mutant_id for p in result.plans}) == len(result.plans)
    json.dumps(result.to_dict(), allow_nan=False)
    # Input mapping order does not affect IDs or plan order.
    reordered = copy.deepcopy(contract)
    reordered["paths"] = dict(reversed(list(reordered["paths"].items())))
    assert generate_response_plans(reordered).to_dict() == result.to_dict()


def test_plans_array_index_is_explicitly_unbound(contract):
    contract["components"]["schemas"]["Item"]["properties"]["tags"]["items"] = {
        "type": "object", "required": ["a/b~"], "properties": {"a/b~": {"type": "integer"}}
    }
    result = generate_response_plans(contract)
    nested = next(p for p in result.plans if p.operator == MutationType.REMOVE_REQUIRED
                  and p.value_path == ("tags", None, "a/b~"))
    assert nested.to_dict()["value_path"] == ["tags", None, "a/b~"]
    assert any(p.schema_pointer.endswith("/properties/a~1b~0") for p in result.plans)


@pytest.mark.parametrize("schema,reason", [
    ({"$ref": "https://example.test/schema"}, "External"),
    ({"$ref": "#/components/schemas/Missing"}, "Unresolved"),
    ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, "Composed"),
    ({"type": ["string", "null"]}, "unsupported schema type"),
])
def test_unsupported_schema_produces_diagnostic(contract, schema, reason):
    content = contract["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]
    content["application/json"]["schema"] = schema
    result = generate_response_plans(contract)
    assert any(reason in d.reason for d in result.diagnostics)
    assert not any(p.media_type == "application/json" for p in result.plans)


def test_recursive_ref_terminates_but_sibling_targets_remain(contract):
    item = contract["components"]["schemas"]["Item"]
    item["properties"]["parent"] = {"$ref": "#/components/schemas/Item"}
    result = generate_response_plans(contract)
    assert any("Recursive" in d.reason for d in result.diagnostics)
    assert any(p.value_path == ("price",) for p in result.plans)


def test_local_response_ref_and_escaped_schema_ref(contract):
    components = contract["components"]
    components["schemas"]["a/b~"] = {"type": "integer"}
    components["responses"] = {"OK": {"description": "OK", "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/a~1b~0"}}
    }}}
    contract["paths"]["/items/{id}"]["get"]["responses"] = {"200": {"$ref": "#/components/responses/OK"}}
    plans = generate_response_plans(contract).plans
    assert any(p.schema_pointer == "/components/schemas/a~1b~0" for p in plans)


def test_default_map_reports_status_skip_without_hiding_schema_targets(contract):
    responses = contract["paths"]["/items/{id}"]["get"]["responses"]
    responses["default"] = {"description": "Other responses"}
    result = generate_response_plans(contract)
    assert not any(p.operator == MutationType.STATUS_CODE_SWAP for p in result.plans)
    assert any(p.operator == MutationType.TYPE_CHANGE for p in result.plans)
    assert result.diagnostics


def test_unsupported_version_and_non_json_are_visible(contract):
    c = copy.deepcopy(contract)
    c["openapi"] = "3.1.0"
    result = generate_response_plans(c)
    assert not result.plans and result.diagnostics
    content = contract["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]
    content["text/plain"] = {"schema": {"type": "string"}}
    assert any("JSON" in d.reason for d in generate_response_plans(contract).diagnostics)


@pytest.mark.parametrize("value", [None, [], "bad", 42])
def test_invalid_roots_do_not_crash(value):
    assert generate_mutants(value) == []
    assert generate_response_plans(value).diagnostics


def test_demo_contract_does_not_relax_path_parameter():
    path = Path(__file__).parent / "fixtures" / "demo_openapi.yaml"
    c = yaml.safe_load(path.read_text())
    assert mutate_type_change(c) and mutate_status_code_swap(c)
    assert mutate_remove_required(c) == []


@pytest.mark.parametrize("total,killed,expected", [(10, 7, 70.0), (5, 5, 100.0), (5, 0, 0.0), (0, 0, None)])
def test_score_on_evaluated_mutants(total, killed, expected):
    result = calculate_mutation_score(total, killed)
    assert result.mutation_score_percent == expected
    assert result.survived_mutants == total - killed
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize("value", [None, True, 1.5, float("nan"), float("inf"), "10"])
def test_score_rejects_non_integer_counts(value):
    with pytest.raises(TypeError):
        calculate_mutation_score(value, 0)
    with pytest.raises(TypeError):
        calculate_mutation_score(10, value)


@pytest.mark.parametrize("total,killed", [(-1, 0), (2, -1), (2, 3)])
def test_score_rejects_invalid_counts(total, killed):
    with pytest.raises(ValueError):
        calculate_mutation_score(total, killed)


def test_existing_exports_and_metrics_interface_remain_available():
    import prototype.evaluate as ev
    assert ev.generate_mutants is generate_mutants
    assert ev.calculate_mutation_score is calculate_mutation_score
    from prototype.evaluate.metrics import calculate_metrics
    metrics = calculate_metrics(agent_result={}, contract_summary={"operation_count": 3},
                               generation_time_seconds=1, mutation_score_percent=75)
    assert metrics.mutation_score_percent == 75


# Validation regression checks
"""Validation of mutation inputs and prepared defects, without HTTP experiments."""

import copy
from dataclasses import replace
import json

import pytest

from prototype.evaluate.mutation import MutationType, generate_mutants, generate_response_plans
from prototype.evaluate.mutation import (
    prepare_response_mutation, validate_contract, validate_contract_mutant,
)


@pytest.fixture
def validation_contract():
    return {"openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
            "paths": {"/items": {"get": {"responses": {"200": {
                "description": "OK", "content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/Item"
                }}}
            }}}}}, "components": {"schemas": {"Item": {
                "type": "object", "required": ["id", "name"], "properties": {
                    "id": {"type": "integer"}, "name": {"type": "string"},
                    "price": {"type": "integer", "minimum": 0},
                    "tags": {"type": "array", "items": {"type": "object",
                             "required": ["a/b~"], "properties": {"a/b~": {"type": "string"}}}},
                }
            }}}}


@pytest.fixture
def body():
    return {"id": 1, "name": "Book", "price": 100, "tags": [{"a/b~": "x"}]}


def plan_for(validation_contract, operator, path=()):
    return next(p for p in generate_response_plans(validation_contract).plans
                if p.operator == operator and p.value_path == path)


def test_valid_contract_is_unchanged(validation_contract):
    original = copy.deepcopy(validation_contract)
    assert validate_contract(validation_contract).status == "valid"
    assert validation_contract == original


@pytest.mark.parametrize("broken", [{}, [], {"openapi": "3.1.0"}, {"openapi": "3.0.3"}])
def test_missing_structure_or_unsupported_version_cannot_pass(broken):
    result = validate_contract(broken)
    assert result.status in {"invalid", "unsupported"}
    assert result.issues


def test_optional_path_parameter_is_invalid(validation_contract):
    validation_contract["paths"]["/items/{id}"] = {"get": {
        "parameters": [{"name": "id", "in": "path", "required": False, "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "OK"}}
    }}
    assert validate_contract(validation_contract).status == "invalid"


def test_missing_required_path_declaration_is_invalid(validation_contract):
    validation_contract["paths"]["/items/{id}"] = validation_contract["paths"].pop("/items")
    assert validate_contract(validation_contract).status == "invalid"


def test_integer_yaml_status_is_supported_without_changing_source(validation_contract):
    response = validation_contract["paths"]["/items"]["get"]["responses"].pop("200")
    validation_contract["paths"]["/items"]["get"]["responses"][200] = response
    assert validate_contract(validation_contract).status == "valid"
    assert 200 in validation_contract["paths"]["/items"]["get"]["responses"]


def test_duplicate_integer_and_string_status_is_invalid(validation_contract):
    validation_contract["paths"]["/items"]["get"]["responses"][200] = {"description": "Duplicate"}
    assert validate_contract(validation_contract).status == "invalid"


@pytest.mark.parametrize("ref,status", [("https://example.invalid/schema", "unsupported"),
                                      ("file:///private/test.json", "unsupported"),
                                      ("#/components/schemas/Missing", "invalid"),
                                      ("#/components/schemas/bad~2escape", "invalid")])
def test_refs_are_checked_before_validator_can_read_external_data(validation_contract, monkeypatch, ref, status):
    validation_contract["components"]["schemas"]["Item"]["properties"]["price"] = {"$ref": ref}
    def forbidden():
        raise AssertionError("External reference must be rejected before validator")
    monkeypatch.setattr("prototype.evaluate.mutation._validators", forbidden)
    assert validate_contract(validation_contract).status == status


def test_missing_validator_is_unavailable_not_valid(validation_contract, monkeypatch):
    def missing():
        raise ImportError("optional dependency")
    monkeypatch.setattr("prototype.evaluate.mutation._validators", missing)
    assert validate_contract(validation_contract).status == "unavailable"


def test_validator_failure_is_unavailable_not_invalid(validation_contract, monkeypatch):
    class Broken:
        def __init__(self, _):
            pass
        def iter_errors(self):
            raise RuntimeError("validator failed")
    monkeypatch.setattr("prototype.evaluate.mutation._validators", lambda: (Broken, None))
    assert validate_contract(validation_contract).status == "unavailable"


def test_nonfinite_and_cyclic_documents_cannot_pass(validation_contract):
    validation_contract["info"]["x-value"] = float("nan")
    assert validate_contract(validation_contract).status == "invalid"
    validation_contract["info"]["x-value"] = validation_contract
    assert validate_contract(validation_contract).status == "invalid"


def test_export_with_type_enum_contradiction_is_invalid(validation_contract):
    validation_contract["components"]["schemas"]["Item"]["properties"]["price"]["enum"] = [100]
    mutant = next(m for m in generate_mutants(validation_contract) if m.target_path.endswith("/price/type"))
    assert validate_contract_mutant(validation_contract, mutant).status == "invalid"


def test_export_with_extra_change_is_invalid(validation_contract):
    mutant = generate_mutants(validation_contract)[0]
    mutant.mutated_contract["info"]["title"] = "unrelated change"
    assert validate_contract_mutant(validation_contract, mutant).status == "invalid"


def test_simple_export_is_valid(validation_contract):
    mutant = next(m for m in generate_mutants(validation_contract) if m.target_path.endswith("/price/type"))
    assert validate_contract_mutant(validation_contract, mutant).status == "valid"


@pytest.mark.parametrize("operator,path", [
    (MutationType.TYPE_CHANGE, ("price",)),
    (MutationType.REMOVE_REQUIRED, ("name",)),
    (MutationType.STATUS_CODE_SWAP, ()),
])
def test_three_defects_are_prepared_without_mutating_inputs(validation_contract, body, operator, path):
    plan = plan_for(validation_contract, operator, path)
    original_body, original_contract = copy.deepcopy(body), copy.deepcopy(validation_contract)
    result = prepare_response_mutation(validation_contract, plan, response_status=200, body=body)
    assert result.status == "ready" and result.violations
    assert body == original_body and validation_contract == original_contract
    assert result.status not in {"killed", "survived"}
    if operator == MutationType.TYPE_CHANGE:
        assert result.body == {**body, "price": "mutation-value"}
        assert result.value_pointer == "/price"
    elif operator == MutationType.REMOVE_REQUIRED:
        assert result.body == {key: value for key, value in body.items() if key != "name"}
        assert result.value_pointer == "/name"
    else:
        assert result.mutated_status == 500 and result.body == body
    json.dumps(result.to_dict(), allow_nan=False)


def test_absent_optional_field_is_not_applicable(validation_contract, body):
    del body["price"]
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",)),
                                       response_status=200, body=body)
    assert result.status == "not_applicable"
    assert result.mutated_status is None


def test_absent_required_field_is_baseline_invalid(validation_contract, body):
    del body["name"]
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.REMOVE_REQUIRED, ("name",)),
                                       response_status=200, body=body)
    assert result.status == "baseline_invalid" and result.violations


@pytest.mark.parametrize("value", [True, -1, "100"])
def test_type_or_constraint_error_in_baseline_blocks_preparation(validation_contract, body, value):
    body["price"] = value
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",)),
                                       response_status=200, body=body)
    assert result.status == "baseline_invalid"


def test_empty_array_does_not_create_fake_target(validation_contract, body):
    body["tags"] = []
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.TYPE_CHANGE, ("tags", None, "a/b~")),
                                       response_status=200, body=body)
    assert result.status == "not_applicable"


def test_first_matching_array_item_has_exact_escaped_pointer(validation_contract, body):
    body["tags"].append({"a/b~": "second"})
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.REMOVE_REQUIRED, ("tags", None, "a/b~")),
                                       response_status=200, body=body)
    assert result.status == "ready" and result.value_pointer == "/tags/0/a~1b~0"
    assert result.body["tags"] == [{}, {"a/b~": "second"}]
    assert body["tags"][0] == {"a/b~": "x"}


def test_wrong_response_status_and_media_are_not_applied(validation_contract, body):
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    assert prepare_response_mutation(validation_contract, plan, response_status=404, body=body).status == "not_applicable"
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body,
                                     media_type="text/plain").status == "not_applicable"
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body,
                                     media_type="application/json; charset=utf-8").status == "ready"


def test_stale_or_forged_plan_is_invalid(validation_contract, body):
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    assert prepare_response_mutation(validation_contract, replace(plan, mutated_value="integer"),
                                     response_status=200, body=body).status == "invalid"
    validation_contract["components"]["schemas"]["Item"]["properties"]["price"]["type"] = "number"
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "invalid"


def test_nullable_value_can_be_changed_to_incompatible_type(validation_contract, body):
    validation_contract["components"]["schemas"]["Item"]["properties"]["name"]["nullable"] = True
    body["name"] = None
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.TYPE_CHANGE, ("name",)),
                                       response_status=200, body=body)
    assert result.status == "ready" and result.body["name"] == 0


def test_whole_response_root_can_be_changed(validation_contract):
    validation_contract["components"]["schemas"]["Item"] = {"type": "integer"}
    result = prepare_response_mutation(validation_contract, plan_for(validation_contract, MutationType.TYPE_CHANGE),
                                       response_status=200, body=1)
    assert result.status == "ready" and result.value_pointer == "" and result.body == "mutation-value"


def test_composed_and_recursive_response_schemas_are_unsupported(validation_contract, body):
    validation_contract["components"]["schemas"]["Item"]["properties"]["name"] = {
        "oneOf": [{"type": "string"}, {"type": "integer"}]
    }
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "unsupported"
    validation_contract["components"]["schemas"]["Item"]["properties"]["name"] = {"$ref": "#/components/schemas/Item"}
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "unsupported"


def test_literal_ref_in_example_and_property_named_ref_are_not_resolved(validation_contract):
    item = validation_contract["components"]["schemas"]["Item"]
    item["example"] = {"$ref": "https://example.invalid/literal"}
    item["properties"]["$ref"] = {"type": "string"}
    item["properties"]["example"] = {"$ref": "#/components/schemas/Scalar"}
    validation_contract["components"]["schemas"]["Scalar"] = {"type": "string"}
    assert validate_contract(validation_contract).status == "valid"


def test_external_ref_under_property_named_example_is_still_blocked(validation_contract):
    validation_contract["components"]["schemas"]["Item"]["properties"]["example"] = {
        "$ref": "https://example.invalid/schema"
    }
    assert validate_contract(validation_contract).status == "unsupported"


def test_readonly_required_field_cannot_be_deleted_from_response(validation_contract, body):
    validation_contract["components"]["schemas"]["Item"]["properties"]["id"]["readOnly"] = True
    plan = plan_for(validation_contract, MutationType.REMOVE_REQUIRED, ("id",))
    result = prepare_response_mutation(validation_contract, plan, response_status=200, body=body)
    assert result.status == "ready"


def test_optional_array_items_are_searched_in_order(validation_contract, body):
    item = validation_contract["components"]["schemas"]["Item"]["properties"]["tags"]["items"]
    del item["required"]
    body["tags"] = [{}, {"a/b~": "second"}]
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("tags", None, "a/b~"))
    result = prepare_response_mutation(validation_contract, plan, response_status=200, body=body)
    assert result.status == "ready" and result.value_pointer == "/tags/1/a~1b~0"
    assert result.body["tags"] == [{}, {"a/b~": 0}]


def test_invalid_original_contract_blocks_preparation(validation_contract, body):
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    del validation_contract["info"]
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "invalid"


def test_nonfinite_response_is_invalid(validation_contract, body):
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    body["price"] = float("nan")
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "invalid"


@pytest.mark.parametrize("fail_on", [1, 2])
def test_response_validator_crash_never_means_ready(validation_contract, body, monkeypatch, fail_on):
    calls = 0
    def broken(schema, value):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise RuntimeError("validation failed")
        return []
    monkeypatch.setattr("prototype.evaluate.mutation._body_errors", broken)
    plan = plan_for(validation_contract, MutationType.TYPE_CHANGE, ("price",))
    assert prepare_response_mutation(validation_contract, plan, response_status=200, body=body).status == "unavailable"


# Campaign regression checks
from dataclasses import replace
import json
from pathlib import Path
import pytest
from prototype.evaluate.mutation import generate_response_plans
from prototype.evaluate.mutation import (
    CaseObservation, DeliveryEvidence, SuiteObservation, classify_result,
    run_mutation_campaign, summarise_results,
)

CONTRACT = {'openapi': '3.0.3', 'info': {'title': 'Mutation demonstration', 'version': '1.0.0'}, 'paths': {'/items/1': {'get': {'responses': {'200': {'description': 'Item', 'content': {'application/json': {'schema': {'type': 'object', 'required': ['id', 'name', 'price'], 'properties': {'id': {'type': 'integer'}, 'name': {'type': 'string'}, 'price': {'type': 'integer', 'minimum': 0}}, 'additionalProperties': False}}}}}}}}}
PLAN = generate_response_plans(CONTRACT).plans[0]
EXPECTED = ('test_api.py::test_item',)


def observation(plan=None, *, outcome='passed', delivered=True):
    return SuiteObservation('completed', int(outcome == 'failed'), EXPECTED,
        (CaseObservation(EXPECTED[0], outcome, failure_kind='assertion' if outcome == 'failed' else None,
                         failure_signature='test_api.py:10:AssertionError' if outcome == 'failed' else None),),
        (DeliveryEvidence(plan.mutant_id, EXPECTED[0], 'call', True, True),) if plan and delivered else (),
        'ready' if plan else None)


@pytest.mark.parametrize('outcome,delivered,expected', [
    ('failed', True, 'killed'), ('passed', True, 'survived'), ('passed', False, 'not_exercised'),
    ('failed', False, 'inconclusive'),
])
def test_classification(outcome, delivered, expected):
    run = observation(PLAN, outcome=outcome, delivered=delivered)
    assert classify_result(PLAN, [run, run], observation(), EXPECTED)[0] == expected


@pytest.mark.parametrize('fault', ['timeout', 'skipped', 'setup', 'changed_collection', 'missing_call',
    'duplicate', 'wrong_id', 'unvalidated', 'undelivered', 'other_test', 'other_exception', 'exit_mismatch'])
def test_bad_evidence_never_kills(fault):
    run = observation(PLAN, outcome='failed')
    case, delivery = run.cases[0], run.deliveries[0]
    if fault == 'timeout': run = replace(run, status='timeout')
    elif fault == 'skipped': run = replace(run, cases=(replace(case, outcome='skipped'),))
    elif fault == 'setup': run = replace(run, cases=(replace(case, phase='setup'),))
    elif fault == 'changed_collection': run = replace(run, collected_nodeids=('other',))
    elif fault == 'missing_call': run = replace(run, cases=())
    elif fault == 'duplicate': run = replace(run, deliveries=(delivery, delivery))
    elif fault == 'wrong_id': run = replace(run, deliveries=(replace(delivery, mutant_id='wrong'),))
    elif fault == 'unvalidated': run = replace(run, deliveries=(replace(delivery, validated=False),))
    elif fault == 'undelivered': run = replace(run, deliveries=(replace(delivery, delivered=False),))
    elif fault == 'other_test': run = replace(run, deliveries=(replace(delivery, nodeid='other'),))
    elif fault == 'other_exception': run = replace(run, cases=(replace(case, failure_kind='other'),))
    elif fault == 'exit_mismatch': run = replace(run, exit_code=0)
    assert classify_result(PLAN, [run, run], observation(), EXPECTED)[0] == 'inconclusive'


@pytest.mark.parametrize('fault', ['one_attempt', 'different_result', 'different_failure', 'recovery_failed'])
def test_repeat_and_recovery_required(fault):
    run = observation(PLAN, outcome='failed')
    attempts, recovery = [run, run], observation()
    if fault == 'one_attempt': attempts.pop()
    if fault == 'different_result': attempts[1] = observation(PLAN)
    if fault == 'different_failure':
        attempts[1] = replace(run, cases=(replace(run.cases[0], failure_signature='different'),))
    if fault == 'recovery_failed': recovery = observation(outcome='failed')
    assert classify_result(PLAN, attempts, recovery, EXPECTED)[0] == 'inconclusive'


def test_metrics_have_distinct_denominators():
    rows = [{'status': s} for s in ['killed'] * 6 + ['survived'] * 2 + ['not_exercised'] * 2
            + ['invalid', 'unsupported']]
    metrics = summarise_results(rows)
    assert metrics['mutation_score']['percent'] == 75
    assert metrics['evaluated_fraction']['percent'] == 80
    assert metrics['detection_including_unexercised']['percent'] == 60
    metrics = summarise_results(rows + [{'status': 'inconclusive'}])
    assert metrics['mutation_score']['percent'] == 75
    assert metrics['detection_including_unexercised']['percent'] is None
    assert summarise_results([])['mutation_score']['percent'] is None
    assert summarise_results(rows, eligible=False)['mutation_score']['percent'] is None


def test_full_campaign_all_plans_and_artifacts(tmp_path):
    calls = []
    def execute(plan, directory):
        calls.append(plan)
        return observation(plan, outcome='failed' if plan else 'passed')
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: 'stable', output_dir=tmp_path)
    assert report['status'] == 'completed'
    assert report['metrics']['counts']['killed'] == 8
    assert report['run_count'] == len(calls) == 25
    assert report['metrics']['mutation_score']['percent'] == 100
    assert json.loads((Path(report['report_dir']) / 'report.json').read_text()) == json.loads(json.dumps(report))
    assert (Path(report['report_dir']) / 'report.md').is_file()


@pytest.mark.parametrize('baseline', [SuiteObservation('timeout'), SuiteObservation('completed', 0),
    observation(outcome='failed'), replace(observation(), cases=(CaseObservation(EXPECTED[0], 'skipped'),))])
def test_bad_baseline_blocks_campaign(tmp_path, baseline):
    calls = []
    def execute(plan, directory):
        calls.append(plan)
        return baseline
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: 'stable', output_dir=tmp_path)
    assert report['status'] == 'baseline_failed'
    assert len(calls) == 1
    assert report['metrics']['mutation_score']['percent'] is None


def test_input_change_stops_campaign_and_invalidates_metrics(tmp_path):
    state = ['stable']
    def execute(plan, directory):
        if plan: state[0] = 'changed'
        return observation(plan, outcome='failed' if plan else 'passed')
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: state[0], output_dir=tmp_path)
    assert report['status'] == 'inputs_changed'
    assert report['run_count'] == 2
    assert report['metrics']['mutation_score']['percent'] is None


@pytest.mark.parametrize('status', ['invalid', 'unsupported'])
def test_rejected_preparation_is_visible(tmp_path, status):
    def execute(plan, directory):
        return SuiteObservation('not_run', preparation_status=status) if plan else observation()
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: 'stable', output_dir=tmp_path)
    assert report['metrics']['counts'][status] == 8
    assert report['run_count'] == 9
    assert report['metrics']['mutation_score']['percent'] is None


def test_interruption_preserves_report(tmp_path):
    def execute(plan, directory):
        if plan: raise KeyboardInterrupt
        return observation()
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: 'stable', output_dir=tmp_path)
    assert report['status'] == 'interrupted'
    assert report['metrics']['counts']['inconclusive'] == 8


def test_parallel_plans_keep_attempts_and_reports_independent(tmp_path):
    import threading
    import time
    lock = threading.Lock()
    active = 0
    maximum = 0
    def execute(plan, directory):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(.01)
            assert directory.is_dir()
            return observation(plan, outcome='failed' if plan else 'passed')
        finally:
            with lock: active -= 1
    report = run_mutation_campaign(CONTRACT, execute=execute, fingerprint=lambda: 'stable',
                                   output_dir=tmp_path, max_workers=3)
    assert 1 < maximum <= 3
    assert report['status'] == 'completed' and report['run_count'] == 25
    assert report['metrics']['counts']['killed'] == 8
    assert [r['plan']['mutant_id'] for r in report['results']] == [p.mutant_id for p in generate_response_plans(CONTRACT).plans]
    for row in report['results']:
        assert all(a['deliveries'][0]['mutant_id'] == row['plan']['mutant_id'] for a in row['attempts'])


@pytest.mark.parametrize('workers', [0, 9, True, 1.5])
def test_invalid_parallelism_is_rejected(tmp_path, workers):
    with pytest.raises(ValueError, match='max_workers'):
        run_mutation_campaign(CONTRACT, execute=lambda p,d: observation(), fingerprint=lambda: 'stable',
                              output_dir=tmp_path, max_workers=workers)


# Catalogue adapter regression checks
import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import pytest

HERE = Path(__file__).resolve().parents[4] / 'benchmark/mutation/catalogue'
sys.path.insert(0, str(HERE))
from run import convert
from run import adapt_result, CONTRACT as CATALOGUE_CONTRACT
spec = importlib.util.spec_from_file_location('catalogue_proxy', HERE / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
from prototype.evaluate.mutation import generate_response_plans
from prototype.evaluate.mutation import validate_contract
from prototype.service_tools.runner.contracts import RunResult, TestResult as RunnerTestResult


def test_conversion_preserves_response_schemas_and_required():
    original = json.loads(CATALOGUE_CONTRACT.read_text())
    converted = convert(CATALOGUE_CONTRACT)
    assert validate_contract(converted).status == 'valid'
    assert converted['components']['schemas'] == original['definitions']
    assert set(converted['paths']) == set(original['paths'])
    parameter = converted['paths']['/catalogue/{id}']['get']['parameters'][0]
    assert parameter == {'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
    plans = generate_response_plans(converted)
    assert len(plans.plans) == 46 and not plans.diagnostics


def test_unknown_snapshot_cannot_be_silently_converted(tmp_path):
    changed = tmp_path / 'changed.json'
    changed.write_text(CATALOGUE_CONTRACT.read_text() + '\n')
    with pytest.raises(ValueError, match='snapshot changed'): convert(changed)


@pytest.mark.parametrize('path,operation', [('/catalogue/size','/catalogue/size'),
    ('/catalogue/abc','/catalogue/{id}'), ('/catalogue/abc/extra',None), ('/missing',None), ('/tags','/tags')])
def test_match_is_specific_and_bounded(path, operation):
    assert proxy.match_operation(convert(CATALOGUE_CONTRACT), path) == operation


def adapter_observation():
    return {'exit_code': 0, 'collected_nodeids': ['test_api'], 'diagnostics': [],
            'cases': [{'nodeid': 'test_api', 'phase': 'call', 'outcome': 'passed'}]}


def test_runner_errors_remain_authoritative():
    result = RunResult('infrastructure_error', 0, 1, tests=(RunnerTestResult('test_api','passed'),),
                       error_message='Policy failed')
    adapted = adapt_result(result, [], adapter_observation())
    assert adapted.status == 'infrastructure_error'
    assert 'Policy failed' in adapted.diagnostics


def test_missing_observation_cannot_look_complete():
    adapted = adapt_result(RunResult('completed', 0, 1), [], None)
    assert adapted.diagnostics and not adapted.collected_nodeids


def test_upstream_schema_violation_remains_inconclusive():
    result = RunResult('completed', 0, 1, tests=(RunnerTestResult('test_api','passed'),))
    event = {'selected': False, 'preparation': {'status': 'baseline_invalid', 'reason': 'bad original'}}
    adapted = adapt_result(result, [event], adapter_observation())
    assert 'bad original' in adapted.diagnostics
    assert not adapted.deliveries


def test_delivery_requires_matching_validated_payload():
    result = RunResult('completed', 0, 1, tests=(RunnerTestResult('test_api','passed'),))
    event = {'selected': True, 'delivered': True, 'nodeid': 'test_api', 'phase': 'call',
             'body': {'wrong': 1}, 'status': 200,
             'preparation': {'status': 'ready', 'mutant_id': 'RESP-0001', 'body': {'expected': 1},
                             'mutated_status': 200, 'value_pointer': '/id', 'violations': [{'reason': 'type'}]}}
    adapted = adapt_result(result, [event], adapter_observation())
    assert len(adapted.deliveries) == 1 and not adapted.deliveries[0].validated


def test_tag_order_does_not_change_original_data():
    from run import comparable_original
    original = {'status': 200, 'body': [{'id': 'a', 'tag': ['blue', 'black']}]}
    shuffled = {'status': 200, 'body': [{'id': 'a', 'tag': ['black', 'blue']}]}
    assert comparable_original('/catalogue', original) == comparable_original('/catalogue', shuffled)
    assert original['body'][0]['tag'] == ['blue', 'black']


def test_original_tag_values_and_duplicates_remain_significant():
    from run import comparable_original
    original = {'status': 200, 'body': {'tags': ['a', 'b']}}
    for tags in (['a', 'c'], ['a', 'b', 'b'], [1, 'b']):
        changed = {'status': 200, 'body': {'tags': tags}}
        assert comparable_original('/tags', original) != comparable_original('/tags', changed)


def test_product_and_image_order_are_not_normalized():
    from run import comparable_original
    body = [{'id': 'a', 'imageUrl': ['first', 'second']}, {'id': 'b'}]
    original = {'status': 200, 'body': body}
    changed = {'status': 200, 'body': list(reversed(body))}
    assert comparable_original('/catalogue', original) != comparable_original('/catalogue', changed)
    changed = copy.deepcopy(original)
    changed['body'][0]['imageUrl'].reverse()
    assert comparable_original('/catalogue', original) != comparable_original('/catalogue', changed)


def test_compact_adapter_build_includes_all_runtime_files(tmp_path, monkeypatch):
    import run as catalogue_run
    calls = []
    monkeypatch.setattr(catalogue_run, 'image_id', lambda name: 'sha256:fixed')
    monkeypatch.setattr(catalogue_run, 'command', lambda *args, **kwargs: calls.append(args) or 'built')
    assert catalogue_run.build_image(tmp_path / 'build') == 'sha256:fixed'
    context = tmp_path / 'build'
    dockerfile = (context / 'Dockerfile').read_text()
    tag_call = calls[0]
    assert tag_call[:4] == ('docker', 'image', 'tag', 'sha256:fixed')
    pinned_tag = tag_call[4]
    build_call = next(c for c in calls if c[:2] == ('docker', 'build'))
    assert f'BASE_IMAGE={pinned_tag}' in build_call
    assert '--pull=false' in build_call
    assert calls[-1] == ('docker', 'image', 'rm', pinned_tag)
    assert 'COPY mutation.py /opt/mutation/prototype/evaluate/' in dockerfile
    assert 'COPY worker.py /opt/runner/pytest_worker.py' in dockerfile
    for filename in ('mutation.py', 'proxy.py', 'worker.py'):
        assert (context / filename).is_file()
    assert 'mutation_validation.py' not in dockerfile
    assert 'pytest_capture.py' not in dockerfile


def test_combined_worker_records_collection_and_assertion_failure(tmp_path, monkeypatch):
    # Load the observer definitions without starting the Docker-only base worker.
    import ast
    import types
    source = (HERE / 'worker.py').read_text()
    tree = ast.parse(source)
    cutoff = next(n.lineno for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'spec' for t in n.targets))
    observer = types.ModuleType('observer_check')
    exec(compile('\n'.join(source.splitlines()[:cutoff - 1]), str(HERE / 'worker.py'), 'exec'), observer.__dict__)
    monkeypatch.setenv('MUTATION_CONTEXT', str(tmp_path / 'context.json'))
    monkeypatch.setenv('MUTATION_OBSERVATION', str(tmp_path / 'observation.json'))
    item = types.SimpleNamespace(nodeid='test_api::check')
    observer.pytest_collection_finish(types.SimpleNamespace(items=[item]))
    observer.pytest_runtest_logstart(item.nodeid, None)
    assert json.loads((tmp_path / 'context.json').read_text())['phase'] == 'setup'
    report = types.SimpleNamespace(outcome='failed', when='call', failed=True, longrepr='bad response')
    error = types.SimpleNamespace(errisinstance=lambda cls: cls is AssertionError,
        traceback=[types.SimpleNamespace(path='test_api.py', lineno=12)], typename='AssertionError')
    hook = observer.pytest_runtest_makereport(item, types.SimpleNamespace(excinfo=error))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(types.SimpleNamespace(get_result=lambda: report))
    observer.pytest_sessionfinish(None, 1)
    result = json.loads((tmp_path / 'observation.json').read_text())
    assert result['collected_nodeids'] == [item.nodeid]
    assert result['cases'][0]['failure_kind'] == 'assertion'
    assert result['exit_code'] == 1


@pytest.mark.parametrize('failure', ['build', 'wrong_base'])
def test_pinned_base_tag_is_cleaned_on_build_failure(tmp_path, monkeypatch, failure):
    import run as catalogue_run
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'build'):
            raise RuntimeError('build failed')
        return ''
    monkeypatch.setattr(catalogue_run, 'command', command)
    monkeypatch.setattr(catalogue_run, 'image_id', lambda name:
        'sha256:other' if failure == 'wrong_base' and name.startswith('api-contract-mutation-base:')
        else 'sha256:fixed')
    with pytest.raises(RuntimeError, match='build failed|differs'):
        catalogue_run.build_image(tmp_path / 'build')
    assert calls[-1] == ('docker', 'image', 'rm', calls[0][-1])
    if failure == 'wrong_base':
        assert not any(c[:2] == ('docker', 'build') for c in calls)
