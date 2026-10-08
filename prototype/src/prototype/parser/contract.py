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
import re
from pathlib import Path
from typing import Any

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


def _validate_structure(data: dict[str, Any]) -> None:
    """Check the fields this parser consumes, not the full OpenAPI schema."""
    if "openapi" in data and "swagger" in data:
        raise ContractInputError("Укажите только одну спецификацию: OpenAPI или Swagger.")
    if "openapi" in data:
        version = data["openapi"]
        if not isinstance(version, str) or not re.fullmatch(r"3\.\d+\.\d+(?:[-+][\w.-]+)?", version):
            raise ContractInputError("Поддерживается OpenAPI 3.x (например, 3.0.3).")
    elif data.get("swagger") != "2.0":
        raise ContractInputError("Контракт должен содержать OpenAPI 3.x или Swagger 2.0.")
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

    _validate_structure(data)
    return data


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
        "file": str(path),
        "protocol": protocol,
        "specification": specification,
        "title": info.get("title", ""),
        "operation_count": len(operations),
        "operations": operations,
    }
