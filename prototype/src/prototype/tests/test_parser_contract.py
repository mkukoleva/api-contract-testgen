"""Contract input validation; no agent, LLM or network."""

import json

import pytest
import yaml

from prototype.parser.contract import _load_contract, normalize_contract, read_contract_summary


VALID = {"openapi": "3.0.3", "info": {"title": "Items", "version": "1"},
         "paths": {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}}}


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_valid_contract_formats_have_same_summary(tmp_path, suffix):
    path = tmp_path / ("contract" + suffix)
    text = json.dumps(VALID) if suffix == ".json" else yaml.safe_dump(VALID)
    path.write_text(text, encoding="utf-8")
    summary = read_contract_summary(str(path))
    assert summary["specification"] == "OpenAPI 3.0.3"
    assert summary["title"] == "Items"
    assert summary["operations"] == ["GET /items"]


@pytest.mark.parametrize("changes,field", [
    ({"paths": None}, "paths"), ({"paths": []}, "paths"),
    ({"info": None}, "info"), ({"info": "Items"}, "info"),
    ({"paths": {1: {"get": {}}}}, "paths"),
    ({"paths": {"/items": None}}, "paths"),
    ({"paths": {"/items": {1: {}}}}, "paths"),
    ({"paths": {"/items": {"get": []}}}, "get"),
])
def test_malformed_structure_has_readable_error(tmp_path, changes, field):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(VALID | changes), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        _load_contract(path)


@pytest.mark.parametrize("spec", [{}, {"openapi": "2.0"}, {"openapi": 3},
                                   {"swagger": "1.2"}, {"openapi": "3.0.3", "swagger": "2.0"}])
def test_unsupported_specification_has_readable_error(tmp_path, spec):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"info": {}, "paths": {}} | spec), encoding="utf-8")
    with pytest.raises(ValueError, match="OpenAPI|Swagger"):
        _load_contract(path)


def test_invalid_yaml_is_reported_as_input_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("paths: [", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON/YAML"):
        _load_contract(path)


def test_swagger_and_path_metadata_remain_supported(tmp_path):
    path = tmp_path / "swagger.yaml"
    path.write_text(yaml.safe_dump({"swagger": "2.0", "info": {}, "paths": {
        "/items": {"parameters": [], "$ref": "#/x-path-item", "GET": {}},
        "x-note": "extension",}, "x-path-item": {}}), encoding="utf-8")
    summary = read_contract_summary(str(path))
    assert summary["specification"] == "Swagger 2.0"
    assert summary["operations"] == ["GET /items"]


@pytest.mark.parametrize("paths", [None, {}, {"/items": {"parameters": []}}, {"x-note": "extension"}])
def test_missing_paths_or_http_operations_are_rejected(tmp_path, paths):
    data = {"openapi": "3.0.3", "info": {"title": "Demo", "version": "1"}}
    if paths is not None:
        data["paths"] = paths
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="paths|HTTP"):
        read_contract_summary(str(path))


def test_summary_preserves_api_version(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    assert read_contract_summary(str(path))["version"] == "1"


def test_path_item_reference_counts_as_an_http_operation(tmp_path):
    data = {"openapi": "3.1.0", "info": {}, "paths": {"/items": {"$ref": "#/components/pathItems/Items"}},
            "components": {"pathItems": {"Items": {"get": {"responses": {"200": {"description": "OK"}}}}}}}
    path = tmp_path / "refs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert read_contract_summary(str(path))["operations"] == ["GET /items"]


def test_dangling_local_reference_has_readable_input_error(tmp_path):
    path = tmp_path / "refs.json"
    data = VALID | {"paths": {"/items": {"get": {"responses": {"200": {"$ref": "#/missing"}}}}}}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"\$ref"):
        _load_contract(path)


def test_ref_like_property_names_and_example_data_are_not_references():
    data = {"components": {"schemas": {"Item": {"type": "object", "properties": {
        "$ref": {"type": "string"}}, "example": {"$ref": "#/literal-payload"},
        "enum": [{"$ref": "#/another-literal"}]} }}}
    normalized = normalize_contract(data)
    assert normalized == data
    assert normalized is not data


def test_json_pointer_escaping_and_recursive_refs_are_preserved():
    data = {"schema": {"$ref": "#/definitions/A~1B~0C"}, "definitions": {
        "A/B~C": {"type": "object", "properties": {"child": {"$ref": "#/definitions/A~1B~0C"}}}}}
    result = normalize_contract(data)
    assert result["schema"]["type"] == "object"
    assert result["schema"]["properties"]["child"]["$ref"] == "#/definitions/A~1B~0C"
    assert data["schema"] == {"$ref": "#/definitions/A~1B~0C"}


@pytest.mark.parametrize("index", ["-1", "01"])
def test_invalid_json_pointer_array_index_is_rejected(index):
    data = {"schema": {"$ref": f"#/components/schemas/Union/anyOf/{index}"},
        "components": {"schemas": {"Union": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}}
    with pytest.raises(ValueError, match=r"\$ref"):
        normalize_contract(data)


def test_null_reference_is_an_input_error_without_changing_literal_payload():
    with pytest.raises(ValueError, match=r"\$ref"):
        normalize_contract({"schema": {"$ref": None}})
    assert normalize_contract({"example": {"$ref": None}}) == {"example": {"$ref": None}}
