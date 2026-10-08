"""Contract input validation; no agent, LLM or network."""

import json

import pytest
import yaml

from prototype.parser.contract import _load_contract, read_contract_summary


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
        "/items": {"parameters": [], "$ref": "#/paths/shared", "GET": {}},
        "x-note": "extension",}}), encoding="utf-8")
    summary = read_contract_summary(str(path))
    assert summary["specification"] == "Swagger 2.0"
    assert summary["operations"] == ["GET /items"]
