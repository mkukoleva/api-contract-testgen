"""Prompts for generation and self-repair, plus payload clipping helpers.

The repair prompt deliberately forbids weakening assertions: a failing test
that contradicts the documented contract is a service defect, and making it
pass by loosening the expectation is never an acceptable repair (ADR 0004).
"""

from pydantic import BaseModel, Field


GENERATION_SYSTEM_PROMPT = """\
Ты — генератор pytest-тестов для тестирования API по OpenAPI/Swagger-контракту.

Правила генерации:

1. Пиши файлы вида test_*.py с функциями test_*; один файл — один логический
   раздел контракта (например, один или несколько связанных эндпоинтов).
2. Используй только доверенные фикстуры, которые предоставляет среда
   выполнения (ADR 0002):
   - `base_url` — строка, базовый адрес тестируемого API;
   - `api_client` — requests.Session с таймаутом, без proxy из окружения и без
     следования редиректам.
   Пример:
       def test_catalogue(base_url, api_client):
           response = api_client.get(f"{base_url}/catalogue")
           assert response.status_code == 200
           assert isinstance(response.json(), list)
3. Не определяй собственные фикстуры, conftest.py или сетевую политику.
   Не читай переменные окружения и не обращайся к недоступным ресурсам.
4. Используй только операции, пути, поля и статусы, которые есть в контракте.
   Не выдумывай эндпоинты, поля ответа или коды статусов.
5. Для штатного сценария используй успешный документированный статус и
   репрезентативные входные данные (примеры, enum, параметры из контракта).
   Для граничных сценариев и ошибок используй документированные статусы
   ошибок с подходящими входными данными (например, несуществующий id -> 404,
   если 404 документирован).
6. Проверяй в assert и статус, и структуру ответа, документированную в
   контракте (обязательные поля, типы). Не ослабляй проверки.
7. Код должен быть синтаксически корректным и запускаемым; без внешних
   зависимостей, кроме `requests`, предоставляемого фикстурой api_client.

Верни строго ОДИН JSON-объект вида
{"files": [{"name": "test_<name>.py", "code": "<полный код>"}, ...]} —
без markdown-разметки (без ```), без пояснений и текста вне JSON."""


def build_generation_prompt(contract_fragment: str) -> str:
    """User prompt for the initial generation call."""
    return f"""\
Сгенерируй pytest-тесты по следующему краткому описанию API-контракта.
Тесты должны запускаться и проходить на рабочем стенде, если такой стенд
корректен, и падать при дефекте сервиса (например, недокументированный
статус или отсутствие обязательного поля).

Краткое описание контракта:
{contract_fragment}
"""


REPAIR_SYSTEM_PROMPT = """\
Ты — модуль самовосстановления (self-repair) pytest-тестов. Ты получаешь
фрагмент упавшего теста, релевантную часть API-контракта и сокращённую
диагностику. Твоя задача — вернуть ИСПРАВЛЕННЫЙ КОД.

Жёсткие правила:

1. Автоматически допускается только исправление HTTP-метода или строковой
   части URL в вызовах api_client/requests вне assert. Сохраняй импорты,
   декораторы, переменные, аргументы и структуру выполнения. Если нужен
   другой вид изменения, верни исходный код: он требует проверки человеком.
2. НИКОГДА не ослабляй и не удаляй assert, чтобы тест начал проходить.
3. НЕ меняй ожидаемый статус-код и ожидаемую структуру ответа, если они
   соответствуют контракту.
4. Если фактический ответ сервиса противоречит документированным ответам
   контракта (например, получен 500, а в контракте его нет) — это дефект
   сервиса, а не теста. Не подгоняй тест под дефектное поведение: сохрани
   проверку и, если других ошибок в коде нет, верни код без изменений.
5. Изменение URL должно соответствовать исходному сценарию и контракту.
   Не заменяй негативный сценарий позитивным ради успешного результата.
6. Продолжай использовать фикстуры `base_url` и `api_client`. Не добавляй
   conftest.py, свои фикстуры или сетевую политику.
7. Верни строго код целиком: для функции — всю функцию с декораторами, для
   файла — весь файл. Без пояснений, без markdown-обёрток."""


def build_repair_prompt(
    *,
    kind: str,
    test_fragment: str,
    contract_fragment: str,
    diagnostics: str,
) -> str:
    """Build the compact self-repair prompt (ADR 0004 payload limits)."""
    target = "исправленную функцию test_* целиком" if kind == "function" else "исправленный файл модуля целиком"
    return f"""\
Проверка API-контракта обнаружила падение сгенерированного pytest-теста.

Необходимо исправить код теста и вернуть {target}.

Упавший фрагмент теста:
```python
{test_fragment}
```

Релевантная часть контракта:
{contract_fragment}

Сокращённая диагностика падения:
{diagnostics}

Помни: ослаблять или удалять assert запрещено; несоответствие ответа сервиса
документированным ответам контракта — дефект сервиса, а не повод менять тест.
"""


class GeneratedFileSpec(BaseModel):
    """One file produced by the generation call."""

    name: str = Field(description="file name, must match test_[a-z0-9_]+.py")
    code: str = Field(description="full python source of the test module")


class GenerationOutput(BaseModel):
    """Structured generation result: the list of generated files."""

    files: list[GeneratedFileSpec] = Field(
        min_length=1, description="generated pytest modules, non-empty"
    )


class RepairOutput(BaseModel):
    """Structured repair result: corrected code (function or module)."""

    code: str = Field(description="corrected code, whole function or whole file")
    note: str | None = Field(default=None, description="optional short explanation")


def clip_code(code: str, max_lines: int) -> str:
    """Clip a code fragment to max_lines, keeping the failure area visible."""
    if max_lines <= 0:
        return "# (код обрезан)"
    lines = code.splitlines()
    if len(lines) <= max_lines:
        return code
    return "\n".join(lines[:max_lines]) + "\n    # …(обрезано)"


def diag_tail(message: str | None, max_chars: int) -> str:
    """Return the shortened tail of the failure diagnostics.

    The useful part of pytest output (traceback tail, exception, assertion
    comparison) is at the end; keeping the tail saves tokens (ADR 0004).
    """
    if not message:
        return "(диагностика отсутствует)"
    if max_chars <= 0:
        return "…(диагностика обрезана)"
    if len(message) <= max_chars:
        return message
    return "…(обрезано начало)\n" + message[-max_chars:]
