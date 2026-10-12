"""Compact, relevant views of an API contract for the LLM prompts.

The full OpenAPI document never reaches the model: the generation prompt gets
a condensed operation list, and a self-repair payload gets only the fragment of
the operation whose test failed (ADR 0004). Matching of observed request strings
to contract templates tolerates concrete segment values ("/catalogue/2" matches
"/catalogue/{id}").
"""

import json
from pathlib import Path
from typing import Any

from ..parser.contract import HTTP_METHODS, ContractInputError, _load_contract, normalize_contract


def load_contract(path: Path | str) -> dict[str, Any]:
    """Read a JSON/YAML OpenAPI or Swagger contract as a dict."""
    return _load_contract(path)


def match_contract_path(contract_path: str, observed: str) -> bool:
    """Match an observed request path against a contract template path.

    Contract segments in curly braces match any single concrete segment;
    other segments must be equal. An absolute observed URL is normalised to
    its path first.
    """
    observed = _to_path(observed)
    contract_parts = contract_path.strip("/").split("/")
    observed_parts = observed.strip("/").split("/")
    if contract_parts == [""]:
        contract_parts = []
    if observed_parts == [""]:
        observed_parts = []
    if len(contract_parts) != len(observed_parts):
        return False
    for template, concrete in zip(contract_parts, observed_parts):
        if template.startswith("{") and template.endswith("}"):
            continue
        if template != concrete:
            return False
    return True


def _to_path(text: str) -> str:
    """Return the path part of an URL (or the text itself), trailing slash off."""
    text = text.strip().rstrip("/") or "/"
    if "://" not in text:
        return text.split("?")[0]
    try:
        from urllib.parse import urlparse

        parsed = urlparse(text)
        return (parsed.path.rstrip("/") or "/")
    except ValueError:
        return text.split("?")[0]


def find_operation(
    contract: dict[str, Any], method: str, observed_path: str
) -> tuple[str, dict[str, Any]] | None:
    """Return (contract path, operation dict) for method + observed path.

    ``method`` may be lowercase; the first matching template wins. Returns
    None when the method is unknown or no path matches.
    """
    return _find_operation(normalize_contract(contract), method, observed_path)


def _find_operation(contract, method, observed_path):
    method = method.lower().strip()
    if not method:
        return None
    paths = contract.get("paths", {})
    if not isinstance(paths, dict):
        return None
    observed = _to_path(observed_path)
    # Static routes such as /catalogue/size win over /catalogue/{id},
    # regardless of their order in the document.
    for contract_path, path_item in sorted(paths.items(), key=lambda item: item[0] != observed):
        if not isinstance(path_item, dict):
            continue
        key = next((key for key in path_item if isinstance(key, str) and key.lower() == method), None)
        if key is None:
            continue
        if match_contract_path(contract_path, observed_path):
            operation = path_item[key]
            if isinstance(operation, dict):
                return contract_path, operation
    return None


def documented_status_codes(
    contract: dict[str, Any], method: str, observed_path: str
) -> frozenset[int]:
    """Documented HTTP status codes of the matching operation.

    Responses declared as "default" are ignored; string keys that are not
    three-digit codes are skipped. Returns an empty set when the operation
    cannot be matched.
    """
    found = find_operation(contract, method, observed_path)
    if found is None:
        return frozenset()
    _, operation = found
    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        return frozenset()
    codes = set()
    for key in responses:
        if key == "default":
            continue
        try:
            code = int(key)
        except (TypeError, ValueError):
            continue
        if 100 <= code <= 599:
            codes.add(code)
    return frozenset(codes)


def _brief_schema(schema: dict[str, Any], depth: int = 0) -> str:
    """Short, token-thrifty rendering of a JSON Schema."""
    if depth > 5:
        return "…(схема обрезана)"
    if isinstance(schema, bool):
        return json.dumps(schema)
    if not isinstance(schema, dict):
        return str(schema)[:40]
    kind = schema.get("type", "object" if "properties" in schema else "schema")
    # OpenAPI 3.1 uses type unions, e.g. [object, null], rather than only nullable.
    shapes = []
    for item in kind if isinstance(kind, list) else [kind]:
        if item == "array":
            shapes.append(f"array[{_brief_schema(schema.get('items', {}), depth + 1)}]")
        elif item == "object":
            properties = schema.get("properties", {})
            required = set(schema.get("required", []) or [])
            entries = [f"{name}{'*' if name in required else ''}:{_brief_schema(prop, depth + 1)}"
                       for name, prop in properties.items()] if isinstance(properties, dict) else []
            shapes.append("{" + ", ".join(entries) + "}")
        else:
            shapes.append(str(item))
    text = "|".join(shapes)
    extras = []
    for key in ("$ref", "format", "required", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
                "minProperties", "maxProperties", "enum", "const", "nullable", "default", "example", "examples",
                "readOnly", "writeOnly", "additionalProperties"):
        if key in schema:
            extras.append(f"{key}={json.dumps(schema[key], ensure_ascii=False, separators=(',', ':'))}")
    for key in ("oneOf", "anyOf", "allOf"):
        if isinstance(schema.get(key), list):
            extras.append(key + "=[" + ",".join(_brief_schema(item, depth + 1) for item in schema[key]) + "]")
    return text + (f" ({', '.join(extras)})" if extras else "")


def _examples(node: dict) -> str:
    return " ".join(f"{key}={json.dumps(node[key], ensure_ascii=False, separators=(',', ':'))}"
                    for key in ("example", "examples") if key in node)


def _content_brief(content: dict[str, Any]) -> str:
    chunks = []
    for media_type, media in content.items():
        parts = [str(media_type)]
        schema = media.get("schema") if isinstance(media, dict) else None
        if isinstance(media, dict) and "schema" in media:
            parts.append(_brief_schema(schema))
        if isinstance(media, dict):
            parts.append(_examples(media))
        chunks.append(":".join(parts))
    return " ".join(chunks) if chunks else ""


def operation_fragment(
    contract: dict[str, Any],
    method: str,
    observed_path: str,
    *,
    max_chars: int = 1500,
) -> str:
    """Render the relevant contract fragment for one failed test."""
    return _operation_fragment(normalize_contract(contract), method, observed_path, max_chars=max_chars)


def _operation_fragment(contract, method, observed_path, *, max_chars):
    found = _find_operation(contract, method, observed_path)
    if found is None:
        return (
            f"Контракт не описывает операцию {method.upper()} {observed_path}.\n"
            "Сверь используемый метод и путь с разделом paths контракта."
        )
    contract_path, operation = found
    lines = [f"{method.upper()} {contract_path}"]
    if operation.get("operationId"):
        lines.append(f"operationId: {operation['operationId']}")
    summary = operation.get("summary")
    description = operation.get("description")
    if summary:
        lines.append(f"summary: {summary}")
    if description and description != summary:
        lines.append(f"description: {description}")

    # An operation-level parameter overrides the same (name, in) on the path.
    shared = contract.get("paths", {}).get(contract_path, {}).get("parameters", [])
    parameters_by_key = {}
    for group in (shared, operation.get("parameters", [])):
        if isinstance(group, list):
            for parameter in group:
                if isinstance(parameter, dict):
                    key = (parameter.get("name"), parameter.get("in"), parameter.get("$ref"))
                    parameters_by_key[key] = parameter
    parameters = list(parameters_by_key.values())
    if isinstance(parameters, list) and parameters:
        param_lines = []
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            name = parameter.get("name", "?")
            location = parameter.get("in", "?")
            required = "required" if parameter.get("required") else "optional"
            schema = parameter.get("schema", parameter)  # Swagger 2 non-body parameters
            brief = _brief_schema(schema) if isinstance(schema, dict) else str(schema)[:40]
            param_lines.append(f"  - {name} ({location}, {required}) {brief} {_examples(parameter)}")
        lines.append("parameters:")
        lines.extend(param_lines)

    request_body = operation.get("requestBody", {})
    if isinstance(request_body, dict):
        if "$ref" in request_body:
            lines.append(f"requestBody $ref: {request_body['$ref']} (не раскрыта)")
        content = request_body.get("content")
        if isinstance(content, dict):
            lines.append(f"requestBody ({'required' if request_body.get('required') else 'optional'}): {_content_brief(content)}")

    responses = operation.get("responses", {})
    if isinstance(responses, dict) and responses:
        lines.append("responses:")
        for code, response in responses.items():
            if not isinstance(response, dict):
                lines.append(f"  - {code}")
                continue
            if "$ref" in response:
                lines.append(f"  - {code} $ref: {response['$ref']} (не раскрыта)")
            text = response.get("description", "")
            content = response.get("content")
            brief = _content_brief(content) if isinstance(content, dict) else ""
            if "schema" in response:  # Swagger 2 responses
                brief += " " + _brief_schema(response["schema"])
            suffix = f" {text}" if text else ""
            suffix += f" {brief}" if brief else ""
            suffix += " " + _examples(response)
            lines.append(f"  - {code}{suffix}")
            if isinstance(response.get("links"), dict):
                for name, link in response["links"].items():
                    if isinstance(link, dict):
                        mapping = {key: link[key] for key in ("operationId", "operationRef", "parameters", "requestBody", "$ref") if key in link}
                        lines.append(f"    link {name}: {json.dumps(mapping, ensure_ascii=False, separators=(',', ':'))}")

    fragment = "\n".join(lines)
    if max_chars > 0 and len(fragment) > max_chars:
        marker = "\n…(обрезано)"
        fragment = fragment[:max(0, max_chars - len(marker))].rstrip() + marker[:max_chars]
    return fragment


def render_contract_for_generation(
    contract: dict[str, Any], *, max_chars: int = 8000
) -> str:
    """Condensed whole-contract view for the generation prompt."""
    contract = normalize_contract(contract)
    info = contract.get("info", {}) if isinstance(contract, dict) else {}
    lines = [
        f"Название API: {info.get('title', '')}",
        f"Версия: {info.get('version', '')}",
        "Операции:",
    ]
    details = []
    paths = contract.get("paths", {})
    for route, path_item in paths.items() if isinstance(paths, dict) else []:
        if not isinstance(path_item, dict):
            continue
        for method in path_item:
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue
            statuses = sorted({
                str(code)
                for code in (operation.get("responses") or {})
                if (str(code).isdigit() or str(code) == "default"
                    or (len(str(code)) == 3 and str(code)[0] in "12345"
                        and str(code)[1:] == "XX"))
            }) if isinstance(operation.get("responses"), dict) else []
            suffix = " ".join(str(code) for code in statuses)
            lines.append(f"- {method.upper()} {route}{f' -> {suffix}' if suffix else ''}")
            details.append(_operation_fragment(contract, method, route, max_chars=0))

    header = "\n".join(lines)
    text = header + "\n\nОписание операций (* — обязательное поле):\n" + "\n\n".join(details)
    if max_chars > 0 and len(text) > max_chars:
        marker = "\n…(обрезано: подробности операций)"
        if len(header) + len(marker) > max_chars:
            raise ContractInputError("Список операций не помещается в лимит summary; увеличьте лимит.")
        return text[:max_chars - len(marker)].rstrip() + marker
    return text
