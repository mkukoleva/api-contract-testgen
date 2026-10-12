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
     "http://127.0.0.1:9911".
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


def build_agent(*, generation_settings=None):
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

    if generation_settings is not None:
        from .service_tools.runner.tools import create_generation_tool
        return create_agent(
            model=model,
            tools=[create_generation_tool(generation_settings, model=model)],
            system_prompt=(
                "Ты — ядро агента запуска настроенного конвейера pytest. "
                "Вызови generate_tests_tool ровно один раз с пустыми аргументами. "
                "Прочитай фактический результат инструмента и сообщи статус, "
                "результаты pytest и пути отчётов. Не выдумывай успех при ошибке. "
                "Адрес, сеть, лимиты и каталог задаёт доверенный оркестратор."
            ),
        )

    return create_agent(
        model=model,
        tools=[
            schemathesis_tool,
            demo_api_test_tool,
            generate_user_story_tool,
            verify_user_story_tool,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


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


def run(contract_path: str, *, report_dir: Path | str | None = None, generation_settings=None) -> int | None:
    """
    Запустить полный цикл работы агента для одного API-контракта.
    """

    from .evaluate.metrics import (
        calculate_metrics,
        format_metrics_markdown,
        save_metrics_report,
    )
    from .parser.contract import read_contract_summary

    print("[CORE] Читаю API-контракт...")

    contract = read_contract_summary(contract_path)

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
    if generation_settings is not None:
        from .parser.contract import ContractInputError
        if Path(contract_path).resolve() != generation_settings.contract_path.resolve():
            raise ContractInputError("Контракт CLI не соответствует доверенным настройкам генерации.")
        try:
            agent = build_agent(generation_settings=generation_settings)
        except Exception as exc:
            from .llm.diagnostics import describe_error
            agent = None
            agent_error = describe_error(exc)
    else:
        agent = build_agent()

    user_message = f"""
Цель:
Продемонстрировать цикл работы агента автоматического тестирования API.

Краткое описание контракта:
{json.dumps(contract, ensure_ascii=False)}

Определи подходящий доступный инструмент.
Вызови его.
После выполнения прочитай результат инструмента
и реши, требуется ли следующий шаг.
"""

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
    if generation_settings is not None:
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
        result = agent.invoke(request)

    generation_time = time.perf_counter() - generation_started_at

    final_message = f"Ошибка агента: {agent_error}" if agent_error else _extract_final_message(result)
    generation_results = []
    if generation_settings is not None:
        for message in result.get("messages", []):
            if message.__class__.__name__ != "ToolMessage":
                continue
            try:
                data = json.loads(message.content) if isinstance(message.content, str) else message.content
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("tool") == "generate_tests_tool":
                generation_results.append(data)
        print("[PYTEST] Фактические результаты инструмента:")
        print(json.dumps(generation_results, ensure_ascii=False, indent=2))

    print()
    print("=" * 60)
    print("[CORE] Агент завершил работу")
    print("=" * 60)
    print(final_message)

    now = datetime.now(timezone.utc)
    # Keep Markdown and metrics together without changing the metrics API.
    # UUID separates concurrent runs even when their timestamps are identical.
    report_root = Path(report_dir) if report_dir is not None else REPORTS_DIR
    run_dir = report_root / f"{now.strftime('%Y-%m-%d_%H%M%S')}_{uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)

    report_path = run_dir / (
        f"report_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
    )

    report_content = (
        f"# Отчёт агента тестирования API\n\n"
        f"- Дата: {now.isoformat()}\n"
        f"- Контракт: {contract_path}\n\n"
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

    print(
        f"[CORE] Основной отчёт сохранён: {report_path}"
    )

    # ==========================================================
    # Расчёт метрик качества — задача 2.1.7
    # ==========================================================

    metrics = calculate_metrics(
        agent_result=result,
        contract_summary=contract,
        generation_time_seconds=generation_time,
    )

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
        return 0 if not agent_error and len(generation_results) == 1 and generation_results[0].get("pipeline_status") == "success" else 1


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
        help="Путь к OpenAPI/Swagger-контракту.",
    )
    parser.add_argument(
        "--report-dir", type=Path, default=None,
        help="Каталог отчётов и метрик; поддерживается путь вне проекта.",
    )
    parser.add_argument("--generate", action="store_true", help="Генерация pytest и запуск через настроенный M3.")
    parser.add_argument("--base-url", help="Адрес тестируемого API для --generate.")
    parser.add_argument("--network", help="Docker-сеть тестируемого API для --generate.")
    parser.add_argument("--blocked-network", action="append", default=[], help="Запрещённая Docker-сеть (можно повторять).")
    parser.add_argument("--runner-timeout", type=float, default=None, help="Общий лимит сбора и запуска, по умолчанию 60 с.")
    parser.add_argument("--max-repair-attempts", type=int, default=None, help="Лимит попыток ремонта; по умолчанию из окружения или 3.")
    parser.add_argument("--repair-token-budget", type=int, default=None, help="Бюджет выходных токенов ремонта.")
    parser.add_argument("--tests-output-dir", type=Path, help="Каталог сохранённых версий тестов для --generate.")

    args = parser.parse_args()

    generation_settings = None
    if args.generate:
        if not args.base_url or not args.network or not args.network.strip():
            parser.error("--generate требует --base-url и --network")
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
                output_root=args.tests_output_dir, runner_timeout_seconds=timeout, **overrides)
        except ValueError as exc:
            parser.error(str(exc))
    elif any(value is not None for value in (args.base_url, args.network, args.runner_timeout,
             args.max_repair_attempts, args.repair_token_budget, args.tests_output_dir)) or args.blocked_network:
        parser.error("Параметры runner и ремонта требуют --generate")

    from .parser.contract import ContractInputError

    try:
        if generation_settings is not None:
            code = run(args.contract, report_dir=args.report_dir, generation_settings=generation_settings)
            if code:
                raise SystemExit(code)
        elif args.report_dir is None:
            run(args.contract)
        else:
            run(args.contract, report_dir=args.report_dir)
    except (ContractInputError, OSError, UnicodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
