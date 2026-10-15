"""Compact, relevant views of an API contract for the LLM prompts.

The full OpenAPI document never reaches the model: the generation prompt gets
a condensed operation list, and a self-repair payload gets only the fragment of
the operation whose test failed (ADR 0004). Matching of observed request strings
to contract templates tolerates concrete segment values ("/catalogue/2" matches
"/catalogue/{id}").
"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

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


MAX_DEPENDENCIES_PER_OPERATION = 8
MAX_PRODUCER_DEPENDENCY_LINES = 4
_PATH_PARAMETER_RE = re.compile(r"\{([^}/]+)\}")


def _base_resource(route: str) -> str:
    """First static segment of a route; '' when the route has none."""
    for segment in route.strip("/").split("/"):
        if segment and not segment.startswith("{"):
            return segment
    return ""


def _path_parameters(route: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_PATH_PARAMETER_RE.findall(route)))


def _is_success_code(code: Any) -> bool:
    if isinstance(code, str):
        if code.upper() == "2XX":
            return True
        if not code.isdigit():
            return False
        code = int(code)
    return isinstance(code, int) and not isinstance(code, bool) and 200 <= code <= 299


def _success_code_number(code: Any) -> int:
    if isinstance(code, str) and code.isdigit():
        return int(code)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return 200


def _fields_of_schema(schema: Any, depth: int = 0) -> set[str]:
    """Property names at the top of the schema and of direct array items.

    allOf/oneOf/anyOf members are followed one level so composed schemas
    still expose their identifiers; recursion is bounded.
    """
    if depth > 3 or not isinstance(schema, dict):
        return set()
    fields: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        fields.update(name for name in properties if isinstance(name, str))
    items = schema.get("items")
    if isinstance(items, dict):
        sub = items.get("properties")
        if isinstance(sub, dict):
            fields.update(name for name in sub if isinstance(name, str))
    for key in ("allOf", "oneOf", "anyOf"):
        members = schema.get(key)
        if isinstance(members, list):
            for member in members:
                fields |= _fields_of_schema(member, depth + 1)
    return fields


def _response_schema(response: Any) -> Any:
    if not isinstance(response, dict):
        return None
    if "schema" in response:  # Swagger 2 responses
        return response["schema"]
    content = response.get("content")
    if isinstance(content, dict):
        for media in content.values():
            if isinstance(media, dict) and "schema" in media:
                return media["schema"]
    return None


def _collect_operations(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-operation facts for dependency detection of a normalized contract:
    route, consumed path parameters, produced 2xx response fields and links."""
    operations: dict[str, dict[str, Any]] = {}
    paths = contract.get("paths", {})
    if not isinstance(paths, dict):
        return operations
    for route, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            produces: dict[str, int] = {}
            links: list[dict[str, Any]] = []
            responses = operation.get("responses")
            if isinstance(responses, dict):
                for code, response in responses.items():
                    if not isinstance(response, dict):
                        continue
                    if _is_success_code(code):
                        numeric = _success_code_number(code)
                        for field in _fields_of_schema(_response_schema(response)):
                            current = produces.get(field)
                            if current is None or numeric < current:
                                produces[field] = numeric
                    link_collection = response.get("links")
                    if isinstance(link_collection, dict):
                        for link in link_collection.values():
                            if not isinstance(link, dict):
                                continue
                            parameters = link.get("parameters")
                            identifiers = (tuple(name for name in parameters if isinstance(name, str))
                                           if isinstance(parameters, dict) else ())
                            operation_id = link.get("operationId")
                            operation_ref = link.get("operationRef")
                            links.append({
                                "operationId": operation_id if isinstance(operation_id, str) else None,
                                "operationRef": operation_ref if isinstance(operation_ref, str) else None,
                                "identifiers": identifiers,
                            })
            operations[f"{method.upper()} {route}"] = {
                "route": route,
                "consumes": _path_parameters(route),
                "produces": produces,
                "operation_id": operation.get("operationId") if isinstance(operation.get("operationId"), str) else None,
                "links": links,
            }
    return operations


def _resolve_operation_ref(contract: dict[str, Any], ref: Any) -> str | None:
    """Resolve a '#/paths/...' operationRef to a 'METHOD /route' label."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    # Operation pointer is always paths -> route key -> method: the route key
    # is a single token with its slashes escaped (~1).
    tokens = [token.replace("~1", "/").replace("~0", "~")
              for token in unquote(ref[2:]).split("/")]
    if len(tokens) != 3 or tokens[0] != "paths" or not tokens[1].startswith("/"):
        return None
    method = tokens[2].lower()
    if method not in HTTP_METHODS:
        return None
    route = tokens[1]
    paths = contract.get("paths", {})
    path_item = paths.get(route) if isinstance(paths, dict) else None
    if not isinstance(path_item, dict):
        return None
    operation = next((value for key, value in path_item.items()
                      if isinstance(key, str) and key.lower() == method), None)
    return f"{method.upper()} {route}" if isinstance(operation, dict) else None


def _detect_operation_dependencies(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Inter-operation identifier links of a normalized contract.

    A consumer is an operation whose route uses a path parameter '{name}';
    a producer is an operation whose 2xx response documents a field with
    that name. Name-based pairs are only formed within one base resource
    (the first static path segment) and marked "inferred" — they are a
    documented-contract hypothesis, not a guarantee. OpenAPI links are
    explicit and marked "documented"; when both agree on a pair, the link
    is reported once as "documented". Deterministic order, bounded per
    consumer so the summary cannot balloon.
    """
    operations = _collect_operations(contract)
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    def entry_for(consumer: str, producer: str) -> dict[str, Any]:
        return merged.setdefault((consumer, producer),
                                 {"identifiers": set(), "codes": {}, "documented": False})

    for consumer_label, consumer in operations.items():
        base = _base_resource(consumer["route"])
        for producer_label, producer in operations.items():
            if producer_label == consumer_label or _base_resource(producer["route"]) != base:
                continue
            shared = sorted(set(consumer["consumes"]) & set(producer["produces"]))
            if not shared:
                continue
            entry = entry_for(consumer_label, producer_label)
            for field in shared:
                entry["identifiers"].add(field)
                entry["codes"].setdefault(field, producer["produces"][field])

    by_operation_id = {op["operation_id"].lower(): label
                       for label, op in operations.items() if op["operation_id"]}
    for producer_label, producer in operations.items():
        for link in producer["links"]:
            target_id = link["operationId"]
            target_label = by_operation_id.get(target_id.lower()) if target_id else None
            if target_label is None:
                target_label = _resolve_operation_ref(contract, link["operationRef"])
            if target_label is None or target_label == producer_label:
                continue
            entry = entry_for(target_label, producer_label)
            entry["documented"] = True
            entry["identifiers"].update(link["identifiers"])

    by_consumer: dict[str, list[dict[str, Any]]] = {}
    for (consumer, producer), entry in merged.items():
        by_consumer.setdefault(consumer, []).append({
            "consumer": consumer,
            "producer": producer,
            "identifiers": tuple(sorted(entry["identifiers"])),
            "codes": {field: entry["codes"][field] for field in sorted(entry["codes"])},
            "kind": "documented" if entry["documented"] else "inferred",
        })
    result: list[dict[str, Any]] = []
    for consumer in sorted(by_consumer):
        items = sorted(by_consumer[consumer],
                       key=lambda item: (item["kind"] != "documented", item["producer"]))
        result.extend(items[:MAX_DEPENDENCIES_PER_OPERATION])
    return result


def detect_operation_dependencies(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Detected and documented inter-operation links (see _detect_operation_dependencies)."""
    return _detect_operation_dependencies(normalize_contract(contract))


def _dependency_section(dependencies: list[dict[str, Any]]) -> str:
    lines = []
    for dep in dependencies:
        fields = ", ".join(dep["identifiers"])
        if dep["kind"] == "documented":
            suffix = f" (uses {fields})" if fields else ""
            lines.append(f"- {dep['producer']} -> {dep['consumer']}{suffix} [documented link]")
        else:
            lines.append(f"- {dep['producer']} (creates {fields}) -> {dep['consumer']} "
                         f"(uses path: {fields}) [inferred]")
    return "\n\nСвязи между операциями:\n" + "\n".join(lines)


def _dependency_lines(label: str, dependencies: list[dict[str, Any]]) -> list[str]:
    """Short per-operation lines: consumed and produced identifiers."""
    consumer_lines: list[str] = []
    producer_lines: list[str] = []
    for dep in dependencies:
        if dep["consumer"] == label:
            detail = ", ".join(f"{field} — {dep['codes'][field]}" if field in dep["codes"] else field
                               for field in dep["identifiers"])
            consumer_lines.append(f"depends on: {dep['producer']} ({detail}) [{dep['kind']}]")
        elif dep["producer"] == label:
            producer_lines.append(f"creates: {', '.join(dep['identifiers'])} — used by "
                                  f"{dep['consumer']} [{dep['kind']}]")
    return (consumer_lines[:MAX_DEPENDENCIES_PER_OPERATION]
            + producer_lines[:MAX_PRODUCER_DEPENDENCY_LINES])


def operation_fragment(
    contract: dict[str, Any],
    method: str,
    observed_path: str,
    *,
    max_chars: int = 1500,
) -> str:
    """Render the relevant contract fragment for one failed test."""
    return _operation_fragment(normalize_contract(contract), method, observed_path, max_chars=max_chars)


def _operation_fragment(contract, method, observed_path, *, max_chars,
                        dependencies: list[dict[str, Any]] | None = None):
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

    if dependencies is None:
        dependencies = _detect_operation_dependencies(contract)
    lines.extend(_dependency_lines(f"{method.upper()} {contract_path}", dependencies))

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
    dependencies = _detect_operation_dependencies(contract)
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
            details.append(_operation_fragment(contract, method, route, max_chars=0,
                                               dependencies=dependencies))

    header = "\n".join(lines)
    middle = _dependency_section(dependencies) if dependencies else ""
    text = header + middle + "\n\nОписание операций (* — обязательное поле):\n" + "\n\n".join(details)
    if max_chars > 0 and len(text) > max_chars:
        marker = "\n…(обрезано: подробности операций)"
        if len(header) + len(marker) > max_chars:
            raise ContractInputError("Список операций не помещается в лимит summary; увеличьте лимит.")
        return text[:max_chars - len(marker)].rstrip() + marker
    return text


SCHEMATHESIS_DIGEST_MAX_CHARS = 1500
SCHEMATHESIS_DIGEST_MAX_FAILURES = 10


def schemathesis_digest(
    result: Any,
    *,
    max_chars: int = SCHEMATHESIS_DIGEST_MAX_CHARS,
    max_failures: int = SCHEMATHESIS_DIGEST_MAX_FAILURES,
) -> str:
    """Token-thrifty digest of a Schemathesis run for the generation prompt.

    Keeps the run status, scenario counts, a bounded list of unique failure
    titles and the duration. Code samples, request details and the raw
    payload never enter the model context (TZ v3 4.1.2.3: deterministic tool
    results enrich the context; the contract stays the source of truth).
    """
    if not isinstance(result, dict):
        return ""
    status = str(result.get("status", "unknown"))
    duration = result.get("duration_seconds")
    header = f"Schemathesis: {status}"
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        header += f" (время: {duration}с)"
    lines = [header]
    counts = result.get("summary")
    if isinstance(counts, dict):
        rendered = ", ".join(
            f"{key} {counts[key]}" for key in ("total", "passed", "failed", "errors", "skipped")
            if isinstance(counts.get(key), int) and not isinstance(counts.get(key), bool))
        if rendered:
            lines.append("сценарии: " + rendered)
    failures: list[dict] = []
    seen: set[tuple] = set()
    raw_failures = result.get("failures")
    if isinstance(raw_failures, list):
        for failure in raw_failures:
            if not isinstance(failure, dict):
                continue
            key = (failure.get("operation"), failure.get("check"), failure.get("title"))
            if key in seen:
                continue
            seen.add(key)
            failures.append(failure)
    if failures:
        lines.append(f"неудачные проверки ({len(failures)}):")
        for failure in failures[:max_failures]:
            title = " ".join(str(failure.get("title") or "").split())[:150]
            lines.append(f"  - {failure.get('operation') or '?'}: "
                         f"{failure.get('check') or '?'} — {title}")
        if len(failures) > max_failures:
            lines.append(f"  … и ещё {len(failures) - max_failures} (подробности в отчёте)")
    text = "\n".join(lines)
    if max_chars > 0 and len(text) > max_chars:
        marker = "\n(сводка обрезана)"
        text = text[:max(0, max_chars - len(marker))].rstrip() + marker[:max_chars]
    return text
