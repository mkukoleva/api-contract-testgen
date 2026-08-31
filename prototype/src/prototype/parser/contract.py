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
        ValueError: если корневой элемент контракта не является объектом.
    """

    text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise ValueError(
            "API-контракт должен содержать объект JSON/YAML."
        )

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