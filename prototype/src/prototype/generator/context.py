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

from ..parser.contract import HTTP_METHODS, _load_contract


def load_contract(path: Path | str) -> dict[str, Any]:
    """Read a JSON/YAML OpenAPI or Swagger contract as a dict."""
    return _load_contract(Path(path))


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
    method = method.lower().strip()
    if not method:
        return None
    paths = contract.get("paths", {})
    if not isinstance(paths, dict):
        return None
    for contract_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        if method not in path_item:
            continue
        if match_contract_path(contract_path, observed_path):
            operation = path_item[method]
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
    if depth > 2:
        return "..."
    if not isinstance(schema, dict):
        return str(schema)[:40]
    if "oneOf" in schema or "anyOf" in schema:
        alternatives = schema.get("oneOf") or schema.get("anyOf")
        return "|".join(_brief_schema(item, depth + 1) for item in alternatives[:3])
    if "allOf" in schema:
        return "+".join(_brief_schema(item, depth + 1) for item in schema["allOf"][:3])
    if "type" in schema:
        kind = schema["type"]
        if kind == "array":
            item = schema.get("items", {})
            return f"array[{_brief_schema(item, depth + 1)}]"
        if kind == "object":
            properties = schema.get("properties", {})
            required = set(schema.get("required", []) or [])
            entries = [
                f"{name}{'*' if name in required else ''}:{_brief_schema(prop, depth + 1)}"
                for name, prop in list(properties.items())[:8]
            ]
            extra = "…" if len(properties) > 8 else ""
            return "{" + ", ".join(entries) + extra + "}"
        extras = []
        if "format" in schema:
            extras.append(str(schema["format"]))
        if "enum" in schema:
            extras.append("=" + ",".join(str(item) for item in schema["enum"][:4]))
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"{kind}{suffix}"
    if "$ref" in schema:
        return str(schema["$ref"]).split("/")[-1]
    return "..."


def _content_brief(content: dict[str, Any]) -> str:
    chunks = []
    for media_type, media in list(content.items())[:3]:
        parts = [str(media_type)]
        schema = media.get("schema") if isinstance(media, dict) else None
        if schema:
            parts.append(_brief_schema(schema))
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
    found = find_operation(contract, method, observed_path)
    if found is None:
        return (
            f"Контракт не описывает операцию {method.upper()} {observed_path}.\n"
            "Сверь используемый метод и путь с разделом paths контракта."
        )
    contract_path, operation = found
    lines = [f"{method.upper()} {contract_path}"]
    summary = operation.get("summary")
    description = operation.get("description")
    if summary:
        lines.append(f"summary: {summary}")
    if description and description != summary:
        lines.append(f"description: {description}")

    parameters = operation.get("parameters", [])
    if isinstance(parameters, list) and parameters:
        param_lines = []
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            name = parameter.get("name", "?")
            location = parameter.get("in", "?")
            required = "required" if parameter.get("required") else "optional"
            schema = parameter.get("schema", {})
            brief = _brief_schema(schema) if isinstance(schema, dict) else str(schema)[:40]
            param_lines.append(f"  - {name} ({location}, {required}) {brief}")
        lines.append("parameters:")
        lines.extend(param_lines)

    request_body = operation.get("requestBody", {})
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            lines.append(f"requestBody: {_content_brief(content)}")

    responses = operation.get("responses", {})
    if isinstance(responses, dict) and responses:
        lines.append("responses:")
        for code, response in responses.items():
            if not isinstance(response, dict):
                lines.append(f"  - {code}")
                continue
            text = response.get("description", "")
            content = response.get("content")
            brief = _content_brief(content) if isinstance(content, dict) else ""
            suffix = f" {text}" if text else ""
            suffix += f" {brief}" if brief else ""
            lines.append(f"  - {code}{suffix}")

    fragment = "\n".join(lines)
    if max_chars > 0 and len(fragment) > max_chars:
        fragment = fragment[:max_chars].rstrip() + "\n…(обрезано)"
    return fragment


def render_contract_for_generation(
    contract: dict[str, Any], *, max_chars: int = 8000
) -> str:
    """Condensed whole-contract view for the generation prompt."""
    info = contract.get("info", {}) if isinstance(contract, dict) else {}
    lines = [
        f"Название API: {info.get('title', '')}",
        f"Версия: {info.get('version', '')}",
        "Операции:",
    ]
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
            summary = operation.get("summary")
            if isinstance(summary, str) and summary:
                lines.append(f"    {summary}")

    text = "\n".join(lines)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…(обрезано)"
    return text
