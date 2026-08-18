"""
Этот модуль содержит инструменты, которые может вызывать ИИ-агент.

Сейчас здесь находится только демонстрационный инструмент-заглушка.
Он не запускает Schemathesis, EvoMaster или другие реальные системы.

Его задача — проверить сам механизм:
LLM -> выбор инструмента -> вызов Python-функции -> возврат результата -> LLM.
"""

from langchain.tools import tool

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