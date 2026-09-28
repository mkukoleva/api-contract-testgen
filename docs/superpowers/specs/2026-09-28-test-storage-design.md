# Дизайн: сохранение сгенерированных тестов (ТЗ 2.1.6)

Дата: 2026-09-28. Статус: согласован, ожидает плана реализации.

## Цель

Пункт ТЗ 2.1.6: «Сохранение сгенерированных тестов в виде исполняемого,
версионируемого кода». Модуль принимает pytest-код от генератора (ТЗ 2.1.5,
ещё не реализован) после постобработки и сохраняет его как неизменяемую
версию, которую затем запускает runner (ADR 0002, `RunConfig.tests_dir`).

- **Версионируемый** — каждый прогон генерации сохраняется в отдельный
  каталог с меткой даты и времени и `manifest.json`; существующие версии
  никогда не перезаписываются. Коммит в git делает человек.
- **Исполняемый** — каждый файл проходит `ast.parse`, затем набор проверяется
  `pytest --collect-only`; результат фиксируется в манифесте.

Вне рамок: генерация кода, постобработка, запуск тестов, расчёт метрик,
сетевая изоляция (ТЗ 2.2.6), подключение tool в `build_agent`/`SYSTEM_PROMPT`.

## Размещение

Новый пакет `prototype/src/prototype/storage/`. Импорт пакета использует только
стандартную библиотеку и не загружает LLM, `.env`, LangChain или YAML
(аналогично `prototype.runner.contracts`). pytest вызывается подпроцессом.

- `storage/__init__.py` — реэкспорт публичного API.
- `storage/contracts.py` — `GeneratedFile`, `SaveRequest`, `SavedSuite`.
- `storage/store.py` — `save_test_suite()` и вспомогательные функции.
- `runner/tools.py` — добавляется `save_tests_tool` (тонкая обёртка).

## Структура хранения

```
<output_root>/                          по умолчанию prototype/generated
  <contract_slug>/                      slug из info.title, иначе имя файла
    <run_id>/                           YYYY-MM-DD_HHMMSS, при совпадении _2, _3…
      tests/test_*.py                   синтаксически корректные файлы
      rejected/test_*.py                файлы с синтаксической ошибкой
      manifest.json
```

`output_root`: аргумент запроса → переменная окружения `TESTGEN_OUTPUT_DIR` →
`prototype/generated` (определяется относительно пакета, вне `src/`).

`contract_slug`: `info.title` из контракта, приведённый к `[a-z0-9-]`
(латиница в нижнем регистре, прочие символы → `-`, повторы схлопываются),
не длиннее 64 символов; при пустом результате — stem имени файла контракта;
если пуст и он — `contract`. Контракт читается как JSON, при неудаче — через
PyYAML, импортируемый лениво внутри функции (импорт пакета `storage` остаётся
без сторонних зависимостей). Неразборчивый контракт не ошибка: title пустой,
используется fallback.

Запись атомарна: всё пишется во временный каталог `.<run_id>.tmp` внутри
каталога контракта и переименовывается в `<run_id>` одним `os.rename`. При
исключении временный каталог удаляется. Незавершённая версия не видна под
именем `run_id`.

## Данные

```python
@dataclass(frozen=True)
class GeneratedFile:
    name: str   # только ^test_[a-z0-9_]+\.py$
    code: str

@dataclass(frozen=True)
class SaveRequest:
    contract_path: Path | str
    files: tuple[GeneratedFile, ...]
    model: str | None = None
    generator_meta: Mapping[str, Any] = {}   # JSON-совместимое, иначе ValueError
    output_root: Path | str | None = None
    collect: bool = True
    collect_timeout_seconds: float = 30.0    # конечное положительное

@dataclass(frozen=True)
class SavedSuite:
    run_id: str
    run_dir: Path
    tests_dir: Path          # передаётся в RunConfig(tests_dir=...)
    manifest_path: Path
    manifest: dict           # содержимое manifest.json
```

Валидация в `__post_init__` (ошибки → `ValueError`, на диске ничего не
создаётся): пустой список файлов; имя не по шаблону (включая `conftest.py`,
разделители пути, `..`, верхний регистр); повторяющиеся имена; `code` не
строка; `generator_meta` не сериализуется в JSON; некорректный таймаут.
Отсутствие файла контракта проверяется в `save_test_suite` → `FileNotFoundError`.

`conftest.py` запрещён: по ADR 0002 фикстуры `base_url` и `api_client`
предоставляет доверенная среда выполнения, а не сгенерированный код.

## Алгоритм `save_test_suite(request) -> SavedSuite`

1. Проверить существование контракта, вычислить его sha256, title и slug.
2. Для каждого файла: `ast.parse`. Успех → `tests/`, ошибка → `rejected/`
   с `SyntaxError.msg` и номером строки. Для корректных файлов через ast
   посчитать функции `test_*` верхнего уровня и методы `test_*` классов
   `Test*`; ноль → статус `no_tests` (файл остаётся в `tests/`). Отметить
   предупреждение `top_level_code`, если на верхнем уровне модуля есть
   узлы кроме `import`, `from … import`, `def`, `async def`, `class`,
   присваиваний (включая аннотированные) и строкового докстринга.
3. Записать файлы (UTF-8, `\n`), посчитать sha256 каждого.
4. Если `collect` и в `tests/` есть файлы — выполнить сбор (см. ниже).
   Иначе статус сбора `skipped`.
5. Записать `manifest.json` (UTF-8, `ensure_ascii=False`, `indent=2`,
   `allow_nan=False`), переименовать временный каталог, вернуть `SavedSuite`.

Ошибки сбора — не исключения: они фиксируются в манифесте, чтобы
self-repair (ТЗ 2.1.2) получил конкретные сообщения.

### Сбор pytest

```
<sys.executable> -m pytest --collect-only -q -p no:cacheprovider
    --rootdir <tmp_tests_dir> -c <пустой ini вне каталога версии> <tmp_tests_dir>
```

Пустой ini создаётся через `tempfile` в системном временном каталоге и
удаляется после сбора — в каталог версии он не попадает.

- `cwd` — временный каталог версии; таймаут `collect_timeout_seconds`.
- Окружение копируется без переменных с префиксами `DEEPCODE_`, `OPENAI_`,
  `PYTEST_ADDOPTS`; `PYTHONDONTWRITEBYTECODE=1` — кэш байткода и
  `.pytest_cache` не создаются, версия содержит только наши файлы.
- `-c` на пустой ini изолирует сбор от `pyproject.toml`/`pytest.ini` проекта.
- Вывод `-q`: строки вида `<path>::<name>` — это nodeid; nodeid
  нормализуются к пути относительно `tests/` с разделителем `/`.
- Статус: код 0 → `ok`; код 5 (нет тестов) → `no_tests`; код 2/3/4 или
  наличие ошибок сбора → `errors` (текст из секции `ERRORS`, обрезанный до
  4000 символов на ошибку); `TimeoutExpired` → `timeout`; невозможность
  запустить интерпретатор → `unavailable`.

**Ограничение безопасности.** `--collect-only` импортирует модули тестов и
выполняет их код верхнего уровня. Это не песочница и не сетевая изоляция:
изоляция — ответственность runner (ТЗ 2.2.6, ADR 0002). Предупреждение
`top_level_code` в манифесте помогает заметить опасный код; после появления
Docker-runner сбор можно отключить (`collect=False`) и перенести в изоляцию.

## manifest.json

```json
{
  "schema_version": 1,
  "run_id": "2026-09-28_153012",
  "created_at": "2026-09-28T15:30:12+07:00",
  "contract": {"path": "…/demo_openapi.yaml", "sha256": "…", "title": "Demo Catalogue API", "slug": "demo-catalogue-api"},
  "generator": {"model": "deepseek-ai/DeepSeek-V4-Flash", "meta": {}},
  "files": [
    {"name": "test_catalogue.py", "location": "tests", "sha256": "…",
     "status": "ok", "test_functions": 3, "warnings": [], "error": null},
    {"name": "test_broken.py", "location": "rejected", "sha256": "…",
     "status": "syntax_error", "test_functions": null, "warnings": [],
     "error": "line 4: invalid syntax"}
  ],
  "collection": {"status": "ok", "collected": 3,
                 "nodeids": ["test_catalogue.py::test_list"], "errors": [],
                 "duration_seconds": 0.41},
  "summary": {"files_total": 2, "files_ok": 1, "files_rejected": 1,
              "test_functions": 3, "collected": 3}
}
```

`status` файла: `ok`, `no_tests`, `syntax_error`. `created_at` — локальное
время с часовым поясом. `summary.collected` равно `null`, если сбор не
выполнялся или завершился `timeout`/`unavailable`.

## Tool агента

`save_tests_tool(contract_path: str, files: list[dict[str, str]], model: str | None = None)`
в `runner/tools.py`. Вызывает `save_test_suite` и возвращает компактный
словарь: `tool`, `status` (`success`/`error`), `run_id`, `tests_dir`,
`summary`, `collection.status` и первые 5 ошибок сбора/синтаксиса. Код
тестов обратно в LLM не передаётся (ТЗ 2.2.5). `ValueError` и
`FileNotFoundError` превращаются в `status: "error"` с сообщением, как у
остальных tools. Tool не добавляется в `build_agent` и `SYSTEM_PROMPT` —
подключение выполнит автор генератора; существующий тест «четыре tools»
остаётся без изменений.

## Тесты

`prototype/src/prototype/tests/test_storage.py`, на `tmp_path`, без сети,
LLM и Docker:

- корректный набор → каталог версии, `tests/`, манифест, sha256 совпадает
  с содержимым, `collection.status == "ok"`, ожидаемые nodeid;
- синтаксическая ошибка → файл в `rejected/`, остальные собраны;
- ошибка импорта в модуле → `collection.status == "errors"` с текстом;
- файл без тестов → `no_tests`;
- недопустимые имена (`../x.py`, `conftest.py`, `Test_A.py`, `a/test_b.py`),
  дубли, пустой список, не-JSON `generator_meta` → `ValueError`, каталог
  `output_root` не создан;
- нет контракта → `FileNotFoundError`;
- два сохранения с одинаковой меткой времени (время подменяется) → `_2`,
  первая версия не изменена;
- таймаут сбора (`time.sleep` на верхнем уровне, таймаут 1 с) → `timeout`,
  предупреждение `top_level_code`;
- `collect=False` → `skipped`;
- в версии нет `__pycache__` и `.pytest_cache`;
- `TESTGEN_OUTPUT_DIR` учитывается, если `output_root` не задан;
- slug из title JSON- и YAML-контракта и fallback на имя файла;
- импорт `prototype.storage` в изолированном интерпретаторе не загружает
  `prototype.llm.model` и сторонние пакеты;
- `save_tests_tool` (с подменой зависимостей LangChain не требуется —
  tool вызывается через `.func`) возвращает компактный результат и
  `status: "error"` на недопустимом имени.

## Документация

- `docs/adr/0003-generated-tests-storage.md` — решение о формате хранения,
  манифесте, границах ответственности и ограничении безопасности сбора.
- `prototype/ReadMe.md` — раздел о модуле сохранения и пример вызова.
- `.gitignore` не меняется: коммитить ли `generated/`, решает команда.
