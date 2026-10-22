"""Contract fragments for prompts: matching and rendering, no network."""

import copy

import pytest

from prototype.generator.context import (
    detect_operation_dependencies,
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
    """Render/summarize paths are pure: external refs stay explicit markers.

    Fetching happens once at load time (test_external_refs); a contract dict
    that bypasses the loader must never trigger a download here.
    """
    from prototype.parser import contract as contract_module
    monkeypatch.setattr(contract_module, "_download_contract_bytes",
                        lambda *a, **k: pytest.fail("No network while summarizing"))
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


def test_openapi_31_discriminator_is_preserved_in_context():
    contract = {"openapi": "3.1.0", "info": {}, "paths": {"/pets": {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": {
            "oneOf": [{"type": "object", "properties": {"meows": {"type": "boolean"}}},
                      {"type": "object", "properties": {"barks": {"type": "boolean"}}}],
            "discriminator": {"propertyName": "petType"}}}}}}}}}}
    text = render_contract_for_generation(contract)
    assert 'discriminator={"propertyName":"petType"}' in text


def test_summary_operation_description_covers_all_tz_4_1_2_4_elements():
    """Checklist for TZ v3 4.1.2.4: one fixture exercises every required element
    of the operation description in the summary; one assertion group per element."""
    contract = {
        "openapi": "3.1.0",
        "info": {"title": "Things", "version": "1"},
        "paths": {
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "parameters": [
                        {"name": "trace", "in": "header", "required": False,
                         "schema": {"type": "string", "pattern": "^t-[0-9]+$"}},
                    ],
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"type": "object", "required": ["name"], "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 50,
                                      "enum": ["chair", "desk"], "example": "chair"},
                            "qty": {"type": "integer", "format": "int32",
                                     "minimum": 1, "maximum": 10}}},
                        "example": {"name": "chair", "qty": 2}}}},
                    "responses": {
                        "201": {"description": "Created", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Thing"}}}},
                        "400": {"description": "Bad request"},
                        "default": {"description": "Other"},
                    },
                },
            },
            "/things/{id}": {
                "get": {
                    "operationId": "getThing",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True,
                         "schema": {"type": "string", "format": "uuid"}},
                        {"name": "verbose", "in": "query", "required": False,
                         "schema": {"type": "boolean"}},
                    ],
                    "responses": {
                        "200": {"description": "OK", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Thing"},
                            "example": {"id": "00000000-0000-0000-0000-000000000000",
                                        "name": "chair"}}}},
                    },
                },
            },
        },
        "components": {"schemas": {"Thing": {
            "type": "object", "required": ["id", "name"],
            "properties": {"id": {"type": "string", "format": "uuid"},
                           "name": {"type": "string"}}}}},
    }
    text = render_contract_for_generation(contract)
    # 1. Method and path.
    assert "POST /things" in text
    assert "GET /things/{id}" in text
    # 2. Parameters (path, query, header) with types and formats.
    assert "id (path, required) string (format=\"uuid\")" in text
    assert "verbose (query, optional) boolean" in text
    assert "trace (header, optional) string (pattern=" in text
    # 3. Constraints: min/max, pattern, enum.
    assert "minimum=1" in text and "maximum=10" in text
    assert "minLength=1" in text and "maxLength=50" in text
    assert 'enum=["chair","desk"]' in text
    assert "format=\"int32\"" in text
    # 4. Required request body fields.
    assert "requestBody (required):" in text
    assert "name*:" in text
    # 5. Documented response codes and response schemas.
    assert "POST /things -> 201 400 default" in text
    assert "id*:string (format=\"uuid\")" in text
    # 6. Examples.
    assert "example=" in text
    assert '"name":"chair"' in text
    assert "00000000-0000-0000-0000-000000000000" in text
    # 7. Inter-operation links (identifier created by one, used by another).
    assert "Связи между операциями" in text
    assert "- POST /things (creates id) -> GET /things/{id} (uses path: id) [inferred]" in text
    # The per-operation description also carries the dependency lines.
    get_fragment = operation_fragment(contract, "GET", "/things/1")
    assert "depends on: POST /things (id — 201) [inferred]" in get_fragment


ITEMS_CONTRACT = {
    "openapi": "3.1.0",
    "info": {"title": "Items", "version": "1"},
    "paths": {
        "/items": {
            "post": {
                "operationId": "createItem",
                "responses": {"201": {"description": "Created", "content": {"application/json": {
                    "schema": {"type": "object", "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string"}}}}}}},
            },
        },
        "/items/{id}": {
            "get": {
                "operationId": "getItem",
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
}


def test_dependency_detection_pairs_created_identifier_with_path_parameter():
    assert detect_operation_dependencies(ITEMS_CONTRACT) == [{
        "consumer": "GET /items/{id}",
        "producer": "POST /items",
        "identifiers": ("id",),
        "codes": {"id": 201},
        "kind": "inferred",
    }]


def test_dependency_detection_does_not_pair_across_resources():
    contract = {"openapi": "3.1.0", "info": {}, "paths": {
        "/orders": {"post": {"responses": {"201": {"content": {"application/json": {
            "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}}}}}},
        "/items": {"get": {"responses": {"200": {"description": "OK"}}}},
        "/items/{id}": {"get": {"responses": {"200": {"description": "OK"}}}},
    }}
    # POST /orders also creates an "id", but that belongs to another resource.
    assert detect_operation_dependencies(contract) == []


def test_dependency_detection_reads_array_item_fields():
    contract = {"openapi": "3.1.0", "info": {}, "paths": {
        "/nodes": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}}}}}}}}}},
        "/nodes/{id}": {"get": {"responses": {"200": {"description": "OK"}}}},
    }}
    assert [dep["producer"] for dep in detect_operation_dependencies(contract)] == ["GET /nodes"]


def test_dependency_detection_marks_documented_links_and_avoids_duplicates():
    contract = copy.deepcopy(ITEMS_CONTRACT)
    contract["paths"]["/items"]["post"]["responses"]["201"]["links"] = {
        "getItem": {"operationId": "getItem", "parameters": {"id": "$response.body#/id"}}}
    assert detect_operation_dependencies(contract) == [{
        "consumer": "GET /items/{id}",
        "producer": "POST /items",
        "identifiers": ("id",),
        "codes": {"id": 201},
        "kind": "documented",
    }]


def test_dependency_detection_resolves_operation_ref():
    producer_response = {"links": {"open": {
        "operationRef": "#/paths/~1sessions~1{id}/get",
        "parameters": {"id": "$response.body#/token"}}}}
    contract = {
        "openapi": "3.1.0", "info": {},
        "paths": {
            "/sessions": {"post": {"responses": {"201": producer_response}}},
            "/sessions/{id}": {"get": {"responses": {"200": {"description": "OK"}}}},
        },
    }
    assert detect_operation_dependencies(contract) == [{
        "consumer": "GET /sessions/{id}",
        "producer": "POST /sessions",
        "identifiers": ("id",),
        "codes": {},
        "kind": "documented",
    }]


def test_dependency_detection_limits_links_per_consumer():
    paths = {route: {"post": {"responses": {"201": {"content": {"application/json": {
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}}}}}}
        for route in ("/items", *(f"/items/v{index}" for index in range(1, 10)))}
    paths["/items/{id}"] = {"get": {"responses": {"200": {"description": "OK"}}}}
    deps = detect_operation_dependencies({"info": {}, "paths": paths})
    assert len(deps) == 8
    assert all(dep["consumer"] == "GET /items/{id}" for dep in deps)


def test_render_contract_for_generation_lists_dependencies():
    text = render_contract_for_generation(ITEMS_CONTRACT)
    assert "Связи между операциями" in text
    assert "- POST /items (creates id) -> GET /items/{id} (uses path: id) [inferred]" in text


def test_render_contract_for_generation_lists_documented_link():
    contract = copy.deepcopy(ITEMS_CONTRACT)
    contract["paths"]["/items"]["post"]["responses"]["201"]["links"] = {
        "getItem": {"operationId": "getItem", "parameters": {"id": "$response.body#/id"}}}
    text = render_contract_for_generation(contract)
    assert "- POST /items -> GET /items/{id} (uses id) [documented link]" in text


def test_operation_fragment_contains_dependency_lines():
    consumer = operation_fragment(ITEMS_CONTRACT, "get", "/items/7")
    assert "depends on: POST /items (id — 201) [inferred]" in consumer
    producer = operation_fragment(ITEMS_CONTRACT, "post", "/items")
    assert "creates: id — used by GET /items/{id} [inferred]" in producer
def test_catalogue_generation_contains_all_required_product_fields():
    from pathlib import Path
    contract = load_contract(Path(__file__).resolve().parents[4] /
                             "benchmark/catalogue/catalogue.swagger.json")
    text = render_contract_for_generation(contract)
    assert len(text) < 8000
    for field in ("id", "name", "description", "imageUrl", "price", "count", "tag"):
        assert field + "*:" in text
    assert "count*:integer" in text and "price*:number" in text
    assert "imageUrl*:array[string]" in text and "tag*:array[string]" in text
