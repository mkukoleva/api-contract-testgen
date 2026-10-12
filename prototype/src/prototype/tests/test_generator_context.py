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
    # The full file is not in the prompt; relevant schema details are included.
    assert "requestBody" in rendered


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


def test_generation_context_resolves_refs_and_preserves_constraints_examples_and_links():
    contract = {
        "openapi": "3.1.0", "info": {"title": "Items", "version": "2"},
        "paths": {"/items": {
            "parameters": [{"$ref": "#/components/parameters/Limit"}],
            "post": {"operationId": "createItem", "parameters": [{"name": "trace", "in": "header",
                "schema": {"type": "string", "pattern": "^abc$"}, "example": "abc"}],
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Item"}, "example": {"name": "chair"}}}},
                "responses": {"201": {"description": "Created", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Item"}}}, "links": {"getItem": {
                        "operationId": "getItem", "parameters": {"id": "$response.body#/id"}}}}}},
            "get": {"parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 5}}],
                    "responses": {"200": {"description": "List"}}}},
            "/items/{id}": {"get": {"operationId": "getItem", "responses": {"200": {"description": "OK"}}}}},
        "components": {"parameters": {"Limit": {"name": "limit", "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": 10}, "example": 3}},
            "schemas": {"Item": {"type": "object", "required": ["name"], "properties": {
                "id": {"type": "string", "format": "uuid"},
                "name": {"type": "string", "minLength": 1, "maxLength": 50, "enum": ["chair", "desk"]}}}},
            "x-unused": "MUST_NOT_REACH_LLM"},
    }
    text = render_contract_for_generation(contract)
    for expected in ("requestBody", "name*", "minimum=1", "maximum=10", "minLength=1", "maxLength=50",
                     "pattern=", "enum=", "uuid", "example", "chair", "getItem", "$response.body#/id"):
        assert expected in text, (expected, text)
    assert "MUST_NOT_REACH_LLM" not in text
    get_fragment = operation_fragment(contract, "GET", "/items")
    assert "maximum=5" in get_fragment and "maximum=10" not in get_fragment
    assert "minimum=1" not in get_fragment  # operation-level parameter replaces the shared one


def test_swagger_schema_body_and_response_are_in_generation_context():
    contract = {"swagger": "2.0", "info": {}, "paths": {"/items": {"post": {
        "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Item"}}],
        "responses": {200: {"description": "OK", "schema": {"$ref": "#/definitions/Item"},
                             "examples": {"application/json": {"name": "example-item"}}}}}}},
        "definitions": {"Item": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}}
    text = render_contract_for_generation(contract)
    assert "name*" in text and "example-item" in text


def test_recursive_and_external_refs_are_explicit_and_never_fetched(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("No network while summarizing"))
    contract = {"info": {}, "paths": {"/nodes": {"get": {"responses": {"200": {"content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}},
        "400": {"$ref": "https://example.invalid/errors.yaml#/Bad"}}}}},
        "components": {"schemas": {"Node": {"type": "object", "properties": {
            "child": {"$ref": "#/components/schemas/Node"}}}}}}
    text = render_contract_for_generation(contract)
    assert "#/components/schemas/Node" in text
    assert "https://example.invalid/errors.yaml#/Bad" in text
    assert len(text) < 8000


def test_generation_summary_budget_never_silently_drops_operations():
    contract = {"info": {}, "paths": {f"/items/{n}": {"get": {"summary": "long description " * 100,
        "responses": {"200": {"description": "OK"}}}} for n in range(12)}}
    text = render_contract_for_generation(contract, max_chars=800)
    assert len(text) <= 800
    for n in range(12):
        assert f"GET /items/{n} ->" in text
    assert "обрезано" in text


def test_impossible_summary_budget_is_rejected_before_generation_call():
    from prototype.generator.generate import generate_suite
    from prototype.generator.contracts import GenerationSettings
    contract = {"info": {}, "paths": {"/" + "long-route" * 1000: {"get": {"responses": {"200": {}}}}}}
    class NoModel:
        def invoke(self, *args):
            pytest.fail("No LLM call if even the operation list does not fit")
    result = generate_suite(contract, GenerationSettings(contract_path="unused"), model=NoModel())
    assert result["calls"] == 0 and not result["prepared"]
    assert "summary" in result["errors"][0]


def test_static_operation_takes_priority_over_parameterized_route():
    contract = {"info": {}, "paths": {
        "/catalogue/{id}": {"get": {"responses": {"200": {"schema": {"type": "string"}}}}},
        "/catalogue/size": {"get": {"responses": {"200": {"schema": {"type": "object",
            "required": ["size"], "properties": {"size": {"type": "integer"}}}}}}}}}
    fragment = operation_fragment(contract, "GET", "http://catalogue:8080/catalogue/size")
    assert fragment.startswith("GET /catalogue/size\n")
    assert "size*:integer" in fragment
    assert "size*:integer" in render_contract_for_generation(contract)


def test_yaml_date_example_is_usable_in_summary(tmp_path):
    path = tmp_path / "dates.yaml"
    path.write_text("""openapi: 3.0.3
info: {title: Dates, version: '1'}
paths:
  /date:
    get:
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema: {type: string, format: date, example: 2026-10-12}
""", encoding="utf-8")
    assert "2026-10-12" in render_contract_for_generation(load_contract(path))


@pytest.mark.parametrize("schema,expected", [
    ({"type": ["object", "null"], "required": ["id"], "properties": {"id": {"type": "integer"}}}, "id*:integer"),
    ({"type": ["array", "null"], "items": {"type": "string", "minLength": 1}}, "array[string (minLength=1)]"),
])
def test_openapi_31_nullable_types_preserve_structure(schema, expected):
    contract = {"openapi": "3.1.0", "info": {}, "paths": {"/items": {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": schema}}}}}}}}
    text = render_contract_for_generation(contract)
    assert expected in text and "null" in text


def test_openapi_31_false_schema_is_preserved_in_context():
    contract = {"openapi": "3.1.0", "info": {}, "paths": {"/items": {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": False}}}}}}}}
    assert "application/json:false" in render_contract_for_generation(contract)
