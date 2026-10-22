"""
Главный файл прототипа ИИ-агента.

Здесь собирается полный цикл:

1. Python читает API-контракт.
2. Из контракта создаётся короткое summary.
3. Summary передаётся LLM.
4. LLM выбирает доступный tool.
5. LangChain вызывает выбранный Python-tool.
6. Результат tool возвращается LLM.
7. LLM анализирует результат.
8. LLM либо выбирает следующий tool, либо завершает работу.
9. Рассчитываются метрики качества генерации.
10. Результаты сохраняются в отчёты.

Доступны инструменты:
- Schemathesis;
- демонстрационная проверка API;
- генерация user story;
- проверка user story.
В режиме --generate используется только инструмент pytest-конвейера
с настройками, зафиксированными доверенным CLI.
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SYSTEM_PROMPT = """
Ты являешься ядром автоматизированного агента тестирования API.

Твой рабочий цикл:

1. Получить краткое описание API-контракта.
2. Определить, какой доступный инструмент нужно использовать.
3. Вызвать выбранный инструмент.
4. Получить и проанализировать его результат.
5. Решить, требуется ли запуск следующего инструмента.
6. Если задача выполнена — завершить работу.

Правила:

- Не придумывай информацию, которой нет в описании контракта.
- Используй доступные инструменты, когда это требуется задачей.
- Не вызывай один и тот же инструмент повторно без причины.

Порядок работы при демонстрации тестирования API:

1. Сначала вызови schemathesis_tool, чтобы выполнить реальное
   контрактное тестирование API через микросервис Schemathesis.
   Передай ему:
   - contract_path — путь к контракту (возьми из поля "file"
     краткого описания контракта);
   - base_url — адрес тестируемого API, всегда
     "http://catalogue:8080" (из контейнера Schemathesis в API-сети стенда).
   После успешного выполнения повторно его не вызывай.
2. Затем вызови demo_api_test_tool, чтобы продемонстрировать
   тестирование API. После успешного выполнения повторно его не вызывай.
3. Далее вызови generate_user_story_tool, передав ему путь к контракту,
   чтобы сформировать user story — цепочку эндпоинтов с желаемым
   результатом и конечной целью.
4. Далее вызови verify_user_story_tool, передав ему steps и final_goal,
   полученные от generate_user_story_tool, чтобы проверить цепочку.
5. После выполнения всех шагов кратко объясни результат
   и заверши работу.
"""


REPORTS_DIR = Path(__file__).resolve().parent / "reports"
PYTEST_SYSTEM_PROMPT = (
    "Ты — ядро агента запуска настроенного конвейера pytest. "
    "Вызови generate_tests_tool ровно один раз с пустыми аргументами. "
    "Результат инструмента завершает запуск; статус, результаты pytest "
    "и пути отчётов выводит оркестратор. "
    "Адрес, сеть, лимиты и каталог задаёт доверенный оркестратор."
)


def _record_model(agent, model):
    from .llm.model import describe_model
    agent._prototype_model_metadata = describe_model(model)
    return agent


def testing_system_prompt(settings):
    from .service_tools.selection import select_backend
    backend = select_backend(settings.intent)
    return (f"Запрос пользователя: {settings.intent}. Вызови {backend}_tool один раз с пустыми аргументами. "
            "Доверенный оркестратор выбрал инструмент: Schemathesis для быстрой проверки, "
            "EvoMaster для глубокого поиска, Microcks для моков и multi-protocol. "
            "Сообщи фактический статус и рекомендации из результата. Ошибка или неполный прогон не являются успехом. "
            "Подробности находятся в сохранённом отчёте; не меняй контракт и проверки ради зелёного результата.")


def build_agent(*, generation_settings=None, testing_settings=None):
    """
    Создать LangChain-агента.
    """

    from langchain.agents import create_agent

    from .llm.model import build_model
    from .service_tools.runner.tools import (
        demo_api_test_tool,
        generate_user_story_tool,
        schemathesis_tool,
        verify_user_story_tool,
    )

    model = build_model()

    if testing_settings is not None:
        from .service_tools.selection import create_selected_tool
        return _record_model(create_agent(model=model, tools=[create_selected_tool(testing_settings)],
            system_prompt=testing_system_prompt(testing_settings)), model)

    if generation_settings is not None:
        from .service_tools.runner.tools import create_generation_tool
        from langchain.agents.middleware import wrap_model_call
        from langchain_core.messages import ToolMessage

        @wrap_model_call
        def retry_generation_arguments(request, handler):
            response = handler(request)
            calls = [call for message in response.result
                     for call in getattr(message, "tool_calls", [])]
            if not any(call["name"] == "generate_tests_tool" and call.get("args") != {}
                       for call in calls):
                return response
            # Reject the whole proposed batch before execution. Ask once for
            # a new call; never strip arguments or apply model-supplied policy.
            feedback = [ToolMessage(
                content="Call rejected; no tool executed. Call generate_tests_tool exactly once with args {}. Configuration is fixed by the orchestrator.",
                tool_call_id=call["id"], status="error") for call in calls]
            corrected = handler(request.override(
                messages=[*request.messages, *response.result, *feedback]))
            corrected_calls = [call for message in corrected.result
                               for call in getattr(message, "tool_calls", [])]
            if (len(corrected_calls) != 1 or corrected_calls[0]["name"] != "generate_tests_tool"
                    or corrected_calls[0].get("args") != {}):
                raise ValueError("Generation tool call still invalid after one correction; expected generate_tests_tool with args {}")
            return corrected

        generation_tool = create_generation_tool(generation_settings, model=model)
        # The pipeline already performs generation, repair and reporting.
        # A second agent completion can repeatedly call the cached tool.
        generation_tool.return_direct = True
        return _record_model(create_agent(
            model=model,
            tools=[generation_tool],
            system_prompt=PYTEST_SYSTEM_PROMPT,
            middleware=[retry_generation_arguments],
        ).with_config(recursion_limit=8), model)

    return _record_model(create_agent(
        model=model,
        tools=[
            schemathesis_tool,
            demo_api_test_tool,
            generate_user_story_tool,
            verify_user_story_tool,
        ],
        system_prompt=SYSTEM_PROMPT,
    ), model)


def _extract_final_message(result) -> str:
    """
    Получить итоговое сообщение агента.
    """

    messages = result.get("messages", [])

    if not messages:
        return "Агент не вернул сообщений."

    final_message = messages[-1]

    content = getattr(final_message, "content", "")

    if isinstance(content, str):
        return content

    return json.dumps(
        content,
        ensure_ascii=False,
        indent=2,
    )


def _append_metrics_to_report(
    report_path: Path,
    metrics_markdown: str,
) -> None:
    """
    Добавить рассчитанные метрики в существующий Markdown-отчёт.
    """

    with report_path.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write("\n\n")
        file.write("---\n\n")
        file.write(metrics_markdown)


def _report_input_error(exc, report_dir=None, *, inspect=False):
    """Persist early failure without raw input/URLs, model calls or invented facts."""
    import html
    import sys
    from .llm.diagnostics import describe_error
    root = Path(report_dir) if report_dir is not None else (Path.cwd() / ".contract-runs" if inspect else REPORTS_DIR)
    directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex)
    payload = {"status": "input_error", "stage": "contract_input", "error": describe_error(exc)}
    try:
        directory.mkdir(parents=True, exist_ok=False)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        (directory / "input-error.json").write_text(text, encoding="utf-8")
        (directory / "input-error.md").write_text(
            "# Ошибка загрузки входных данных\n\n<pre>" + html.escape(text) + "</pre>\n", encoding="utf-8")
        print(f"[CORE] Диагностика входных данных: {directory}", file=sys.stderr)
    except (OSError, UnicodeError) as report_error:
        print(f"[CORE] Не удалось сохранить отчёт об ошибке входа: {describe_error(report_error)}", file=sys.stderr)


def _load_inputs(contract_path, source_code_path, report_dir, *, inspect=False):
    from .parser.contract import ContractInputError, load_contract_document
    from .parser.source_context import read_source_context
    try:
        document = load_contract_document(contract_path)
        return document, read_source_context(source_code_path, document.data)
    except (ContractInputError, OSError, UnicodeError) as exc:
        _report_input_error(exc, report_dir, inspect=inspect)
        raise


def inspect_contract(contract_path: str, *, report_dir=None, source_code_path=None) -> None:
    """Validate input and persist its identity without creating an LLM client."""
    from .parser.contract import save_contract_snapshot
    document, context = _load_inputs(contract_path, source_code_path, report_dir, inspect=True)
    root = Path(report_dir) if report_dir is not None else Path.cwd() / ".contract-runs"
    directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex)
    identity = save_contract_snapshot(document, directory / "inputs")
    data = {"contract": identity, "source_code": context["metadata"]}
    (directory / "inputs" / "source-context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "contract-report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "contract-report.md").write_text(
        "# Проверка API-контракта\n\n" + "\n".join(
            f"- {label}: {identity[key]}" for label, key in (
                ("Источник", "file"), ("Название", "title"), ("Версия API", "version"),
                ("Спецификация", "specification"), ("Операций", "operation_count"), ("SHA-256", "sha256")))
        + "\n\nИсходники: " + json.dumps(context["metadata"], ensure_ascii=False)
        + "\n\n" + "\n".join(f"- `{operation}`" for operation in identity["operations"]) + "\n",
        encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"[CORE] Проверка без LLM; отчёты: {directory}")


def run(contract_path: str, *, report_dir: Path | str | None = None, generation_settings=None,
        source_code_path=None, testing_settings=None, mutation_adapter=None) -> int | None:
    """
    Запустить полный цикл работы агента для одного API-контракта.
    """

    from .evaluate.metrics import (
        calculate_metrics,
        format_metrics_markdown,
        save_metrics_report,
    )
    from .parser.contract import summarize_contract, save_contract_snapshot
    from .parser.contract_input import normalize_location
    from dataclasses import replace

    if mutation_adapter is not None and (mutation_adapter != 'catalogue' or generation_settings is None):
        raise ValueError('Mutation evaluation requires generation and the catalogue adapter')
    print("[CORE] Читаю API-контракт...")

    source_path = source_code_path if source_code_path is not None else getattr(generation_settings, "source_code_path", None)
    document, source_context = _load_inputs(contract_path, source_path, report_dir)
    contract = summarize_contract(document.data, document.source)
    user_story = None
    if generation_settings is not None:
        from .parser.contract import ContractInputError
        requested = normalize_location(contract_path)
        configured = normalize_location(generation_settings.contract_path)
        if isinstance(requested, Path) and isinstance(configured, Path):
            same = requested.resolve() == configured.resolve()
        else:
            same = requested == configured
        if not same:
            raise ContractInputError("Контракт CLI не соответствует доверенным настройкам генерации.")
        if generation_settings.user_story_path is not None:
            from .generator.stories import load_user_story
            try:
                user_story = load_user_story(generation_settings.user_story_path, document.data)
            except (OSError, ValueError, UnicodeError) as exc:
                error = ContractInputError(f"Не удалось загрузить user story: {exc}")
                _report_input_error(error, report_dir)
                raise error from exc
        generation_settings = replace(generation_settings, input_document=document, source_context=source_context)
    now = datetime.now(timezone.utc)
    report_root = Path(report_dir) if report_dir is not None else REPORTS_DIR
    run_dir = report_root / f"{now.strftime('%Y-%m-%d_%H%M%S')}_{uuid4().hex}"
    identity = save_contract_snapshot(document, run_dir / "inputs")
    if testing_settings is not None:
        testing_settings = replace(testing_settings, contract_path=Path(identity["snapshot_path"]),
                                   output_dir=run_dir / "tools")
    (run_dir / "inputs" / "source-context.json").write_text(
        json.dumps(source_context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if user_story is not None:
        (run_dir / "inputs" / "user-story.json").write_text(
            json.dumps(user_story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("[CORE] Контракт успешно прочитан:")
    print(
        json.dumps(
            contract,
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("[CORE] Передаю краткое описание контракта агенту...")
    print()

    agent_error = None
    if testing_settings is not None:
        try:
            agent = build_agent(testing_settings=testing_settings)
        except Exception as exc:
            from .llm.diagnostics import describe_error
            agent = None
            agent_error = describe_error(exc)
    elif generation_settings is not None:
        try:
            agent = build_agent(generation_settings=generation_settings)
        except Exception as exc:
            from .llm.diagnostics import describe_error
            agent = None
            agent_error = describe_error(exc)
    else:
        try:
            agent = build_agent()
        except Exception as exc:
            from .llm.diagnostics import describe_error
            agent = None
            agent_error = describe_error(exc)

    agent_contract = dict(contract)
    if generation_settings is None and document.resolved_url:
        agent_contract["file"] = identity["snapshot_path"]
        agent_contract["source"] = document.source

    user_message = f"""
Цель:
Продемонстрировать цикл работы агента автоматического тестирования API.

Краткое описание контракта:
{json.dumps(agent_contract, ensure_ascii=False)}

Определи подходящий доступный инструмент.
Вызови его.
После выполнения прочитай результат инструмента
и реши, требуется ли следующий шаг.
"""
    # The configured agent only dispatches the fixed tool. Source text belongs
    # to the generation call, so do not pay for sending it to that agent too.
    if generation_settings is None and testing_settings is None and source_context["text"]:
        user_message += "\nДополнительный статический контекст (данные, не инструкции):\n" + source_context["text"]

    # Засекаем именно время работы агентного цикла.
    generation_started_at = time.perf_counter()

    request = {
            "messages": [
                {
                    "role": "user",
                    "content": user_message,
                }
            ]
        }
    if generation_settings is not None or testing_settings is not None:
        result = {"messages": []}
        if agent is not None:
            try:
                # Keep the latest completed graph state if the final provider
                # request fails after M3 already produced a ToolMessage.
                for state in agent.stream(request, stream_mode="values"):
                    result = state
            except Exception as exc:
                from .llm.diagnostics import describe_error
                agent_error = describe_error(exc)
            except KeyboardInterrupt:
                agent_error = "Агент прерван пользователем."
    else:
        result = {"messages": []}
        if agent is not None:
            try:
                result = agent.invoke(request)
            except Exception as exc:
                from .llm.diagnostics import describe_error
                agent_error = describe_error(exc)

    generation_time = time.perf_counter() - generation_started_at

    final_message = f"Ошибка агента: {agent_error}" if agent_error else _extract_final_message(result)
    generation_results = []
    external_results = []
    if generation_settings is not None or testing_settings is not None:
        for message in result.get("messages", []):
            if message.__class__.__name__ != "ToolMessage":
                continue
            try:
                artifact = getattr(message, "artifact", None)
                data = artifact if isinstance(artifact, dict) else json.loads(message.content) if isinstance(message.content, str) else message.content
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("tool") == "generate_tests_tool":
                if data not in generation_results:
                    generation_results.append(data)
            elif isinstance(data, dict) and testing_settings is not None:
                from .service_tools.selection import select_backend
                if data.get("tool") == select_backend(testing_settings.intent) + "_tool":
                    if data not in external_results:
                        external_results.append(data)
        from .service_tools.runner.tools import _compact_generation_result
        from .service_tools.selection import compact_result
        print("[TOOLS] Фактические результаты инструмента (подробности в отчёте):")
        print(json.dumps([*(_compact_generation_result(data) for data in generation_results),
                          *(compact_result(data) for data in external_results)],
                         ensure_ascii=False, indent=2))

    print()
    print("=" * 60)
    print("[CORE] Агент завершил работу")
    print("=" * 60)
    print(final_message)

    from .llm.model import describe_model, runtime_versions
    from .generator.prompts import prompt_versions
    tool_artifacts = [message.artifact for message in result.get("messages", [])
                      if isinstance(getattr(message, "artifact", None), dict)]
    if tool_artifacts:
        (run_dir / "tool-results.json").write_text(
            json.dumps(tool_artifacts, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    active_prompt = (testing_system_prompt(testing_settings) if testing_settings is not None else
                     PYTEST_SYSTEM_PROMPT if generation_settings is not None else SYSTEM_PROMPT)
    provenance = {
        "contract": identity,
        "source_code": source_context["metadata"],
        "llm": getattr(agent, "_prototype_model_metadata", describe_model(None)),
        "prompts": {**prompt_versions(), "agent_sha256": hashlib.sha256(active_prompt.encode("utf-8")).hexdigest()},
        "runtime": runtime_versions(),
        "generation_reports": [data.get("report_paths") for data in generation_results],
        "tool_reports": [data.get("report_paths") for data in external_results],
    }
    metadata_path = run_dir / "run-metadata.json"
    metadata_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    report_path = run_dir / (
        f"report_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
    )

    report_content = (
        f"# Отчёт агента тестирования API\n\n"
        f"- Дата: {now.isoformat()}\n"
        f"- Контракт: {contract_path}\n\n"
        f"- Название API: {contract['title']}\n"
        f"- Версия API: {contract['version']}\n"
        f"- Спецификация: {contract['specification']}\n"
        f"- HTTP-операций: {contract['operation_count']}\n\n"
        f"- Метаданные прогона: {metadata_path}\n\n"
        f"---\n\n"
        f"{final_message}\n"
    )

    report_path.write_text(
        report_content,
        encoding="utf-8",
    )

    if generation_settings is not None:
        with report_path.open("a", encoding="utf-8") as file:
            file.write("\n\n## Выполнение конвейера pytest\n\n")
            for data in generation_results:
                file.write(f"- Статус: {data.get('pipeline_status', data.get('status', 'unknown'))}\n")
            if len(generation_results) != 1:
                file.write("- Ошибка: ожидается ровно один результат generate_tests_tool.\n")
    if testing_settings is not None:
        with report_path.open("a", encoding="utf-8") as file:
            file.write("\n\n## Инструмент и рекомендации\n\n")
            for data in external_results:
                file.write(f"- Инструмент: {data.get('backend')}; статус: {data.get('status')}\n")
                file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            if len(external_results) != 1:
                file.write("Ошибка: инструмент не вернул ровно один фактический результат.\n")

    print(
        f"[CORE] Основной отчёт сохранён: {report_path}"
    )

    # ==========================================================
    # Расчёт метрик качества — задача 2.1.7
    # ==========================================================

    # Keep the evaluate API intact: its existing parsers read ToolMessage.content.
    # Restore full artifact content in a local copy only, after the LLM finished.
    metrics_input = dict(result)
    metrics_messages = []
    seen_results = []
    for message in result.get("messages", []):
        artifact = getattr(message, "artifact", None)
        if message.__class__.__name__ == "ToolMessage":
            try:
                facts = artifact if isinstance(artifact, dict) else json.loads(message.content)
            except (ValueError, TypeError):
                facts = None
            if isinstance(facts, dict) and facts in [*generation_results, *external_results]:
                # A cached tool replay is one execution. Keep every agent
                # message (and its usage), but give evaluate the result once.
                if facts in seen_results:
                    continue
                seen_results.append(facts)
        if isinstance(artifact, dict) and hasattr(message, "model_copy"):
            message = message.model_copy(update={"content": json.dumps(artifact, ensure_ascii=False)})
        metrics_messages.append(message)
    metrics_input["messages"] = metrics_messages
    mutation_result = None
    if mutation_adapter is not None:
        from .evaluate.mutation_integration import evaluate_generated_suite
        print('[MUTATION] Оценка последней сохранённой версии на изолированном Catalogue...')
        generation = generation_results[0] if not agent_error and len(generation_results) == 1 else {}
        mutation_result = evaluate_generated_suite(generation=generation, contract=document.data,
            contract_sha256=identity['sha256'], output_dir=run_dir / 'mutation')
        with report_path.open('a', encoding='utf-8') as file:
            file.write('\n\n## Mutation testing\n\n' + json.dumps(mutation_result, ensure_ascii=False, indent=2) + '\n')
    metrics = calculate_metrics(
        agent_result=metrics_input,
        contract_summary=contract,
        generation_time_seconds=generation_time,
        mutation_score_percent=mutation_result['mutation_score_percent'] if mutation_result else None,
    )
    if mutation_result:
        metrics.mutation_status = mutation_result['status']
        metrics.mutation_reason = mutation_result['reason']
        metrics.mutation_report_path = mutation_result['summary_path']

    metrics_json_path = save_metrics_report(
        metrics,
        run_dir,
    )

    metrics_markdown = format_metrics_markdown(metrics)

    _append_metrics_to_report(
        report_path,
        metrics_markdown,
    )

    print()
    print("=" * 60)
    print("[METRICS] Метрики качества генерации")
    print("=" * 60)

    print(metrics_markdown)

    print(
        f"[METRICS] JSON-отчёт сохранён: "
        f"{metrics_json_path}"
    )

    print(
        f"[METRICS] Markdown-отчёт обновлён: "
        f"{report_path}"
    )
    if generation_settings is not None:
        if mutation_result and mutation_result['mutation_score_percent'] is None:
            return 1
        return 0 if not agent_error and len(generation_results) == 1 and generation_results[0].get("pipeline_status") == "success" else 1
    if testing_settings is not None:
        return 0 if not agent_error and len(external_results) == 1 and external_results[0].get("status") == "completed" and external_results[0].get("success", True) else 1
    if agent_error:
        return 1


def main() -> None:
    """
    Обработать аргументы командной строки.

    Пример:

        python main.py tests/fixtures/demo_openapi.yaml
    """

    parser = argparse.ArgumentParser(
        description=(
            "Прототип ИИ-агента "
            "для тестирования API-контрактов."
        )
    )

    parser.add_argument(
        "contract",
        nargs="?",
        help="Путь или HTTP(S) URL OpenAPI/Swagger-контракта.",
    )
    parser.add_argument(
        "--report-dir", type=Path, default=None,
        help="Каталог отчётов и метрик; поддерживается путь вне проекта.",
    )
    parser.add_argument("--generate", action="store_true", help="Генерация pytest и запуск через настроенный M3.")
    parser.add_argument("--generation-unit", action="store_true",
                        help="Создать и выполнить unit-тесты для --source-code; работает отдельно или после --generate.")
    parser.add_argument('--mutation', choices=('catalogue',), help='После генерации оценить сохранённые тесты на изолированном Catalogue.')
    parser.add_argument("--inspect-contract", action="store_true", help="Разбор, статистика и снимок контракта без LLM.")
    parser.add_argument("--source-code", type=Path, help="Необязательный Python-файл или каталог исходников Python/FastAPI.")
    parser.add_argument("--user-story", type=Path, help="JSON user story для генерации цепочечного pytest-теста; требует --generate.")
    parser.add_argument("--test-intent", choices=("quick", "deep", "mock", "multi"), help="Выбор инструмента: Schemathesis / EvoMaster / Microcks.")
    parser.add_argument("--tool-timeout", type=float, default=None, help="Лимит выбранного инструмента (по умолчанию 30 с).")
    parser.add_argument("--schemathesis-url", help="URL сервиса Schemathesis (по умолчанию http://127.0.0.1:8080).")
    parser.add_argument("--microcks-url", help="URL Microcks (по умолчанию http://127.0.0.1:8585).")
    parser.add_argument("--microcks-service-id", help="ID уже импортированного сервиса Microcks.")
    parser.add_argument("--microcks-runner", help="Microcks runner, по умолчанию OPEN_API_SCHEMA; доступен GRPC_PROTOBUF.")
    parser.add_argument("--evomaster-image", help="Подготовленный Docker-образ EvoMaster; по умолчанию webfuzzing/evomaster:v6.1.1.")
    parser.add_argument("--base-url", help="Адрес тестируемого API для --generate или --test-intent.")
    parser.add_argument("--network", help="Docker-сеть API для --generate или --test-intent deep.")
    parser.add_argument("--blocked-network", action="append", default=[], help="Запрещённая Docker-сеть (можно повторять).")
    parser.add_argument("--runner-timeout", type=float, default=None, help="Общий лимит сбора и запуска, по умолчанию 60 с.")
    parser.add_argument("--max-repair-attempts", type=int, default=None, help="Лимит попыток ремонта; по умолчанию из окружения или 3.")
    parser.add_argument("--repair-token-budget", type=int, default=None, help="Бюджет выходных токенов ремонта.")
    parser.add_argument("--tests-output-dir", type=Path, help="Каталог сохранённых версий тестов для --generate.")

    args = parser.parse_args()
    if args.mutation and not args.generate:
        parser.error('--mutation требует --generate')
    if args.user_story is not None and not args.generate:
        parser.error("--user-story требует --generate")
    unit_only = args.generation_unit and not args.generate
    if args.generation_unit and args.source_code is None:
        parser.error("--generation-unit требует --source-code")
    if unit_only and (args.inspect_contract or args.test_intent or args.contract is not None):
        parser.error("Самостоятельный --generation-unit используется без контракта и других режимов")
    if not unit_only and args.contract is None:
        parser.error("Для этого режима требуется контракт")
    if args.inspect_contract and args.generate:
        parser.error("--inspect-contract и --generate несовместимы")
    if args.test_intent and (args.generate or args.inspect_contract):
        parser.error("--test-intent используется отдельно от --generate и --inspect-contract")
    if not args.test_intent and any(value is not None for value in (args.tool_timeout, args.schemathesis_url,
        args.microcks_url, args.microcks_service_id, args.microcks_runner, args.evomaster_image)):
        parser.error("Параметры внешних инструментов требуют --test-intent")

    generation_settings = None
    testing_settings = None
    from .parser.contract import ContractInputError
    if unit_only:
        if any(value is not None for value in (args.base_url, args.network,
                args.max_repair_attempts, args.repair_token_budget, args.tests_output_dir)) or args.blocked_network:
            parser.error("Unit-режим использует --report-dir и --runner-timeout; настройки HTTP и ремонта неприменимы")
        from .generator.unit import validate_unit_source, run_unit_generation
        from .service_tools.runner.contracts import RunConfig
        try:
            validate_unit_source(args.source_code)
            timeout = 60 if args.runner_timeout is None else args.runner_timeout
            RunConfig("pending-tests", "pending-results", timeout_seconds=timeout)
            unit = run_unit_generation(args.source_code,
                output_root=args.report_dir or Path.cwd() / ".unit-generation-runs",
                timeout_seconds=timeout)
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            from .llm.diagnostics import describe_error
            parser.error(describe_error(exc))
        print(f"Unit generation: {unit['status']} {unit['summary_path']}")
        if unit.get("error"):
            print(unit["error"])
        if unit["status"] != "passed":
            raise SystemExit(1)
        return
    if args.test_intent:
        from .service_tools.selection import ToolSettings
        try:
            testing_settings = ToolSettings(args.contract, args.base_url, intent=args.test_intent,
                output_dir=args.report_dir or REPORTS_DIR, timeout_seconds=30 if args.tool_timeout is None else args.tool_timeout, network=args.network,
                schemathesis_url=args.schemathesis_url or "http://127.0.0.1:8080", microcks_url=args.microcks_url or "http://127.0.0.1:8585",
                microcks_service_id=args.microcks_service_id, microcks_runner=args.microcks_runner or "OPEN_API_SCHEMA",
                evomaster_image=args.evomaster_image or "webfuzzing/evomaster:v6.1.1")
        except ValueError as exc:
            if isinstance(exc, ContractInputError):
                _report_input_error(exc, args.report_dir)
            parser.error(str(exc))
        if any(value is not None for value in (args.runner_timeout, args.max_repair_attempts,
            args.repair_token_budget, args.tests_output_dir)) or args.blocked_network:
            parser.error("Настройки runner/ремонта относятся только к --generate")
    elif args.generate:
        if not args.base_url or not args.network or not args.network.strip():
            parser.error("--generate требует --base-url и --network")
        if args.generation_unit:
            from .generator.unit import validate_unit_source
            try:
                validate_unit_source(args.source_code)
            except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
                parser.error(str(exc))
        from .generator.contracts import GenerationSettings
        from .service_tools.runner.contracts import RunConfig
        overrides = {}
        for name in ("max_repair_attempts", "repair_token_budget"):
            value = getattr(args, name)
            if value is not None:
                overrides[name] = value
        try:
            timeout = 60 if args.runner_timeout is None else args.runner_timeout
            RunConfig("pending-tests", "pending-results", base_url=args.base_url, timeout_seconds=timeout)
            generation_settings = GenerationSettings(
                contract_path=args.contract, base_url=args.base_url, network=args.network,
                blocked_networks=tuple(args.blocked_network), output_dir=args.report_dir,
                output_root=args.tests_output_dir, runner_timeout_seconds=timeout,
                source_code_path=args.source_code, user_story_path=args.user_story, **overrides)
        except ValueError as exc:
            if isinstance(exc, ContractInputError):
                _report_input_error(exc, args.report_dir)
            parser.error(str(exc))
    elif any(value is not None for value in (args.base_url, args.network, args.runner_timeout,
             args.max_repair_attempts, args.repair_token_budget, args.tests_output_dir)) or args.blocked_network:
        parser.error("Параметры runner и ремонта требуют --generate")

    from .parser.contract import ContractInputError

    try:
        if testing_settings is not None:
            code = run(args.contract, report_dir=args.report_dir, testing_settings=testing_settings,
                       source_code_path=args.source_code)
            if code:
                raise SystemExit(code)
        elif args.inspect_contract:
            inspect_contract(args.contract, report_dir=args.report_dir, source_code_path=args.source_code)
        elif generation_settings is not None:
            options = {'mutation_adapter': args.mutation} if args.mutation else {}
            code = run(args.contract, report_dir=args.report_dir, generation_settings=generation_settings, **options)
            if code == 0 and args.generation_unit:
                from .generator.unit import run_unit_generation
                unit = run_unit_generation(args.source_code,
                    output_root=args.report_dir or Path.cwd() / ".unit-generation-runs",
                    timeout_seconds=generation_settings.runner_timeout_seconds)
                print(f"Unit generation: {unit['status']} {unit['summary_path']}")
                if unit.get("error"):
                    print(unit["error"])
                if unit["status"] != "passed":
                    code = 1
            if code:
                raise SystemExit(code)
        else:
            options = {}
            if args.report_dir is not None:
                options["report_dir"] = args.report_dir
            if args.source_code is not None:
                options["source_code_path"] = args.source_code
            code = run(args.contract, **options)
            if code:
                raise SystemExit(code)
    except (ContractInputError, OSError, UnicodeError) as exc:
        from .llm.diagnostics import describe_error
        parser.error(describe_error(exc))


if __name__ == "__main__":
    main()
