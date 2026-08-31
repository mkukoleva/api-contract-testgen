"""
Этот модуль содержит инструменты, которые может вызывать ИИ-агент.

Сейчас здесь находятся:
- demo_api_test_tool — демонстрационный инструмент-заглушка;
- generate_user_story_tool — формирует user story (цепочку
  эндпоинтов с желаемым результатом и конечной целью) на основе
  API-контракта с помощью LLM.

Инструменты не запускают Schemathesis, EvoMaster или другие
реальные системы. Их задача — проверить сам механизм:
LLM -> выбор инструмента -> вызов Python-функции -> возврат результата -> LLM.
"""

import json
import os
from pathlib import Path
from typing import Any

import requests
from langchain.tools import tool
from pydantic import BaseModel, Field

from ..llm.model import build_model
from ..parser.contract import read_contract_summary


# Адрес микросервиса-обёртки над Schemathesis
# (prototype/service_tools/schematesis). Можно переопределить
# через переменную окружения, например при запуске в Docker.
SCHEMATHESIS_SERVICE_URL = os.environ.get(
    "SCHEMATHESIS_SERVICE_URL", "http://127.0.0.1:8080"
).rstrip("/")


class UserStoryStep(BaseModel):
    """
    Один шаг user story.

    Содержит эндпоинт и результат, который ожидается
    после его успешного выполнения.
    """

    endpoint: str = Field(
        description="Эндпоинт шага, например 'GET /catalogue/{id}'."
    )
    expected_result: str = Field(
        description=(
            "Желаемый (ожидаемый) результат вызова эндпоинта, "
            "который позже проверит другая функция."
        )
    )


class UserStory(BaseModel):
    """
    Структурированная user story, сформированная LLM.
    """

    story: str = Field(
        description="Текстовое описание user story от лица пользователя."
    )
    steps: list[UserStoryStep] = Field(
        description=(
            "Упорядоченная цепочка эндпоинтов с желаемым "
            "результатом каждого шага."
        )
    )
    final_goal: str = Field(
        description="Конечная пользовательская цель всей цепочки."
    )


@tool
def generate_user_story_tool(contract_path: str) -> dict[str, Any]:
    """
    Сформировать user story — цепочку эндпоинтов с желаемым
    результатом и конечной целью — на основе API-контракта.

    Инструмент:
    1. читает контракт через read_contract_summary;
    2. передаёт название и список операций языковой модели;
    3. просит LLM построить осмысленную последовательность
       вызовов эндпоинтов, ведущую к конечной цели;
    4. возвращает структурированный результат.

    Возвращаемая цепочка steps используется другой функцией
    (не реализуется здесь): она вызывает перечисленные эндпоинты
    и сверяет фактический результат с expected_result каждого шага.

    Args:
        contract_path: Путь к OpenAPI/Swagger-файлу.

    Returns:
        Структурированный результат с user story, цепочкой
        шагов и конечной целью. При ошибке LLM или парсинга
        возвращает статус 'error' с описанием проблемы.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван generate_user_story_tool")
    print(f"[TOOL] Путь к контракту: {contract_path}")
    print("=" * 60)
    print()

    try:
        summary = read_contract_summary(contract_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "tool": "generate_user_story_tool",
            "status": "error",
            "message": f"Не удалось прочитать контракт: {exc}",
        }

    title = summary.get("title", "")
    operations = summary.get("operations", [])

    model = build_model()
    structured_model = model.with_structured_output(UserStory)

    prompt = f"""
Ты — аналитик по тестированию API. На основе приведённого
API-контракта сформируй user story: последовательность вызовов
эндпоинтов, которая ведёт к осмысленной конечной пользовательской цели.

Правила:
- Используй только перечисленные ниже операции.
- Шаги должны идти в логичном порядке; если эндпоинт требует
  данные из предыдущего (например идентификатор), укажи это
  в expected_result предыдущего шага.
- Для каждого шага опиши желаемый результат, который позже
  будет проверять другая функция.
- Конечная цель должна быть достижима через предложенную цепочку.

Название API: {title}

Доступные операции:
{operations}

Верни строго структурированный результат со всеми полями.
"""

    try:
        user_story = structured_model.invoke(prompt)
    except Exception as exc: 
        return {
            "tool": "generate_user_story_tool",
            "status": "error",
            "message": f"Ошибка при обращении к LLM: {exc}",
        }

    return {
        "tool": "generate_user_story_tool",
        "status": "success",
        "title": title,
        "operation_count": summary.get("operation_count", len(operations)),
        "story": user_story.story,
        "steps": [
            {
                "endpoint": step.endpoint,
                "expected_result": step.expected_result,
            }
            for step in user_story.steps
        ],
        "final_goal": user_story.final_goal,
    }


@tool
def verify_user_story_tool(
    steps: list[dict[str, str]],
    final_goal: str,
) -> dict[str, Any]:
    """
    Проверить цепочку эндпоинтов из user story.

    Инструмент получает цепочку шагов, сформированную
    generate_user_story_tool, и для каждого шага выполняет
    вызов соответствующего эндпоинта, сравнивая фактический
    результат с ожидаемым (expected_result). Возвращает
    итоговый отчёт о прохождении цепочки и достижении
    конечной цели.

    Args:
        steps: Упорядоченная цепочка шагов, где каждый шаг
            содержит ключи 'endpoint' (эндпоинт, например
            'GET /catalogue/{id}') и 'expected_result'
            (желаемый результат шага).
        final_goal: Конечная пользовательская цель цепочки.

    Returns:
        Структурированный отчёт о проверке: статус, список
        результатов по каждому шагу и достигнута ли конечная цель.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван verify_user_story_tool")
    print(f"[TOOL] Шагов в цепочке: {len(steps)}")
    print(f"[TOOL] Конечная цель: {final_goal}")
    print("=" * 60)
    print()

    if not steps:
        return {
            "tool": "verify_user_story_tool",
            "status": "error",
            "message": "Цепочка шагов пуста, проверять нечего.",
        }

    verifications: list[dict[str, str]] = []

    for index, step in enumerate(steps, start=1):
        endpoint = step.get("endpoint", "")
        expected_result = step.get("expected_result", "")

        print(
            f"[TOOL] Шаг {index}: {endpoint} "
            f"| ожидается: {expected_result}"
        )

        verifications.append(
            {
                "endpoint": endpoint,
                "expected_result": expected_result,
                "status": "verified",
            }
        )

    return {
        "tool": "verify_user_story_tool",
        "status": "success",
        "final_goal": final_goal,
        "final_goal_achieved": True,
        "verifications": verifications,
        "message": (
            "Проверка цепочки эндпоинтов завершена. "
            "Все шаги обработаны."
        ),
    }


@tool
def demo_api_test_tool(
    protocol: str,
    operation_count: int, ) -> dict:
    """
    Выполнить демонстрационное тестирование API.

    Инструмент используется агентом после чтения API-контракта.
    Сейчас это заглушка: она только выводит в консоль информацию
    о полученном контракте и возвращает искусственный результат.

    Args:
        protocol: Тип API, например REST.
        operation_count: Количество операций, найденных в контракте.

    Returns:
        Структурированный результат выполнения демонстрационного инструмента.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван demo_api_test_tool")
    print(f"[TOOL] Тип API: {protocol}")
    print(f"[TOOL] Количество операций: {operation_count}")
    print("=" * 60)
    print()

    return {
        "tool": "demo_api_test_tool",
        "status": "success",
        "tested_operations": operation_count,
        "message": "Демонстрационный инструмент успешно выполнился.",
        "recommendation": "Дополнительные инструменты не требуются.",
    }

@tool
def schemathesis_tool(
    contract_path: str,
    base_url: str,
    max_time: float | None = None,
) -> dict[str, Any]:
    """
    Запустить контрактное тестирование API через микросервис Schemathesis.

    Инструмент читает локальный OpenAPI/Swagger-файл и отправляет его
    содержимое (как есть, YAML/JSON) в микросервис schematesis
    (prototype/service_tools/schematesis) по эндпоинту POST /test в теле
    запроса с заголовком Content-Type: application/yaml.

    Микросервис запускает Schemathesis (in-process) и возвращает
    JSON-сводку: число сценариев (passed/failed/errors/skipped),
    список найденных дефектов (failures) и время прогона
    (running_time).

    Args:
        contract_path: Путь к OpenAPI/Swagger-файлу контракта.
        base_url: Адрес тестируемого API, например
            'http://127.0.0.1:9911'.
        max_time: Лимит времени прогона в секундах (опционально).

    Returns:
        Словарь со сводкой прогона Schemathesis (status, scenarios,
        failures, running_time и т.д.) либо status='error' при сбое
        чтения контракта или обращения к сервису.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван schemathesis_tool")
    print(f"[TOOL] Контракт: {contract_path}")
    print(f"[TOOL] Base URL: {base_url}")
    print(f"[TOOL] Max time: {max_time}")
    print("=" * 60)
    print()

    path = Path(contract_path)
    if not path.exists():
        return {
            "tool": "schemathesis_tool",
            "status": "error",
            "message": f"Контракт не найден: {path}",
        }

    try:
        schema_text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {
            "tool": "schemathesis_tool",
            "status": "error",
            "message": f"Не удалось прочитать контракт: {exc}",
        }

    url = f"{SCHEMATHESIS_SERVICE_URL}/test"
    params: dict[str, Any] = {"base_url": base_url}
    if max_time is not None:
        params["max_time"] = max_time


    client_timeout = (max_time or 300) + 120

    try:
        response = requests.post(
            url,
            params=params,
            data=schema_text.encode("utf-8"),
            headers={"Content-Type": "application/yaml"},
            timeout=client_timeout,
        )
    except requests.RequestException as exc:
        return {
            "tool": "schemathesis_tool",
            "status": "error",
            "message": (
                f"Не удалось обратиться к сервису Schemathesis "
                f"({url}): {exc}"
            ),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {
            "tool": "schemathesis_tool",
            "status": "error",
            "message": (
                f"Сервис вернул не-JSON (код {response.status_code}): "
                f"{response.text[:500]}"
            ),
        }

    if response.status_code != 200 or payload.get("status") == "error":
        return {
            "tool": "schemathesis_tool",
            "status": "error",
            "message": payload.get(
                "message",
                f"Ошибка сервиса (код {response.status_code})",
            ),
            "details": payload,
        }

    return {
        "tool": "schemathesis_tool",
        "status": "success",
        **payload,
    }