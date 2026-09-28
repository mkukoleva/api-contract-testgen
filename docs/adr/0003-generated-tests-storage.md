# ADR 0003 — Сохранение сгенерированных тестов

Статус: реализован модуль `prototype.storage` и tool `save_tests_tool`;
подключение к генератору — вместе с реализацией п. 2.1.5 ТЗ.

## Контекст

Пункт 2.1.6 ТЗ: «Сохранение сгенерированных тестов в виде исполняемого,
версионируемого кода». ADR 0002 закрепил, что генератор сохраняет pytest-код,
а runner принимает каталог `RunConfig.tests_dir`.

## Решение

Каждый прогон генерации сохраняется в отдельный неизменяемый каталог:

```
<output_root>/<contract_slug>/<run_id>/
  tests/test_*.py      синтаксически корректные модули — это RunConfig.tests_dir
  rejected/test_*.py   модули с синтаксической ошибкой
  manifest.json        происхождение и результаты проверок
```

- `output_root`: аргумент `SaveRequest.output_root` → переменная окружения
  `TESTGEN_OUTPUT_DIR` → `prototype/generated`.
- `contract_slug` — `info.title` контракта в виде `[a-z0-9-]`, иначе имя файла.
- `run_id` — `YYYY-MM-DD_HHMMSS`; при совпадении добавляется `_2`, `_3`…
- Версия пишется во временный `.<run_id>.tmp` и переименовывается одним шагом.
  Существующие версии никогда не перезаписываются.
- Код нормализуется: окончания строк `\n`, ведущий BOM удаляется.
- Коммит версий в git делает человек; `generated/` не добавлен в `.gitignore`.

«Исполняемость» проверяется в два шага: `ast.parse` каждого файла и
`pytest --collect-only` набора в подпроцессе. Результаты — в `manifest.json`;
ошибки сбора не прерывают сохранение, чтобы self-repair получил конкретные
сообщения.

Имена файлов приходят от LLM и считаются недоверенными: допускается только
`test_[a-z0-9_]+.py`. `conftest.py` запрещён — фикстуры `base_url` и
`api_client` предоставляет доверенная среда выполнения (ADR 0002).

## manifest.json (schema_version 1)

| Поле | Значение |
|---|---|
| `run_id`, `created_at` | Идентификатор версии и локальное время с часовым поясом |
| `contract` | `path`, `sha256`, `title`, `slug` |
| `generator` | `model` и произвольные JSON-метаданные генератора `meta` |
| `files[]` | `name`, `location` (`tests`/`rejected`), `sha256`, `status` (`ok`/`no_tests`/`syntax_error`), `test_functions`, `warnings`, `error` |
| `collection` | `status` (`ok`/`no_tests`/`errors`/`timeout`/`unavailable`/`skipped`), `collected`, `nodeids`, `errors`, `duration_seconds` |
| `summary` | `files_total`, `files_ok`, `files_no_tests`, `files_rejected`, `test_functions`, `collected` |

`summary.collected` — `null`, если сбор не выполнялся или не завершился.
Для Runnability нужен исходный перечень тестов генератора; `collected` сам
по себе этой метрикой не является (см. ADR 0002).

## Ограничение безопасности

`pytest --collect-only` импортирует модули тестов и **выполняет их код
верхнего уровня**. Это не песочница и не сетевая изоляция — изоляция
относится к runner (ТЗ 2.2.6). Сбор запускается:

- с таймаутом (`collect_timeout_seconds`, по умолчанию 30 с);
- без переменных `DEEPCODE_*`, `OPENAI_*`, `PYTEST_ADDOPTS`;
- без настроек pytest проекта (пустой ini вне каталога версии);
- без автозагрузки установленных плагинов pytest
  (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`) — например, плагин `langsmith` из
  зависимостей LangChain не попадает в процесс со сгенерированным кодом,
  а сбор ускоряется примерно вдвое;
- без записи `__pycache__` и `.pytest_cache` в каталог версии.

Код верхнего уровня, кроме импортов, определений и присваиваний,
помечается предупреждением `top_level_code`. Когда появится изолированный
runner, сбор следует перенести в него (`collect=False`).

## Ответственность

- Генератор/постобработка передают готовый код в `save_test_suite` или
  агент вызывает `save_tests_tool`.
- Модуль хранения не вызывает LLM, не запускает тела тестов, не считает метрики.
- Runner получает `SavedSuite.tests_dir` как `RunConfig.tests_dir`.
