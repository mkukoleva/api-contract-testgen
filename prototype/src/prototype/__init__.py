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
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path


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


def build_agent():
    """
    Создать LangChain-агента.
    """

    from langchain.agents import create_agent

    from .llm.model import build_model
    from .runner.tools import (
        demo_api_test_tool,
        generate_user_story_tool,
        schemathesis_tool,
        verify_user_story_tool,
    )

    model = build_model()

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


def run(contract_path: str) -> None:
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

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": user_message,
                }
            ]
        }
    )

    generation_time = time.perf_counter() - generation_started_at

    final_message = _extract_final_message(result)

    print()
    print("=" * 60)
    print("[CORE] Агент завершил работу")
    print("=" * 60)
    print(final_message)

    REPORTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    now = datetime.now()

    report_path = REPORTS_DIR / (
        f"report_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
    )

    report_content = (
        f"# Отчёт агента тестирования API\n\n"
        f"- Дата: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- Контракт: {contract_path}\n\n"
        f"---\n\n"
        f"{final_message}\n"
    )

    report_path.write_text(
        report_content,
        encoding="utf-8",
    )

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
        REPORTS_DIR,
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

    args = parser.parse_args()

    run(args.contract)


if __name__ == "__main__":
    main()
