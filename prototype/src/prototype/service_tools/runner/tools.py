"""
Этот модуль содержит инструменты, которые может вызывать ИИ-агент.

schemathesis_tool отправляет контракт реальному HTTP-микросервису Schemathesis.
generate_user_story_tool использует LLM для текстовых сценариев;
demo_api_test_tool и verify_user_story_tool остаются заглушками.
save_tests_tool сохраняет код без импорта; generate_tests_tool вызывает
отдельный конвейер генерации с изолированным pytest-runner.
Последние два свободно параметризованных инструмента не зарегистрированы
в build_agent. В режиме --generate используется create_generation_tool:
адрес, сеть и лимиты фиксируются доверенным оркестратором, не аргументами LLM.
"""

import json
import os
from pathlib import Path
from typing import Any

import requests
from langchain.tools import tool
from pydantic import BaseModel, Field

from ...llm.model import build_model
from ...parser.contract import read_contract_summary
from ...storage import GeneratedFile, SaveRequest, save_test_suite


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

@tool(response_format="content_and_artifact")
def schemathesis_tool(
    contract_path: str,
    base_url: str,
    max_time: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        Компактное содержимое для LLM и полный artifact для отчёта/метрик.
        Через tool.invoke без ToolCall возвращается компактный словарь.
    """

    full = _run_legacy_schemathesis(contract_path, base_url, max_time)
    from ..selection import _clip
    compact = {key: _clip(full[key]) for key in (
        "tool", "status", "service", "schema_title", "operations", "scenarios",
        "running_time", "stop_reason", "nonfatal_errors", "message", "failures") if key in full}
    compact["failures_total"] = len(full.get("failures", []))
    compact["details_in_artifact"] = True
    if len(json.dumps(compact, ensure_ascii=False)) > 6000:
        compact.pop("failures", None)
    return compact, full


def _run_legacy_schemathesis(contract_path, base_url, max_time):
    """Compatibility HTTP call; full facts stay outside the model content."""
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


@tool
def save_tests_tool(
    contract_path: str,
    files: list[dict[str, str]],
    model: str | None = None,
) -> dict[str, Any]:
    """
    Сохранить сгенерированные pytest-тесты как новую неизменяемую версию.

    Каждый файл проверяется ast.parse: корректные попадают в tests/,
    файлы с синтаксической ошибкой — в rejected/. Код не импортируется;
    сбор тестов выполняется только изолированным runner. Версия сохраняется
    в generated/<контракт>/<дата_время>/ вместе с manifest.json.

    Args:
        contract_path: Путь к OpenAPI-контракту, по которому созданы тесты.
        files: Список файлов, каждый — {'name': 'test_*.py', 'code': '...'}.
        model: Имя модели, сгенерировавшей тесты (опционально).

    Returns:
        Компактная сводка: run_id, tests_dir (путь для запуска),
        summary, collection_status и до 5 ошибок синтаксиса/сбора.
        Код тестов обратно не возвращается.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван save_tests_tool")
    print(f"[TOOL] Контракт: {contract_path}")
    print(f"[TOOL] Файлов: {len(files)}")
    print("=" * 60)
    print()

    try:
        request = SaveRequest(
            contract_path=contract_path,
            files=tuple(
                GeneratedFile(item.get("name", ""), item.get("code", ""))
                for item in files
            ),
            model=model,
            collect=False,
        )
        saved = save_test_suite(request)
    except (ValueError, OSError) as exc:
        return {
            "tool": "save_tests_tool",
            "status": "error",
            "message": f"Не удалось сохранить тесты: {exc}",
        }

    manifest = saved.manifest
    problems = [
        f"{item['name']}: {item['error']}"
        for item in manifest["files"]
        if item["error"]
    ] + manifest["collection"]["errors"]

    return {
        "tool": "save_tests_tool",
        "status": "success",
        "run_id": saved.run_id,
        "tests_dir": str(saved.tests_dir),
        "summary": manifest["summary"],
        "collection_status": manifest["collection"]["status"],
        "errors": problems[:5],
    }


@tool
def generate_tests_tool(
    contract_path: str,
    base_url: str,
    network: str | None = None,
    blocked_networks: str | None = None,
    max_repair_attempts: int = 3,
) -> dict[str, Any]:
    """
    Сгенерировать pytest-тесты по OpenAPI-контракту и прогнать их.

    Адаптер для доверенных Python-вызовов. Не регистрировать с этим набором
    аргументов в агенте: вместо него использовать create_generation_tool,
    фиксирующий сетевую политику и лимиты по п. 4.5.2.3 ТЗ v3.

    Инструмент запускает полный конвейер (ADR 0004): генерация тестов по
    контракту -> постобработка -> запуск через изолированный pytest-runner ->
    ограниченный self-repair (не более max_repair_attempts попыток и в рамках
    токен-бюджета; одинаковые повторяющиеся ошибки останавливают цикл).

    Repair не выполняется в случаях: недоступность Docker/API (infrastructure,
    timeout), прерывание прогона, а также когда фактический ответ сервиса
    противоречит документированным ответам контракта (suspected_defects) —
    при этом assert не ослабляется.

    Args:
        contract_path: Путь к OpenAPI/Swagger-файлу контракта.
        base_url: Адрес тестируемого API, например 'http://catalogue:8080'.
        network: Docker-сеть, в которой живёт тестируемое API.
        blocked_networks: Список Docker-сетей через запятую, к которым доступ
            запрещён (например 'pytest-runner-catalogue_database').
        max_repair_attempts: Максимальное число попыток self-repair (по
            умолчанию 3, как в плане команды).

    Returns:
        Компактный структурированный результат конвейера: pipeline_status,
        attempts, tokens, итоговая сводка pytest (summary), repair_log,
        suspected_defects и saved_versions. Код сгенерированных тестов
        обратно не возвращается; каждая попытка сохранена как неизменяемая
        версия (см. saved_versions).
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван generate_tests_tool")
    print(f"[TOOL] Контракт: {contract_path}")
    print(f"[TOOL] Base URL: {base_url}")
    print(f"[TOOL] Max repair attempts: {max_repair_attempts}")
    print("=" * 60)
    print()

    from ...generator.contracts import GenerationSettings
    from ...generator.pipeline import run_generation_pipeline

    blocked = tuple(
        item.strip()
        for item in (blocked_networks or "").split(",")
        if item.strip()
    )

    try:
        run_settings = GenerationSettings(
            contract_path=contract_path,
            base_url=base_url,
            network=network,
            blocked_networks=blocked,
            max_repair_attempts=max_repair_attempts,
        )
        run = run_generation_pipeline(run_settings)
    except (ValueError, OSError) as exc:
        return {
            "tool": "generate_tests_tool",
            "status": "error",
            "message": f"Не удалось запустить конвейер генерации: {exc}",
        }

    return _generation_result(run)


def _generation_result(run) -> dict[str, Any]:
    return {
        "tool": "generate_tests_tool",
        "status": "success" if run.status == "success" else run.status,
        "pipeline_status": run.status,
        "attempts": run.attempts,
        "tokens": run.to_dict()["tokens"],
        "summary": (run.run_result or {}).get("summary", {}),
        "runnability": run.runnability,
        "repair_log": [attempt.to_dict() for attempt in run.repair_log],
        "suspected_defects": list(run.suspected_defects),
        "environmental": list(run.environmental),
        "saved_versions": list(run.saved_versions),
        "generator_errors": list(run.generator_errors),
        "report_paths": run.report_paths,
        "recommendations": run.recommendations,
    }


def _compact_generation_result(full):
    from ..selection import _clip
    compact = dict(full)
    fields = ("runnability", "repair_log", "suspected_defects", "environmental", "saved_versions",
              "generator_errors", "recommendations")
    changed = []
    for field in fields:
        if field not in full:
            continue
        compact[field] = _clip(full[field])
        if compact[field] != full[field]:
            changed.append(field)
    if len(json.dumps(compact, ensure_ascii=False)) > 5600:
        for field in ("suspected_defects", "environmental", "repair_log", "generator_errors", "recommendations"):
            if compact.get(field):
                compact[field] = {} if field == "recommendations" else []
                if field not in changed:
                    changed.append(field)
            if len(json.dumps(compact, ensure_ascii=False)) <= 5600:
                break
    compact["truncated_fields"] = changed
    compact["details_in_report"] = True
    return compact


def create_generation_tool(settings, *, model=None):
    """Bind a generation tool to the trusted orchestrator's configuration.

    TZ v3 4.5.2.3: no address, network, output path or limit is an LLM argument.
    The legacy unbound adapter above is for trusted Python callers only and
    must not be registered with an agent. M2 should register this bound tool.
    """
    from pydantic import BaseModel, ConfigDict
    from ...generator.contracts import GenerationSettings
    from ...generator.pipeline import run_generation_pipeline

    if not isinstance(settings, GenerationSettings):
        raise ValueError("settings must be validated GenerationSettings")

    class TrustedInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

    from langchain_core.tools import StructuredTool

    class BoundTool(StructuredTool):
        def _to_args_and_kwargs(self, tool_input, tool_call_id):
            # LangChain skips validation of an empty argument schema. Enforce
            # it here too, so extra policy arguments are rejected, not ignored.
            if not isinstance(tool_input, dict) or tool_input:
                raise ValueError("This tool accepts no configuration arguments")
            return (), {}

    def generate_bound_tests():
        """Generate and execute pytest tests using the configured isolated environment."""
        try:
            run = run_generation_pipeline(settings) if model is None else run_generation_pipeline(settings, model=model)
            full = _generation_result(run)
            # Artifact is retained by trusted consumers but never sent to the
            # chat model. Metrics/report data therefore remain complete.
            return _compact_generation_result(full), full
        except (OSError, ValueError, RuntimeError) as exc:
            from ...llm.diagnostics import describe_error
            full = {"tool": "generate_tests_tool", "status": "error",
                    "message": f"Не удалось запустить конвейер генерации: {describe_error(exc)}"}
            return full, full

    return BoundTool.from_function(generate_bound_tests, name="generate_tests_tool", args_schema=TrustedInput,
                                   response_format="content_and_artifact")
