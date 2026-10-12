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
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from http.client import HTTPException
import socket
import time

import yaml
from .contract_input import ContractDocument, ContractInputError, normalize_location


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


MAX_CONTRACT_BYTES = 5 * 1024 * 1024
CONTRACT_DOWNLOAD_TIMEOUT = 15.0


class _ContractRedirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        normalize_location(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_contract_document(location: Path | str) -> ContractDocument:
    """Read a file or bounded HTTP(S) response once; never resolve remote $refs."""
    location = normalize_location(location)
    resolved_url = None
    if isinstance(location, Path):
        if location.stat().st_size > MAX_CONTRACT_BYTES:
            raise ContractInputError("Контракт превышает лимит 5 MiB.")
        with location.open("rb") as stream:
            raw = stream.read(MAX_CONTRACT_BYTES + 1)
    else:
        opener = build_opener(ProxyHandler({}), _ContractRedirects())
        deadline = time.monotonic() + CONTRACT_DOWNLOAD_TIMEOUT
        try:
            with opener.open(Request(location, headers={"Accept": "application/json, application/yaml, text/yaml"}),
                             timeout=CONTRACT_DOWNLOAD_TIMEOUT) as response:
                resolved_url = response.geturl()
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_CONTRACT_BYTES:
                    raise ContractInputError("Контракт превышает лимит 5 MiB.")
                chunks, size = [], 0
                while size <= MAX_CONTRACT_BYTES:
                    if time.monotonic() >= deadline:
                        raise ContractInputError("Истёк лимит времени загрузки контракта.")
                    chunk = response.read1(min(65536, MAX_CONTRACT_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
        except HTTPError as exc:
            raise ContractInputError(f"Загрузка контракта: HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException) as exc:
            # Do not include URLs, query secrets or exception headers in reports.
            raise ContractInputError("Не удалось загрузить контракт по HTTP(S): соединение или таймаут.") from exc
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ContractInputError("Контракт превышает лимит 5 MiB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ContractInputError("Контракт должен быть в кодировке UTF-8.") from exc
    try:
        data = _parse_contract(text)
    except RecursionError as exc:
        raise ContractInputError("Слишком глубокая структура JSON/YAML контракта.") from exc
    return ContractDocument(str(location), raw, data, resolved_url)


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
    for field in ("title", "version"):
        if field in data.get("info", {}) and not isinstance(data["info"][field], str):
            raise ContractInputError(f"Поле info.{field} должно быть строкой.")
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


def _load_contract(path: Path | str) -> dict[str, Any]:
    return load_contract_document(path).data


def _parse_contract(text: str) -> dict[str, Any]:
    """Parse JSON or safe YAML, validate consumed fields and expand local refs."""

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
        contract_path: Путь или HTTP(S) URL OpenAPI/Swagger-контракта.

    Returns:
        Краткое структурированное описание контракта.

    Raises:
        FileNotFoundError: если файл не существует.
    """

    document = load_contract_document(contract_path)
    return summarize_contract(document.data, document.source)


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


def save_contract_snapshot(document: ContractDocument, directory: Path) -> dict[str, Any]:
    """Keep the exact input for storage/replay; report the original source."""
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / "contract.yaml"
    snapshot.write_bytes(document.raw)
    result = {**summarize_contract(document.data, document.source),
              "sha256": hashlib.sha256(document.raw).hexdigest(), "snapshot_path": str(snapshot.resolve())}
    if document.resolved_url:
        result["resolved_url"] = document.resolved_url
    return result
