"""
Главный файл прототипа ИИ-агента.

Здесь собирается весь цикл:

1. Python читает API-контракт.
2. Из контракта создаётся короткое summary.
3. Summary передаётся LLM.
4. LLM выбирает доступный tool.
5. LangChain вызывает выбранный Python-tool.
6. Результат tool возвращается LLM.
7. LLM анализирует результат.
8. LLM либо выбирает следующий tool, либо завершает работу.

Пока доступен только один демонстрационный tool.
Позже сюда будут добавлены реальные инструменты.
"""

import argparse
import json

from langchain.agents import create_agent

from llm.model import build_model
from parser.contract import read_contract_summary
from runner.tools import demo_api_test_tool


# Системный prompt задаёт правила поведения ядра агента.
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
- В текущем прототипе для демонстрации тестирования
  используй demo_api_test_tool.
- После успешного выполнения demo_api_test_tool
  повторно его не вызывай.
- После выполнения инструмента кратко объясни результат
  и заверши работу.
"""


def build_agent():
    """
    Создать LangChain-агента.

    Здесь модели передаётся список инструментов,
    которые она имеет право выбирать и вызывать.

    Сейчас доступен только один демонстрационный инструмент.
    """

    model = build_model()

    return create_agent(
        model=model,
        tools=[
            demo_api_test_tool,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


def run(contract_path: str) -> None:
    """
    Запустить полный цикл работы агента для одного API-контракта.

    Args:
        contract_path: Путь к OpenAPI/Swagger-файлу.
    """

    # Шаг 1. Читаем контракт обычным Python-кодом.
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

    # Создаём агента только после успешного чтения контракта.
    agent = build_agent()

    # LLM получает не весь OpenAPI-файл,
    # а только короткое структурированное описание.
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

    # Здесь запускается агентный цикл LangChain.
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

    # Последнее сообщение должно содержать итоговый ответ LLM
    # после выполнения всех необходимых tools.
    final_message = result["messages"][-1]

    print()
    print("=" * 60)
    print("[CORE] Агент завершил работу")
    print("=" * 60)
    print(final_message.content)


def main() -> None:
    """
    Обработать аргументы командной строки и запустить агента.

    Пример:

    python3 main.py tests/fixtures/demo_openapi.yaml
    """

    parser = argparse.ArgumentParser(
        description="Прототип ИИ-агента для тестирования API-контрактов."
    )

    parser.add_argument(
        "contract",
        help="Путь к OpenAPI/Swagger-контракту.",
    )

    args = parser.parse_args()

    run(args.contract)


if __name__ == "__main__":
    main()