"""Mutation generators for task 2.2.9.

Legacy functions export altered specifications. generate_response_plans() instead
produces operation-scoped plans for changing HTTP response data; it does not run
or apply them. Schema weakening and deleting an actual response field are
separate operations. Unsupported response targets have explicit diagnostics.
"""


from __future__ import annotations

import copy
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator
from urllib.parse import unquote
from uuid import uuid4

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class MutationType(str, Enum):
    """Типы операторов мутации API-контракта (задача 2.2.9)."""

    TYPE_CHANGE = "type_change"
    REMOVE_REQUIRED = "remove_required"
    STATUS_CODE_SWAP = "status_code_swap"


@dataclass(frozen=True)
class Mutant:
    """Описание одного сгенерированного мутанта API-контракта."""

    mutant_id: str
    operator: MutationType
    target_path: str
    description: str
    original_value: Any
    mutated_value: Any
    mutated_contract: dict[str, Any] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать метаданные мутанта в словарь (без тяжёлого тела контракта)."""
        return {
            "mutant_id": self.mutant_id,
            "operator": self.operator.value,
            "target_path": self.target_path,
            "description": self.description,
            "original_value": self.original_value,
            "mutated_value": self.mutated_value,
        }

    def dump_yaml(self) -> str:
        """Экспортировать мутированный контракт в формат YAML."""
        if yaml is None:
            raise RuntimeError("Пакет pyyaml не установлен для экспорта в YAML.")
        return yaml.safe_dump(self.mutated_contract, sort_keys=False, allow_unicode=True)

    def dump_json(self, indent: int = 2) -> str:
        """Экспортировать мутированный контракт в формат JSON."""
        return json.dumps(self.mutated_contract, indent=indent, ensure_ascii=False)

    def save_to_file(self, file_path: str | Path) -> Path:
        """Сохранить мутированный контракт в файл (.yaml/.yml/.json)."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in {".yaml", ".yml"}:
            path.write_text(self.dump_yaml(), encoding="utf-8")
        else:
            path.write_text(self.dump_json(), encoding="utf-8")
        return path


@dataclass
class MutationScoreResult:
    """Результат расчёта Mutation Score."""

    total_mutants: int
    killed_mutants: int
    survived_mutants: int
    mutation_score_percent: float | None
    mutants_by_operator: dict[str, int] = field(default_factory=dict)
    killed_by_operator: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать результат в JSON-совместимый словарь."""
        return asdict(self)


# Таблица несовместимых подмен типов данных
TYPE_REPLACEMENTS: dict[str, str] = {
    "string": "integer",
    "integer": "string",
    "number": "string",
    "boolean": "string",
    "array": "object",
    "object": "string",
}

# Таблица подмены статус-кодов ответов
STATUS_CODE_SWAPS: dict[str, str] = {
    "200": "500",
    "201": "400",
    "202": "500",
    "204": "500",
    "400": "200",
    "401": "200",
    "403": "200",
    "404": "200",
    "409": "200",
    "422": "200",
    "500": "200",
    "502": "200",
    "503": "200",
}


# Schema roots are selected by OpenAPI structure, never by arbitrary key names.
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_SCHEMA_MAPS = ("properties",)
_SCHEMA_CHILDREN = ("items", "additionalProperties")
_COMPOSITIONS = ("allOf", "oneOf", "anyOf", "not")


def json_pointer(parts: tuple[Any, ...] | list[Any]) -> str:
    """RFC 6901 address; unlike dotted paths it handles slash/tilde in names."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _operations(contract: dict[str, Any]) -> Iterator[tuple[str, str, dict, tuple]]:
    paths = contract.get("paths", {})
    if not isinstance(paths, dict):
        return
    for route in sorted(paths, key=str):
        item = paths[route]
        if not isinstance(route, str) or not isinstance(item, dict):
            continue
        for method in sorted(item, key=str):
            if method in _HTTP_METHODS and isinstance(item[method], dict):
                yield route, method, item[method], ("paths", route, method)


def _map(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _parameters(contract: dict) -> Iterator[tuple[dict, tuple]]:
    paths = _map(contract.get("paths"))
    for route in sorted(paths, key=str):
        item = _map(paths[route])
        owners = [(item, ("paths", route))]
        owners += [(item[method], ("paths", route, method))
                   for method in sorted(item, key=str)
                   if method in _HTTP_METHODS and isinstance(item[method], dict)]
        for owner, path in owners:
            parameters = owner.get("parameters", [])
            if not isinstance(parameters, list):
                continue
            for index, param in enumerate(parameters):
                if isinstance(param, dict):
                    yield param, (*path, "parameters", index)
    for name, param in sorted(_map(_map(contract.get("components")).get("parameters")).items()):
        if isinstance(param, dict):
            yield param, ("components", "parameters", name)


def _content_schemas(owner: dict, path: tuple) -> Iterator[tuple[dict, tuple]]:
    for media, value in sorted(_map(owner.get("content")).items()):
        schema = _map(value).get("schema")
        if isinstance(schema, dict):
            yield schema, (*path, "content", media, "schema")


def _schema_roots(contract: dict) -> Iterator[tuple[dict, tuple]]:
    components = _map(contract.get("components"))
    for name, schema in sorted(_map(components.get("schemas")).items()):
        if isinstance(schema, dict):
            yield schema, ("components", "schemas", name)
    for parameter, path in _parameters(contract):
        schema = parameter.get("schema")
        if isinstance(schema, dict):
            yield schema, (*path, "schema")
        yield from _content_schemas(parameter, path)
    for _, _, op, path in _operations(contract):
        body = op.get("requestBody")
        if isinstance(body, dict):
            yield from _content_schemas(body, (*path, "requestBody"))
        for code, response in sorted(_map(op.get("responses")).items(), key=lambda pair: str(pair[0])):
            if isinstance(response, dict):
                yield from _content_schemas(response, (*path, "responses", code))
    for section in ("responses", "requestBodies"):
        for name, owner in sorted(_map(components.get(section)).items()):
            if isinstance(owner, dict):
                yield from _content_schemas(owner, ("components", section, name))


def _schema_nodes(schema: dict, path: tuple, ancestors: frozenset[int] = frozenset()) -> Iterator[tuple[dict, tuple]]:
    if id(schema) in ancestors or "$ref" in schema or any(k in schema for k in _COMPOSITIONS):
        return
    yield schema, path
    ancestors = ancestors | {id(schema)}
    for key in _SCHEMA_MAPS:
        for name, child in sorted(_map(schema.get(key)).items()):
            if isinstance(child, dict):
                yield from _schema_nodes(child, (*path, key, name), ancestors)
    for key in _SCHEMA_CHILDREN:
        child = schema.get(key)
        if isinstance(child, dict):
            yield from _schema_nodes(child, (*path, key), ancestors)


def _make_mutant(contract: dict, operator: MutationType, path: tuple,
                 old: Any, new: Any, index: int, *, edited: dict | None = None) -> Mutant:
    mutated = copy.deepcopy(contract) if edited is None else edited
    if edited is None:
        _set_by_path(mutated, list(path), copy.deepcopy(new))
    prefix = {MutationType.TYPE_CHANGE: "TYPE", MutationType.REMOVE_REQUIRED: "REQ",
              MutationType.STATUS_CODE_SWAP: "STATUS"}[operator]
    pointer = json_pointer(path)
    return Mutant(f"MUT-{prefix}-{index:03d}", operator, pointer,
                  f"{operator.value}: {pointer}: {old!r} -> {new!r}",
                  copy.deepcopy(old), copy.deepcopy(new), mutated)


def mutate_type_change(contract: dict[str, Any]) -> list[Mutant]:
    """Export schema type changes; examples/extensions are never traversed."""
    if not isinstance(contract, dict):
        return []
    result = []
    seen = set()
    for root, root_path in _schema_roots(contract):
        for node, path in _schema_nodes(root, root_path):
            kind = node.get("type")
            if isinstance(kind, str) and kind in TYPE_REPLACEMENTS and path not in seen:
                seen.add(path)
                result.append(_make_mutant(contract, MutationType.TYPE_CHANGE,
                                          (*path, "type"), kind, TYPE_REPLACEMENTS[kind], len(result) + 1))
    return result


def mutate_remove_required(contract: dict[str, Any]) -> list[Mutant]:
    """Export schema weakening, NOT deletion from an HTTP response."""
    if not isinstance(contract, dict):
        return []
    result = []
    seen = set()
    for root, root_path in _schema_roots(contract):
        for node, path in _schema_nodes(root, root_path):
            required = node.get("required")
            if path in seen or not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                continue
            seen.add(path)
            for index, name in enumerate(required):
                if name in required[:index]:
                    continue
                result.append(_make_mutant(contract, MutationType.REMOVE_REQUIRED,
                                          (*path, "required"), required,
                                          [x for x in required if x != name], len(result) + 1))
    for parameter, path in _parameters(contract):
        if ("$ref" not in parameter and parameter.get("required") is True
                and parameter.get("in") in {"query", "header", "cookie"}
                and isinstance(parameter.get("name"), str)):
            result.append(_make_mutant(contract, MutationType.REMOVE_REQUIRED,
                                      (*path, "required"), True, False, len(result) + 1))
    return result


def _status_replacement(code: str, responses: dict) -> str | None:
    codes = {str(k) for k in responses}
    if (not code.isascii() or not code.isdigit() or len(code) != 3
            or not 200 <= int(code) <= 599 or code in {"204", "205", "304"}
            or "default" in codes or any(k.endswith("XX") for k in codes)):
        return None
    preferred = STATUS_CODE_SWAPS.get(code, "500" if code.startswith("2") else "200")
    candidates = [preferred, "500", "502", "503"] if code.startswith("2") else [preferred, "200", "201", "202"]
    return next((x for x in candidates if x != code and x not in codes
                 and x.isdigit() and 200 <= int(x) <= 599
                 and x not in {"204", "205", "304"}), None)


def mutate_status_code_swap(contract: dict[str, Any]) -> list[Mutant]:
    """Rename one concrete response code without overwriting adjacent responses."""
    if not isinstance(contract, dict):
        return []
    result = []
    for _, method, operation, path in _operations(contract):
        if method == "head":
            continue
        responses = _map(operation.get("responses"))
        for code, response in sorted(responses.items(), key=lambda pair: str(pair[0])):
            new = _status_replacement(str(code), responses)
            if new is None or not isinstance(response, dict):
                continue
            mutated = copy.deepcopy(contract)
            replacement = copy.deepcopy(responses)
            replacement[new] = replacement.pop(code)
            _set_by_path(mutated, [*path, "responses"], replacement)
            result.append(_make_mutant(contract, MutationType.STATUS_CODE_SWAP,
                                      (*path, "responses", code), str(code), new,
                                      len(result) + 1, edited=mutated))
    return result


def generate_mutants(contract: dict[str, Any], operators: list[MutationType] | None = None) -> list[Mutant]:
    """Legacy export API. Response plans are separate and do not copy contracts."""
    if not isinstance(contract, dict):
        return []
    dispatch = {MutationType.TYPE_CHANGE: mutate_type_change,
                MutationType.REMOVE_REQUIRED: mutate_remove_required,
                MutationType.STATUS_CODE_SWAP: mutate_status_code_swap}
    result = []
    for operator in dict.fromkeys(MutationType if operators is None else operators):
        if operator not in dispatch:
            raise ValueError(f"Неизвестный оператор мутации: {operator}")
        result.extend(dispatch[operator](contract))
    return result


@dataclass(frozen=True)
class ResponseMutationPlan:
    """One response change; None in value_path means an array index to bind later."""
    mutant_id: str
    operator: MutationType
    method: str
    api_path: str
    status_code: int
    media_type: str | None
    schema_pointer: str
    value_path: tuple[str | None, ...]
    original_value: Any
    mutated_value: Any

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        data = asdict(self)
        data["operator"] = self.operator.value
        data["value_path"] = list(self.value_path)
        return data


@dataclass(frozen=True)
class PlanDiagnostic:
    status: str
    pointer: str
    reason: str


@dataclass
class ResponsePlanResult:
    plans: list[ResponseMutationPlan] = field(default_factory=list)
    diagnostics: list[PlanDiagnostic] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {"plans": [p.to_dict() for p in self.plans],
                "diagnostics": [asdict(d) for d in self.diagnostics]}


def _resolve_local(contract: dict, node: dict, path: tuple, seen: frozenset[str]) -> tuple[dict, tuple, frozenset[str]]:
    while "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("External or malformed $ref is unsupported")
        if ref in seen:
            raise ValueError("Recursive $ref is unsupported")
        seen = seen | {ref}
        parts = tuple(x.replace("~1", "/").replace("~0", "~") for x in ref[2:].split("/"))
        target: Any = contract
        for key in parts:
            if not isinstance(target, dict) or key not in target:
                raise ValueError("Unresolved local $ref")
            target = target[key]
        if not isinstance(target, dict):
            raise ValueError("$ref target is not an object")
        node, path = target, parts
    return node, path, seen


def generate_response_plans(contract: dict[str, Any]) -> ResponsePlanResult:
    """Plan response defects for OpenAPI 3.0.x without altering data or calling LLM."""
    result = ResponsePlanResult()

    def diagnostic(path: tuple, reason: str, status: str = "unsupported") -> None:
        result.diagnostics.append(PlanDiagnostic(status, json_pointer(path), reason))

    if not isinstance(contract, dict) or not str(contract.get("openapi", "")).startswith("3.0."):
        diagnostic((), "Only OpenAPI 3.0.x response plans are supported")
        return result

    def add(operator: MutationType, route: str, method: str, code: int, media: str | None,
            path: tuple, value_path: tuple, old: Any, new: Any) -> None:
        result.plans.append(ResponseMutationPlan(f"RESP-{len(result.plans) + 1:04d}", operator,
            method.upper(), route, code, media, json_pointer(path), value_path, old, new))

    def walk(node: dict, path: tuple, values: tuple, refs: frozenset[str],
             route: str, method: str, code: int, media: str, ancestors: frozenset[int] = frozenset()) -> None:
        try:
            node, path, refs = _resolve_local(contract, node, path, refs)
        except ValueError as exc:
            diagnostic(path, str(exc))
            return
        if id(node) in ancestors:
            diagnostic(path, "Recursive schema is unsupported")
            return
        ancestors = ancestors | {id(node)}
        if any(key in node for key in _COMPOSITIONS):
            diagnostic(path, "Composed schemas are unsupported")
            return
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in TYPE_REPLACEMENTS:
            diagnostic(path, "Missing or unsupported schema type")
            return
        add(MutationType.TYPE_CHANGE, route, method, code, media,
            path, values, kind, TYPE_REPLACEMENTS[kind])
        if kind == "object":
            required = node.get("required", [])
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                diagnostic((*path, "required"), "Malformed required list", "invalid")
                return
            for name in dict.fromkeys(required):
                add(MutationType.REMOVE_REQUIRED, route, method, code, media,
                    (*path, "required"), (*values, name), "present", "absent")
            for name, child in sorted(_map(node.get("properties")).items()):
                if isinstance(child, dict):
                    walk(child, (*path, "properties", name), (*values, name), refs,
                         route, method, code, media, ancestors)
                else:
                    diagnostic((*path, "properties", name), "Malformed property schema", "invalid")
            if isinstance(node.get("additionalProperties"), dict):
                diagnostic((*path, "additionalProperties"), "Dynamic property names require runtime planning")
        elif kind == "array":
            items = node.get("items")
            if isinstance(items, dict):
                walk(items, (*path, "items"), (*values, None), refs,
                     route, method, code, media, ancestors)
            else:
                diagnostic((*path, "items"), "Missing or unsupported array items")

    for route, method, op, op_path in _operations(contract):
        responses = _map(op.get("responses"))
        for raw_code, raw_response in sorted(responses.items(), key=lambda pair: str(pair[0])):
            code = str(raw_code)
            path = (*op_path, "responses", raw_code)
            if not code.isascii() or not code.isdigit() or len(code) != 3 or not 200 <= int(code) <= 599:
                diagnostic(path, "Only concrete final HTTP status codes are supported")
                continue
            if method == "head" or code in {"204", "205", "304"}:
                diagnostic(path, "Bodyless response is outside the initial scope")
                continue
            if not isinstance(raw_response, dict):
                diagnostic(path, "Response is not an object", "invalid")
                continue
            try:
                response, response_path, refs = _resolve_local(contract, raw_response, path, frozenset())
            except ValueError as exc:
                diagnostic(path, str(exc))
                continue
            replacement = _status_replacement(code, responses)
            if replacement is not None:
                add(MutationType.STATUS_CODE_SWAP, route, method, int(code), None,
                    path, (), int(code), int(replacement))
            else:
                diagnostic(path, "No unambiguous unused status replacement")
            content = _map(response.get("content"))
            for media, entry in sorted(content.items()):
                schema_path = (*response_path, "content", media, "schema")
                if not isinstance(media, str) or not (media == "application/json" or media.endswith("+json")):
                    diagnostic(schema_path, "Only JSON response media types are supported")
                    continue
                schema = _map(entry).get("schema")
                if not isinstance(schema, dict):
                    diagnostic(schema_path, "Missing response schema")
                    continue
                walk(schema, schema_path, (), refs, route, method, int(code), media)
    return result


def count_mutants_by_operator(mutants: list[Mutant]) -> dict[str, int]:
    """Подсчитать количество сгенерированных мутантов по каждому типу оператора."""
    counts: dict[str, int] = {op.value: 0 for op in MutationType}
    for m in mutants:
        counts[m.operator.value] = counts.get(m.operator.value, 0) + 1
    return counts


def calculate_mutation_score(total_mutants: int, killed_mutants: int,
                             mutants_by_operator: dict[str, int] | None = None,
                             killed_by_operator: dict[str, int] | None = None) -> MutationScoreResult:
    """Score on evaluated mutants; preserve the existing package interface."""
    if type(total_mutants) is not int or type(killed_mutants) is not int:
        raise TypeError("Количество мутантов должно быть целым числом.")
    if not 0 <= killed_mutants <= total_mutants:
        raise ValueError("Требуется 0 <= killed_mutants <= total_mutants.")
    score = round(100 * killed_mutants / total_mutants, 2) if total_mutants else None
    return MutationScoreResult(total_mutants, killed_mutants, total_mutants - killed_mutants,
                               score, mutants_by_operator or {}, killed_by_operator or {})


def _set_by_path(d: Any, path: list[Any], val: Any) -> None:
    """Set an existing target; invalid paths fail rather than emit a no-op."""
    if not path:
        raise ValueError("Путь мутации не может быть пустым.")
    current = d
    for raw_key in path[:-1]:
        key = int(raw_key) if isinstance(current, list) else raw_key
        current = current[key]
    key = int(path[-1]) if isinstance(current, list) else path[-1]
    current[key]  # Confirm existence before writing.
    current[key] = val


# Validation and response preparation


@dataclass(frozen=True)
class ValidationIssue:
    pointer: str
    reason: str


@dataclass
class ValidationResult:
    status: str
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MutationPreparation:
    status: str
    reason: str
    mutant_id: str
    original_status: int
    mutated_status: int | None = None
    body: Any = None
    value_pointer: str | None = None
    violations: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UnsupportedSchema(ValueError):
    pass


def _validators():
    from openapi_spec_validator import OpenAPIV30SpecValidator
    from openapi_schema_validator import OAS30ReadValidator
    return OpenAPIV30SpecValidator, OAS30ReadValidator


def _get(document: Any, pointer: str) -> tuple[Any, tuple]:
    if pointer == "":
        return document, ()
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Invalid JSON Pointer")
    parts = []
    value = document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON Pointer escape")
        key: Any = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isascii() or not key.isdigit() or (len(key) > 1 and key[0] == "0"):
                raise ValueError("Invalid array index")
            key = int(key)
        elif isinstance(value, dict) and key not in value and key.isdigit() and int(key) in value:
            key = int(key)  # YAML's unquoted response status keys
        value = value[key]
        parts.append(key)
    return value, tuple(parts)


def _normalise(document: Any, path: tuple = ()) -> Any:
    """Clone JSON-like data and normalise integer YAML response keys only."""
    if isinstance(document, list):
        return [_normalise(x, (*path, i)) for i, x in enumerate(document)]
    if not isinstance(document, dict):
        return document
    result = {}
    for key, value in document.items():
        if (key == "responses" and isinstance(value, dict) and len(path) == 3
                and path[0] == "paths" and path[2] in _HTTP_METHODS):
            responses = {}
            for code, response in value.items():
                canonical = str(code) if type(code) is int else code
                if canonical in responses:
                    raise ValueError("Duplicate string/integer response status")
                responses[canonical] = _normalise(response, (*path, key, canonical))
            result[key] = responses
        else:
            result[key] = _normalise(value, (*path, key))
    return result


def _reference_preflight(document: Any, root: dict, path: tuple = (),
                         named_map: bool = False) -> Iterator[ValidationIssue]:
    """Guard real references, preserving literal examples and property names."""
    map_fields = {"properties", "schemas", "parameters", "headers", "responses",
                  "requestBodies", "examples", "links", "callbacks", "securitySchemes",
                  "content", "paths", "scopes", "encoding"}
    if isinstance(document, dict):
        if not named_map and "$ref" in document:
            ref = document["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                yield ValidationIssue(json_pointer((*path, "$ref")), "External references are unsupported")
            else:
                try:
                    _get(root, unquote(ref[1:]))
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    yield ValidationIssue(json_pointer((*path, "$ref")), f"Unresolved or malformed local reference: {exc}")
        for key, child in document.items():
            if not named_map and (key in {"example", "default", "enum", "value"}
                                  or (isinstance(key, str) and key.startswith("x-"))):
                continue
            yield from _reference_preflight(child, root, (*path, key),
                                             not named_map and key in map_fields)
    elif isinstance(document, list):
        for index, child in enumerate(document):
            yield from _reference_preflight(child, root, (*path, index))


def validate_contract(contract: Any) -> ValidationResult:
    """Check complete OpenAPI 3.0 document offline, with explicit failure modes."""
    if not isinstance(contract, dict):
        return ValidationResult("invalid", [ValidationIssue("", "Contract must be an object")])
    if not re.fullmatch(r"3\.0\.\d+", str(contract.get("openapi", ""))):
        return ValidationResult("unsupported", [ValidationIssue("/openapi", "Only OpenAPI 3.0.x is supported")])
    try:
        json.dumps(contract, allow_nan=False)
        normal = _normalise(contract)
    except (TypeError, ValueError, RecursionError) as exc:
        return ValidationResult("invalid", [ValidationIssue("", f"Non-JSON or cyclic contract: {exc}")])
    references = list(_reference_preflight(normal, normal))
    if references:
        status = "unsupported" if any("External" in x.reason for x in references) else "invalid"
        return ValidationResult(status, references)
    try:
        spec_validator, _ = _validators()
        from jsonschema_path import SchemaPath
    except ImportError:
        return ValidationResult("unavailable", [ValidationIssue("", "Install mutation validation requirements")])
    try:
        def deny_external(uri: str):
            raise UnsupportedSchema(f"External reference resolution is disabled: {uri}")
        schema_path = SchemaPath.from_dict(normal, handlers={
            "http": deny_external, "https": deny_external,
            "file": deny_external, "<all_urls>": deny_external,
        })
        errors = list(spec_validator(schema_path).iter_errors())
    except Exception as exc:
        # A validator crash is never evidence that the specification is valid.
        return ValidationResult("unavailable", [ValidationIssue("", f"Validator failure: {type(exc).__name__}: {exc}")])
    if errors:
        return ValidationResult("invalid", [ValidationIssue(json_pointer(tuple(e.path)), e.message) for e in errors])
    return ValidationResult("valid")


def _expanded_schema(contract: dict, schema: dict, seen: frozenset[str] = frozenset(),
                     ancestors: frozenset[int] = frozenset()) -> dict:
    if id(schema) in ancestors:
        raise UnsupportedSchema("Recursive schema is unsupported")
    ancestors = ancestors | {id(schema)}
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            raise UnsupportedSchema("External or recursive schema reference")
        target, _ = _get(contract, unquote(ref[1:]))
        if not isinstance(target, dict):
            raise ValueError("Schema reference does not target an object")
        return _expanded_schema(contract, target, seen | {ref}, ancestors)
    if any(key in schema for key in _COMPOSITIONS):
        raise UnsupportedSchema("Composed response schema is unsupported")
    result = copy.deepcopy(schema)
    for name, child in _map(schema.get("properties")).items():
        result["properties"][name] = _expanded_schema(contract, child, seen, ancestors)
    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, dict):
            result[key] = _expanded_schema(contract, child, seen, ancestors)
    return result


def _body_errors(schema: dict, body: Any) -> list[ValidationIssue]:
    _, read_validator = _validators()
    validator = read_validator(schema, format_checker=read_validator.FORMAT_CHECKER)
    return [ValidationIssue(json_pointer(tuple(e.path)), e.message) for e in validator.iter_errors(body)]


def _bindings(body: Any, template: tuple, concrete: tuple = ()) -> Iterator[tuple]:
    if not template:
        yield concrete
        return
    part, remaining = template[0], template[1:]
    if part is None:
        if isinstance(body, list):
            for index, child in enumerate(body):
                yield from _bindings(child, remaining, (*concrete, index))
    elif isinstance(body, dict) and part in body:
        yield from _bindings(body[part], remaining, (*concrete, part))


def _response_schema(contract: dict, plan: ResponseMutationPlan, media: str) -> dict | None:
    operation = contract["paths"][plan.api_path][plan.method.lower()]
    responses = operation["responses"]
    response = responses.get(str(plan.status_code), responses.get(plan.status_code))
    seen = set()
    while isinstance(response, dict) and "$ref" in response:
        ref = response["$ref"]
        if ref in seen:
            raise UnsupportedSchema("Recursive response reference")
        seen.add(ref)
        response, _ = _get(contract, unquote(ref[1:]))
    entry = _map(_map(response).get("content")).get(media)
    if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
        return _expanded_schema(contract, entry["schema"])
    if _map(response).get("content"):
        raise UnsupportedSchema("Response media type has no supported schema")
    return None


def prepare_response_mutation(contract: dict, plan: ResponseMutationPlan, *,
                              response_status: int, body: Any,
                              media_type: str = "application/json") -> MutationPreparation:
    """Prove applicability and prepare a COPY of one defective response."""
    def result(status: str, reason: str, **kwargs: Any) -> MutationPreparation:
        return MutationPreparation(status, reason, plan.mutant_id, response_status, **kwargs)

    validation = validate_contract(contract)
    if validation.status != "valid":
        return result(validation.status, "Original contract is not validated", violations=validation.issues)
    if plan not in generate_response_plans(contract).plans:
        return result("invalid", "Plan does not match the original contract")
    if type(response_status) is not int or response_status != plan.status_code:
        return result("not_applicable", "Observed status differs from the planned response")
    media = media_type.split(";", 1)[0].strip().lower()
    if plan.media_type is not None and media != plan.media_type:
        return result("not_applicable", "Observed media type differs from the planned response")
    try:
        json.dumps(body, allow_nan=False)
        schema = _response_schema(contract, plan, media)
        if schema is None and plan.operator != MutationType.STATUS_CODE_SWAP:
            return result("unsupported", "Body mutation requires a response schema")
        baseline_errors = _body_errors(schema, body) if schema is not None else []
    except UnsupportedSchema as exc:
        return result("unsupported", str(exc))
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return result("invalid", f"Cannot validate response: {exc}")
    except ImportError:
        return result("unavailable", "Install mutation validation requirements")
    except Exception as exc:
        return result("unavailable", f"Response validator failure: {type(exc).__name__}: {exc}")
    if baseline_errors:
        return result("baseline_invalid", "Original response already violates its schema", violations=baseline_errors)

    if plan.operator == MutationType.STATUS_CODE_SWAP:
        # Membership above proves a concrete unused code without default/ranges.
        return result("ready", "Replacement status is not allowed by the original response map",
                      mutated_status=plan.mutated_value, body=copy.deepcopy(body),
                      violations=[ValidationIssue("/status", "Undocumented response status")])

    target = next(_bindings(body, plan.value_path), None)
    if target is None:
        return result("not_applicable", "Target field is absent or array has no matching element")
    mutated = copy.deepcopy(body)
    if plan.operator == MutationType.TYPE_CHANGE:
        replacements = {"integer": 0, "string": "mutation-value", "boolean": False,
                        "object": {}, "array": [], "number": 0.5}
        new = replacements[plan.mutated_value]
        if target:
            parent, _ = _get(mutated, json_pointer(target[:-1]))
            parent[target[-1]] = new
        else:
            mutated = new
    elif plan.operator == MutationType.REMOVE_REQUIRED:
        parent, _ = _get(mutated, json_pointer(target[:-1]))
        del parent[target[-1]]
    else:
        return result("invalid", "Unknown response operator")
    try:
        errors = _body_errors(schema, mutated)
    except Exception as exc:
        return result("unavailable", f"Response validator failure: {type(exc).__name__}: {exc}")
    if not errors:
        return result("invalid", "Change does not violate the original response schema")
    return result("ready", "One applied change violates the original response schema",
                  mutated_status=response_status, body=mutated,
                  value_pointer=json_pointer(target), violations=errors)


# Campaign execution and scoring


STATUSES = ("killed", "survived", "not_exercised", "invalid", "unsupported", "inconclusive")


@dataclass(frozen=True)
class CaseObservation:
    nodeid: str
    outcome: str  # passed, failed, error, skipped
    phase: str = "call"
    failure_kind: str | None = None  # assertion, response_validation, other
    failure_signature: str | None = None  # stable location/type, not volatile text
    message: str = ""


@dataclass(frozen=True)
class DeliveryEvidence:
    mutant_id: str
    nodeid: str
    phase: str
    delivered: bool
    validated: bool
    value_pointer: str | None = None


@dataclass(frozen=True)
class SuiteObservation:
    status: str  # completed, timeout, interrupted, infrastructure_error, not_run
    exit_code: int | None = None
    collected_nodeids: tuple[str, ...] = ()
    cases: tuple[CaseObservation, ...] = ()
    deliveries: tuple[DeliveryEvidence, ...] = ()
    preparation_status: str | None = None
    diagnostics: tuple[str, ...] = ()
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _complete(run: SuiteObservation, expected: tuple[str, ...] | None = None) -> bool:
    collected = run.collected_nodeids
    actual = tuple(c.nodeid for c in run.cases)
    if (run.status != "completed" or type(run.exit_code) is not int
            or run.exit_code not in (0, 1) or run.diagnostics or not collected
            or len(set(collected)) != len(collected) or len(set(actual)) != len(actual)
            or set(collected) != set(actual)):
        return False
    if expected is not None and set(collected) != set(expected):
        return False
    if any(c.outcome not in {"passed", "failed"} or c.phase != "call" for c in run.cases):
        return False
    return run.exit_code == (1 if any(c.outcome == "failed" for c in run.cases) else 0)


def _passing(run: SuiteObservation, expected: tuple[str, ...] | None = None) -> bool:
    return _complete(run, expected) and all(c.outcome == "passed" for c in run.cases)


def _attempt_verdict(plan: ResponseMutationPlan, run: SuiteObservation,
                     expected: tuple[str, ...]) -> tuple[str, str]:
    if run.preparation_status in {"invalid", "unsupported"} and run.status == "not_run":
        if run.deliveries or run.cases or run.collected_nodeids:
            return "inconclusive", "Rejected preparation unexpectedly has execution evidence"
        return run.preparation_status, "; ".join(run.diagnostics) or "Preparation rejected"
    if run.preparation_status in {"baseline_invalid", "unavailable"}:
        return "inconclusive", "Original response or validation infrastructure is not usable"
    if not _complete(run, expected):
        return "inconclusive", "Incomplete, changed, skipped or unsuccessful infrastructure run"
    if not run.deliveries:
        if _passing(run, expected):
            return "not_exercised", "No mutation delivered during the complete successful suite"
        return "inconclusive", "Test failed without evidence of mutation delivery"
    if len(run.deliveries) != 1:
        return "inconclusive", "Exactly one mutation delivery is required"
    delivery = run.deliveries[0]
    if (delivery.mutant_id != plan.mutant_id or delivery.delivered is not True
            or delivery.validated is not True or delivery.phase != "call"
            or delivery.nodeid not in expected or run.preparation_status != "ready"):
        return "inconclusive", "Delivery is unvalidated, mismatched or outside a test body"
    if _passing(run, expected):
        return "survived", "Confirmed defect was delivered; the full suite passed"
    failures = [c for c in run.cases if c.outcome == "failed"]
    # Conservative attribution: a failure in some OTHER test is not enough.
    if any(c.nodeid != delivery.nodeid or c.failure_kind not in {"assertion", "response_validation"}
           or not c.failure_signature for c in failures):
        return "inconclusive", "Failure is not attributable to the test receiving the changed response"
    return "killed", "Response recipient failed; repeat and recovery still required"


def classify_result(plan: ResponseMutationPlan, attempts: list[SuiteObservation],
                    recovery: SuiteObservation | None, expected: tuple[str, ...]) -> tuple[str, str]:
    """Final result requires two consistent observations and successful recovery."""
    if not attempts:
        return "inconclusive", "Mutation was not executed"
    first = _attempt_verdict(plan, attempts[0], expected)
    if first[0] in {"invalid", "unsupported", "inconclusive"}:
        return first
    if len(attempts) != 2:
        return "inconclusive", "Two mutation attempts are required"
    second = _attempt_verdict(plan, attempts[1], expected)
    if second[0] != first[0]:
        return "inconclusive", "Mutation result was not reproducible"
    if first[0] == "killed":
        signatures = [{(c.nodeid, c.failure_kind, c.failure_signature)
                       for c in a.cases if c.outcome == "failed"} for a in attempts]
        if signatures[0] != signatures[1]:
            return "inconclusive", "Different failures occurred in the repeated runs"
    if recovery is None or not _passing(recovery, expected) or recovery.deliveries:
        return "inconclusive", "Recovery did not restore the same successful test suite"
    return first[0], {
        "killed": "Same receiving test detected the defect twice; recovery passed",
        "survived": "Delivered defect went undetected twice; recovery passed",
        "not_exercised": "Neither complete run reached the target; recovery passed",
    }[first[0]]


def summarise_results(rows: list[dict], *, eligible: bool = True) -> dict:
    counts = {s: 0 for s in STATUSES}
    for row in rows:
        if row["status"] not in counts:
            raise ValueError(f"Unknown mutation status: {row['status']}")
        counts[row["status"]] += 1
    k, s, u, e = (counts[name] for name in ("killed", "survived", "not_exercised", "inconclusive"))

    def metric(n, d, *, allowed=True):
        reason = None
        if not eligible:
            reason = "Campaign baseline or input integrity is not valid"
        elif not allowed:
            reason = "Inconclusive applicable targets remain"
        elif not d:
            reason = "No eligible denominator"
        return {"numerator": n, "denominator": d,
                "percent": round(100 * n / d, 2) if reason is None else None, "reason": reason}

    return {"counts": counts, "total_plans": len(rows),
            "mutation_score": metric(k, k + s),
            "evaluated_fraction": metric(k + s, k + s + u + e),
            "detection_including_unexercised": metric(k, k + s + u, allowed=e == 0)}


def _atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _markdown(report: dict) -> str:
    lines = ["# Mutation campaign", "", f"Status: {report['status']}",
             f"Score: {report['metrics']['mutation_score']['percent']}",
             f"Evaluated: {report['metrics']['evaluated_fraction']['percent']}", "",
             "| Mutant | Operator | Status | Reason |", "|---|---|---|---|"]
    for row in report['results']:
        values = [row['plan']['mutant_id'], row['plan']['operator'], row['status'], row['reason']]
        lines.append("| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in values) + " |")
    return "\n".join(lines) + "\n"


def run_mutation_campaign(contract: dict, *,
                          execute: Callable[[ResponseMutationPlan | None, Path], SuiteObservation],
                          fingerprint: Callable[[], str], output_dir: Path | str, max_workers: int = 1) -> dict:
    """Evaluate independent plans; each plan keeps two attempts and recovery in order."""
    if type(max_workers) is not int or not 1 <= max_workers <= 4:
        raise ValueError("max_workers must be an integer from 1 to 4")
    lock = RLock()
    folder = Path(output_dir).resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid4().hex[:8])
    folder.mkdir(parents=True)
    report = {'status': 'running', 'inputs_unchanged': True, 'baseline': None,
              'generation_diagnostics': [], 'results': [], 'run_count': 0,
              'reason': None, 'report_dir': str(folder)}
    baseline_ok = False
    interrupted = False

    def save():
        report['metrics'] = summarise_results(report['results'],
            eligible=baseline_ok and report['inputs_unchanged'])
        _atomic(folder / 'report.json', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        _atomic(folder / 'report.md', _markdown(report))
        return report

    validation = validate_contract(contract)
    if validation.status != 'valid':
        report.update(status='contract_' + validation.status,
                      reason='Original contract was not validated', contract_validation=validation.to_dict())
        return save()
    snapshot = copy.deepcopy(contract)
    generated = generate_response_plans(snapshot)
    report['generation_diagnostics'] = generated.to_dict()['diagnostics']
    report['results'] = [{'plan': p.to_dict(), 'status': 'inconclusive', 'reason': 'Not executed yet',
                         'attempts': [], 'recovery': None} for p in generated.plans]
    try:
        original_hash = fingerprint()
        if not isinstance(original_hash, str) or not original_hash:
            raise ValueError('Fingerprint must be a nonempty string')
        report['input_fingerprint'] = original_hash
    except Exception as exc:
        report.update(status='input_error', inputs_unchanged=False, reason=str(exc))
        return save()

    def intact():
        with lock:
            try:
                report['inputs_unchanged'] &= fingerprint() == original_hash
            except Exception as exc:
                report.update(inputs_unchanged=False, reason=str(exc))
            return report['inputs_unchanged']

    def invoke(plan, label):
        nonlocal interrupted
        if not intact():
            return SuiteObservation('infrastructure_error', diagnostics=('Inputs changed',))
        destination = folder / label
        destination.mkdir()
        with lock:
            report['run_count'] += 1
        try:
            result = execute(plan, destination)
            if not isinstance(result, SuiteObservation):
                raise TypeError('Adapter must return SuiteObservation')
            json.dumps(result.to_dict(), allow_nan=False)
            with lock:
                interrupted |= result.status == 'interrupted'
        except KeyboardInterrupt:
            with lock:
                interrupted = True
            result = SuiteObservation('interrupted')
        except Exception as exc:
            result = SuiteObservation('infrastructure_error', diagnostics=(str(exc),))
        return result if intact() else SuiteObservation('infrastructure_error', diagnostics=('Inputs changed',))

    save()
    baseline = invoke(None, 'baseline')
    report['baseline'] = baseline.to_dict()
    baseline_ok = _passing(baseline) and not baseline.deliveries and report['inputs_unchanged']
    if not baseline_ok:
        report.update(status='interrupted' if interrupted else 'baseline_failed',
                      reason='A complete passing baseline with unchanged inputs is required')
        return save()
    def evaluate(index):
        plan = generated.plans[index]
        if interrupted or not report['inputs_unchanged']:
            return index, None
        row = dict(report['results'][index])
        attempts = [invoke(plan, f'{index + 1:04d}-attempt-1')]
        recovery = None
        verdict, _ = _attempt_verdict(plan, attempts[0], baseline.collected_nodeids)
        if verdict in {'killed', 'survived', 'not_exercised'} and not interrupted and report['inputs_unchanged']:
            attempts.append(invoke(plan, f'{index + 1:04d}-attempt-2'))
            if not interrupted and report['inputs_unchanged']:
                recovery = invoke(None, f'{index + 1:04d}-recovery')
        row.update(attempts=[a.to_dict() for a in attempts], recovery=recovery.to_dict() if recovery else None)
        row['status'], row['reason'] = classify_result(plan, attempts, recovery, baseline.collected_nodeids)
        return index, row

    # Bounded batches prevent queued plans from starting after interruption.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for start in range(0, len(generated.plans), max_workers):
            if interrupted or not report['inputs_unchanged']:
                break
            futures = [pool.submit(evaluate, i) for i in range(start, min(start + max_workers, len(generated.plans)))]
            try:
                for future in futures:
                    index, row = future.result()
                    if row is not None:
                        report['results'][index] = row
                        save()
            except KeyboardInterrupt:
                interrupted = True
            # Retain observations produced by the current batch during cleanup.
            for future in futures:
                index, row = future.result()
                if row is not None:
                    report['results'][index] = row
    intact()
    if interrupted or not report['inputs_unchanged']:
        report['status'] = 'interrupted' if interrupted else 'inputs_changed'
    elif not generated.plans:
        report['status'] = 'no_plans'
    else:
        report['status'] = 'completed_with_inconclusive' if any(r['status'] == 'inconclusive' for r in report['results']) else 'completed'
    return save()
