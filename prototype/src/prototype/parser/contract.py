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
import math
from datetime import date, datetime
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
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
MAX_EXTERNAL_REF_DOCUMENTS = 16


class _ContractRedirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        normalize_location(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_contract_bytes(url: str) -> tuple[bytes, str]:
    """Fetch one HTTP(S) document with bounded size and time; return (raw, resolved_url).

    External $ref documents are fetched through the same bounded path as the
    main contract: no proxy, five redirects, 5 MiB and 15 s per document.
    Error text never includes URLs, query secrets or exception headers.
    """
    opener = build_opener(ProxyHandler({}), _ContractRedirects())
    deadline = time.monotonic() + CONTRACT_DOWNLOAD_TIMEOUT
    try:
        with opener.open(Request(url, headers={"Accept": "application/json, application/yaml, text/yaml"}),
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
    return raw, resolved_url


def load_contract_document(location: Path | str) -> ContractDocument:
    """Read a file or bounded HTTP(S) response once and resolve external $refs.

    Document-local $refs are expanded without I/O; external http(s) $refs are
    fetched through the same bounded download path (see
    resolve_external_refs) and expanded at load time, so the render and
    repair paths that only read the parsed document never touch the network.
    """
    location = normalize_location(location)
    resolved_url = None
    if isinstance(location, Path):
        if location.stat().st_size > MAX_CONTRACT_BYTES:
            raise ContractInputError("Контракт превышает лимит 5 MiB.")
        with location.open("rb") as stream:
            raw = stream.read(MAX_CONTRACT_BYTES + 1)
    else:
        raw, resolved_url = _download_contract_bytes(location)
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ContractInputError("Контракт превышает лимит 5 MiB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ContractInputError("Контракт должен быть в кодировке UTF-8.") from exc
    try:
        data, external_refs = _parse_contract(text)
    except RecursionError as exc:
        raise ContractInputError("Слишком глубокая структура JSON/YAML контракта.") from exc
    return ContractDocument(str(location), raw, data, resolved_url, tuple(external_refs))


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
                if "responses" in operation and not isinstance(operation["responses"], dict):
                    raise ContractInputError(f"Поле responses операции {method.upper()} {route} должно быть объектом.")
                operations += 1
    if require_operations and not operations:
        raise ContractInputError("Раздел paths должен содержать хотя бы одну HTTP-операцию.")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Follow an RFC 6901 JSON pointer inside a parsed document.

    ``pointer`` is the fragment after ``#``: the leading-slash form of
    external refs (``/components/...``) and the bare local form
    (``#/components/...`` → ``components/...``) are both accepted. An empty
    pointer selects the whole document. YAML permits unquoted integer keys
    (response codes); their canonical string representation is used in
    pointers, so an unmatched numeric token is tried as an int key.
    """
    tokens = unquote(pointer).split("/")
    if tokens and tokens[0] == "":
        tokens = tokens[1:]
    value = document
    try:
        for token in tokens:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("invalid JSON pointer array index")
                value = value[int(token)]
            else:
                key = int(token) if token not in value and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", token) else token
                value = value[key]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ContractInputError(f"Не удалось разрешить $ref: {pointer or '(пустой указатель)'}") from exc
    return value


def _split_external_ref(ref: str) -> tuple[str, str]:
    """Split an external $ref into (base URL, JSON pointer fragment)."""
    base, _, pointer = ref.partition("#")
    return base, pointer


def normalize_contract(data: dict[str, Any]) -> dict[str, Any]:
    """Expand document-local JSON pointers without I/O; retain recursive/external refs.

    This is context preparation, not a full OpenAPI/JSON Schema validator.
    Expansion is bounded to prevent recursive or heavily shared schemas
    exhausting the orchestrator. The original input is never mutated.
    External http(s) refs stay as $ref here and are expanded by
    resolve_external_refs at load time.
    """
    visited = 0

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
        if isinstance(value, float) and not math.isfinite(value):
            raise ContractInputError("JSON/YAML контракт не допускает NaN и Infinity.")
        if not isinstance(value, dict):
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ContractInputError("Значения YAML контракта должны быть совместимы с JSON.")
            return value
        normalized_keys = {}
        for key, item in value.items():
            if not isinstance(key, str) and type(key) is not int:
                raise ContractInputError("Ключи объектов YAML должны быть строками или целыми числами.")
            key = str(key)
            if key in normalized_keys:
                raise ContractInputError("Ключи объекта YAML совпадают после преобразования в строки.")
            normalized_keys[key] = item
        value = normalized_keys
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
                target = expand(_resolve_pointer(data, ref[2:]), (*active, ref), depth + 1)
                if isinstance(target, dict):
                    return {**target, **siblings}
                if not siblings:
                    return target
            return {"$ref": ref, **siblings}
        return {key: child(key, item) for key, item in value.items()}

    return expand(data)


def _parse_reference_document(text: str) -> dict[str, Any]:
    """Parse one external $ref document: JSON/YAML top-level object only."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ContractInputError("Внешний $ref-документ: не удалось разобрать JSON/YAML.") from exc
    if not isinstance(data, dict):
        raise ContractInputError("Внешний $ref-документ должен содержать объект JSON/YAML.")
    return data


def resolve_external_refs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Expand external http(s) $refs of a locally normalized document.

    Returns (document, resolved URLs fetched) in fetch order. Runs once at
    load time, so the render and repair paths that read the parsed document
    never touch the network. Each distinct external document is fetched once
    (cached) through the same bounded download as the main contract and is
    locally normalized before expansion, so only external refs can remain in
    it. Bounded by MAX_EXTERNAL_REF_DOCUMENTS documents and the same depth /
    expansion budgets as local refs; a ref already on the active expansion
    chain keeps its $ref marker (same as recursive local refs). Non http(s)
    ref schemes and unavailable documents are input errors.
    """
    cache: dict[str, dict[str, Any]] = {}
    fetched: list[str] = []

    def fetch(base_url: str) -> dict[str, Any]:
        if base_url in cache:
            return cache[base_url]
        if len(cache) >= MAX_EXTERNAL_REF_DOCUMENTS:
            raise ContractInputError(
                f"Превышен лимит внешних $ref-документов: {MAX_EXTERNAL_REF_DOCUMENTS}.")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContractInputError("Внешний $ref должен быть HTTP(S) URL.")
        raw, resolved_url = _download_contract_bytes(base_url)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ContractInputError("Внешний $ref-документ должен быть в кодировке UTF-8.") from exc
        try:
            document = normalize_contract(_parse_reference_document(text))
        except RecursionError as exc:
            raise ContractInputError("Слишком глубокая структура внешнего $ref-документа.") from exc
        cache[base_url] = document
        fetched.append(resolved_url)
        return document

    visited = 0
    swagger = "swagger" in data
    mapping_fields = {"properties", "patternProperties", "dependentSchemas", "schemas", "definitions",
                      "responses", "headers", "links", "parameters", "requestBodies", "pathItems",
                      "callbacks", "securitySchemes", "examples"}

    def expand(value, active=(), depth=0, *, mapping=False, literal=False, swagger_flag=None):
        nonlocal visited
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ContractInputError("Слишком сложный контракт: превышен лимит раскрытия $ref.")
        if swagger_flag is None:
            swagger_flag = swagger
        if isinstance(value, list):
            return [expand(item, active, depth + 1, literal=literal, swagger_flag=swagger_flag) for item in value]
        if not isinstance(value, dict):
            return value
        def child(key, item):
            # Example/default/enum values are API data, not Reference Objects.
            is_literal = literal or (not mapping and (key in {"example", "default", "enum", "const", "value"}
                or (key == "examples" and (isinstance(item, list) or swagger_flag))))
            return expand(item, active, depth + 1,
                          mapping=not mapping and key in mapping_fields and isinstance(item, dict),
                          literal=is_literal, swagger_flag=swagger_flag)
        ref = value.get("$ref")
        if not mapping and not literal and isinstance(ref, str) and not ref.startswith("#/"):
            base_url, pointer = _split_external_ref(ref)
            if ref not in active:
                document = fetch(base_url)
                target = _resolve_pointer(document, pointer)
                siblings = {key: child(key, item) for key, item in value.items() if key != "$ref"}
                expanded_target = expand(target, (*active, ref), depth + 1,
                                         swagger_flag="swagger" in document)
                if isinstance(target, dict):
                    return {**expanded_target, **siblings}
                if not siblings:
                    return expanded_target
            return {"$ref": ref, **{key: child(key, item) for key, item in value.items() if key != "$ref"}}
        return {key: child(key, item) for key, item in value.items()}

    return expand(data), fetched


def _load_contract(path: Path | str) -> dict[str, Any]:
    return load_contract_document(path).data


def _parse_contract(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse JSON or safe YAML, validate consumed fields and expand refs.

    Returns (document, resolved URLs of fetched external $ref documents).
    """

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
    resolved, external_refs = resolve_external_refs(normalized)
    _validate_structure(resolved)
    return resolved, external_refs


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
              "sha256": hashlib.sha256(document.raw).hexdigest(), "snapshot_path": str(snapshot.resolve()),
              "external_refs": list(document.external_refs)}
    if document.resolved_url:
        result["resolved_url"] = document.resolved_url
    return result
