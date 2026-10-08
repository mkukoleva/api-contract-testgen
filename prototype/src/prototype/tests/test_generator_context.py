"""Contract fragments for prompts: matching and rendering, no network."""

import pytest

from prototype.generator.context import (
    documented_status_codes,
    find_operation,
    load_contract,
    match_contract_path,
    operation_fragment,
    render_contract_for_generation,
)

FIXTURES_DIR = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"

CONTRACT = {
    "openapi": "3.0.3",
    "info": {"title": "Demo Catalogue API", "version": "1.0.0"},
    "paths": {
        "/catalogue": {
            "get": {
                "summary": "Полный каталог",
                "responses": {"200": {"description": "OK"}, "500": {"description": "Err"}},
            }
        },
        "/catalogue/{id}": {
            "get": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Товар",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["id", "name"],
                                    "properties": {
                                        "id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "tags": {"type": "array", "items": {"type": "string"}},
                                    },
                                }
                            }
                        },
                    },
                    "404": {"description": "Нет товара"},
                },
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "Создан"}, "400": {"description": "Bad"}},
            },
        },
    },
}


def test_load_contract_reads_demo_yaml():
    contract = load_contract(FIXTURES_DIR / "demo_openapi.yaml")
    assert contract["info"]["title"] == "Demo Catalogue API"


@pytest.mark.parametrize("template, observed", [
    ("/catalogue/{id}", "/catalogue/2"),
    ("/catalogue/{id}", "http://catalogue:8080/catalogue/999999"),
    ("/catalogue", "/catalogue"),
    ("/catalogue/{id}", "/catalogue/2?verbose=true"),
])
def test_match_contract_path_matches(template, observed):
    assert match_contract_path(template, observed)


@pytest.mark.parametrize("template, observed", [
    ("/catalogue/{id}", "/catalogue"),
    ("/catalogue/{id}", "/catalogue/a/b"),
    ("/catalogue", "/catalogue/2"),
    ("/catalogue/{id}", "/items/2"),
])
def test_match_contract_path_does_not_match(template, observed):
    assert not match_contract_path(template, observed)


def test_find_operation_resolves_template_path():
    found = find_operation(CONTRACT, "get", "/catalogue/42")
    assert found is not None
    contract_path, operation = found
    assert contract_path == "/catalogue/{id}"
    assert "200" in operation["responses"]


def test_find_operation_handles_method_case_and_missing():
    assert find_operation(CONTRACT, "GET", "/catalogue") is not None
    assert find_operation(CONTRACT, "delete", "/catalogue") is None
    assert find_operation(CONTRACT, "get", "/unknown/path") is None
    assert find_operation(CONTRACT, "", "/catalogue") is None


def test_documented_status_codes():
    assert documented_status_codes(CONTRACT, "get", "/catalogue") == frozenset({200, 500})
    assert documented_status_codes(CONTRACT, "get", "/catalogue/1") == frozenset({200, 404})
    assert documented_status_codes(CONTRACT, "post", "/catalogue/1") == frozenset({201, 400})
    assert documented_status_codes(CONTRACT, "get", "/missing") == frozenset()


def test_operation_fragment_contains_parameters_and_responses():
    fragment = operation_fragment(CONTRACT, "GET", "/catalogue/7")
    assert "GET /catalogue/{id}" in fragment
    assert "parameters" in fragment
    assert "id" in fragment
    assert "404" in fragment
    assert len(fragment) <= 1500


def test_operation_fragment_without_match_explains_clearly():
    fragment = operation_fragment(CONTRACT, "GET", "/unknown/route")
    assert "не описывает" in fragment


def test_operation_fragment_is_truncated_to_max_chars():
    fragment = operation_fragment(CONTRACT, "GET", "/catalogue/7", max_chars=60)
    assert len(fragment) <= 60 + len("\n…(обрезано)")
    assert fragment.endswith("…(обрезано)")


def test_render_contract_for_generation_lists_operations():
    rendered = render_contract_for_generation(CONTRACT)
    assert "GET /catalogue" in rendered
    assert "GET /catalogue/{id}" in rendered
    assert "POST /catalogue/{id}" in rendered
    assert "-> 200" in rendered
    # The full file is not in the prompt: schema details are condensed.
    assert "requestBody" not in rendered


def test_generation_context_sorts_mixed_status_keys_without_losing_responses():
    contract = {"info": {}, "paths": {"/items": {"get": {"responses": {
        "404": {"description": "Missing"}, 200: {"description": "OK"},
        "201": {"description": "Created"}, "default": {"description": "Other"},
    }}}}}
    rendered = render_contract_for_generation(contract)
    assert "GET /items -> 200 201 404 default" in rendered


def test_generation_context_supports_default_and_status_ranges():
    contract = {"info": {}, "paths": {"/items": {"get": {"responses": {
        "default": {}, "2XX": {}, "404": {},
    }}}}}
    assert "GET /items -> 2XX 404 default" in render_contract_for_generation(contract)
