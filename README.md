# api-contract-testgen

Прототип генератора тестов по API-контракту: из OpenAPI-спецификации
микросервиса генерирует запускаемые pytest-тесты (счастливый путь,
граничные случаи, обработка ошибок) с помощью LLM.

## Стек
- OpenAPI — контракт API
- Python 3.11+, pytest — тесты
- LLM — генерация тестов
- mutmut — mutation testing
- Schemathesis / Specmatic — baseline для сравнения

## Как работает
1. prototype/parser — парсинг OpenAPI во внутреннюю модель.
2. prototype/llm — LLM генерирует тесты.
3. prototype/postprocess — детерминированная пост-обработка.
4. prototype/runner — запуск тестов + self-repair (не более 3 попыток).
5. prototype/evaluate — mutation testing, покрытие контракта, стоимость генерации.

## Запуск
python -m venv .venv
.venv\Scripts\activate
pip install pytest
copy .env.example .env
pytest prototype\tests

## Правила
Прямые коммиты в main запрещены — только pull request.