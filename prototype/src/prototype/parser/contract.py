"""
Этот модуль отвечает за чтение API-контракта.

Его задача — не передавать весь OpenAPI/Swagger-файл в LLM.

Вместо этого обычный Python:
1. читает JSON или YAML;
2. определяет тип спецификации;
3. извлекает HTTP-операции;
4. создаёт короткое summary.

Это уменьшает количество токенов, которые получает модель.
"""

import json
import hashlib
from datetime import date, datetime
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml


# Только эти ключи внутри paths считаем HTTP-методами.
HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "options",
    "head",
    "trace",
}


class ContractInputError(ValueError):
    """A malformed or unsupported contract, before any LLM call."""


def _validate_structure(data: dict[str, Any], *, require_operations: bool = True) -> None:
    """Check the fields this parser consumes, not the full OpenAPI schema."""
    if "openapi" in data and "swagger" in data:
        raise ContractInputError("Укажите только одну спецификацию: OpenAPI или Swagger.")
    if "openapi" in data:
        version = data["openapi"]
        if not isinstance(version, str) or not re.fullmatch(r"3\.\d+\.\d+(?:[-+][\w.-]+)?", version):
            raise ContractInputError("Поддерживается OpenAPI 3.x (например, 3.0.3).")
    elif data.get("swagger") != "2.0":
        raise ContractInputError("Контракт должен содержать OpenAPI 3.x или Swagger 2.0.")
    if "paths" not in data:
        raise ContractInputError("Контракт должен содержать раздел paths.")
    operations = 0
    for field in ("info", "paths"):
        if not isinstance(data.get(field, {}), dict):
            raise ContractInputError(f"Поле {field} должно быть объектом.")
    for route, path_item in data.get("paths", {}).items():
        if not isinstance(route, str):
            raise ContractInputError("Ключи paths должны быть строками.")
        if route.startswith("x-"):
            continue  # OpenAPI extension, not an endpoint.
        if not route.startswith("/") or not isinstance(path_item, dict):
            raise ContractInputError(f"paths[{route!r}] должен описывать путь и содержать объект.")
        for method, operation in path_item.items():
            if not isinstance(method, str):
                raise ContractInputError(f"Ключи paths[{route!r}] должны быть строками.")
            if method.lower() in HTTP_METHODS and not isinstance(operation, dict):
                raise ContractInputError(f"Операция {method} в paths[{route!r}] должна быть объектом.")
            if method.lower() in HTTP_METHODS:
                operations += 1
    if require_operations and not operations:
        raise ContractInputError("Раздел paths должен содержать хотя бы одну HTTP-операцию.")


def normalize_contract(data: dict[str, Any]) -> dict[str, Any]:
    """Expand document-local JSON pointers without I/O; retain recursive/external refs.

    This is context preparation, not a full OpenAPI/JSON Schema validator.
    Expansion is bounded to prevent recursive or heavily shared schemas
    exhausting the orchestrator. The original input is never mutated.
    """
    visited = 0
    def pointer(ref):
        value = data
        try:
            for token in unquote(ref[2:]).split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", token):
                        raise ValueError("invalid JSON pointer array index")
                    value = value[int(token)]
                else:
                    value = value[token]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ContractInputError(f"Не удалось разрешить $ref: {ref}") from exc
        return value

    mapping_fields = {"properties", "patternProperties", "dependentSchemas", "schemas", "definitions",
                      "responses", "headers", "links", "parameters", "requestBodies", "pathItems",
                      "callbacks", "securitySchemes", "examples"}
    def expand(value, active=(), depth=0, *, mapping=False, literal=False):
        nonlocal visited
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ContractInputError("Слишком сложный контракт: превышен лимит раскрытия $ref.")
        if isinstance(value, list):
            return [expand(item, active, depth + 1, literal=literal) for item in value]
        if isinstance(value, (date, datetime)):
            return value.isoformat()  # YAML timestamps become JSON-compatible examples
        if not isinstance(value, dict):
            return value
        def child(key, item):
            # Example/default/enum values are API data, not Reference Objects.
            is_literal = literal or (not mapping and (key in {"example", "default", "enum", "const", "value"}
                or (key == "examples" and (isinstance(item, list) or "swagger" in data))))
            return expand(item, active, depth + 1,
                          mapping=not mapping and key in mapping_fields and isinstance(item, dict), literal=is_literal)
        has_ref = not mapping and not literal and "$ref" in value
        ref = value.get("$ref") if has_ref else None
        if has_ref:
            if not isinstance(ref, str):
                raise ContractInputError("Значение $ref должно быть строкой.")
            siblings = {key: child(key, item) for key, item in value.items() if key != "$ref"}
            if ref.startswith("#/") and ref not in active:
                target = expand(pointer(ref), (*active, ref), depth + 1)
                if isinstance(target, dict):
                    return {**target, **siblings}
                if not siblings:
                    return target
            return {"$ref": ref, **siblings}
        return {key: child(key, item) for key, item in value.items()}

    return expand(data)


def _load_contract(path: Path) -> dict[str, Any]:
    """
    Загрузить контракт из JSON или YAML.

    Сначала пытаемся прочитать файл как JSON.
    Если JSON-парсинг не удался, пытаемся прочитать его как YAML.

    Args:
        path: Путь к файлу контракта.

    Returns:
        Контракт как Python-словарь.

    Raises:
        ContractInputError: невалидный формат или структура контракта.
    """

    text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ContractInputError("Не удалось разобрать контракт как JSON/YAML.") from exc

    if not isinstance(data, dict):
        raise ContractInputError(
            "API-контракт должен содержать объект JSON/YAML."
        )

    _validate_structure(data, require_operations=False)
    normalized = normalize_contract(data)
    _validate_structure(normalized)
    return normalized


def read_contract_summary(contract_path: str) -> dict[str, Any]:
    """
    Прочитать API-контракт и вернуть его краткое описание.

    Агенту передаётся только этот результат, а не весь OpenAPI-файл.

    Args:
        contract_path: Путь к OpenAPI/Swagger-файлу.

    Returns:
        Краткое структурированное описание контракта.

    Raises:
        FileNotFoundError: если файл не существует.
    """

    path = Path(contract_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Контракт не найден: {path}"
        )

    data = _load_contract(path)

    return summarize_contract(data, str(path))


def summarize_contract(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Extract contract identity and operations without reading files or using LLM."""
    # Определяем тип спецификации.
    if "openapi" in data:
        specification = f"OpenAPI {data['openapi']}"
        protocol = "REST"

    elif "swagger" in data:
        specification = f"Swagger {data['swagger']}"
        protocol = "REST"

    else:
        specification = "Unknown"
        protocol = "Unknown"

    operations: list[str] = []

    # Извлекаем все HTTP-операции из секции paths.
    for route, path_item in data.get("paths", {}).items():

        if route.startswith("x-"):
            continue
        if not isinstance(path_item, dict):
            continue

        for method in path_item:

            if method.lower() in HTTP_METHODS:
                operations.append(
                    f"{method.upper()} {route}"
                )

    info = data.get("info", {})

    return {
        "file": source,
        "protocol": protocol,
        "specification": specification,
        "title": info.get("title", ""),
        "version": info.get("version", ""),
        "operation_count": len(operations),
        "operations": operations,
    }


def contract_provenance(data: dict[str, Any], path: Path) -> dict[str, Any]:
    return {**summarize_contract(data, str(path)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
