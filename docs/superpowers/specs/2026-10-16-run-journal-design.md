# Дизайн: журнал выполнения прогона (ТЗ v3, п. 4.1.8)

Дата: 2026-10-16. Статус: согласован в обсуждении, ожидает ревью спецификации.

## Цель

Закрыть подраздел ТЗ ARQA v3 4.1.8 «Администрирование и журналирование»:

- **4.1.8.1** — роли «пользователь» и «администратор/DevOps»; в MVP разграничение
  прав обеспечивается средствами окружения (доступ к `.env` и конфигурации).
- **4.1.8.2** — журнал: этапы агентного цикла, вызовы инструментов и их
  результаты, обращения к LLM (время, токены, ошибки и таймауты), ошибки
  разбора контракта, статусы прогонов тестов (завершён, ошибка сбора тестов,
  ошибка инфраструктуры, превышение времени, тесты не найдены, прерван).
- **4.1.8.3** — воспроизводимость: версия контракта, модель и её параметры,
  версия промптов, версии инструментов и зависимостей, параметры запуска.

Сейчас журнала нет: события выводятся через `print` и теряются; только runner
пишет свои `events.jsonl`. `run-metadata.json` уже покрывает большую часть 4.1.8.3,
кроме параметров запуска.

## Решения, принятые в обсуждении

- Журнал читают и люди, и программы: `journal.jsonl` + `journal.md`.
- Журнал ведётся на каждый запуск CLI `tester` во всех режимах. Генератор и
  runner попадают в журнал как инструменты агента; при прямом вызове из Python
  журнал не ведётся. `tools/verify.py` не меняется.
- Тексты промптов и ответов LLM в журнал не пишутся — только модель, время,
  токены, статус и текст ошибки без секретов.
- Подход A: собственный пакет `prototype.journal` + callbacks LangChain.
  Отвергнуты: стандартный `logging` (глобальное состояние, правки 60+ `print`
  коллег) и сборка журнала после прогона (нет времени вызовов, нет журнала при
  падении или таймауте модели).

Вне рамок: замена существующих `print`, журнал в `tools/verify.py` и CI,
ротация и хранение журналов, разграничение прав средствами кода.

## Размещение

Новый пакет `prototype/src/prototype/journal/`:

| Файл | Ответственность | Зависимости |
|---|---|---|
| `__init__.py` | Публичный API: `Journal`, `JournalCallbackHandler` (лениво), `record_generation_result` | — |
| `core.py` | Класс `Journal`: события, буфер до `attach`, запись, вырезание секретов, `close` | stdlib |
| `render.py` | `render_markdown(events) -> str` для `journal.md` | stdlib |
| `summary.py` | `record_generation_result(journal, data)`: события `tests.run` и `repair` из результата генератора | stdlib |
| `langchain.py` | `JournalCallbackHandler(BaseCallbackHandler)` | `langchain_core` (импорт только в этом файле) |

Импорт `prototype.journal` не загружает LangChain, LLM и `.env`
(как `prototype.storage`). `JournalCallbackHandler` экспортируется лениво
через `__getattr__` пакета.

## Файлы журнала

В папке прогона, рядом с `run-metadata.json`:

```
<report_dir>/<YYYY-MM-DD_HHMMSS>_<uuid>/
  journal.jsonl     одно событие на строку
  journal.md        хронология для людей, собирается в close()
  run-metadata.json (существует; дополняется блоком launch)
```

При ошибке входных данных журнал пишется в папку `input-error`, которую создаёт
`_report_input_error`; в режиме `--inspect-contract` — в папку его отчёта.

## Формат события

Одна строка `journal.jsonl` — JSON-объект (UTF-8, `ensure_ascii=False`,
`allow_nan=False`):

```json
{"seq": 7, "ts": "2026-10-16T12:00:03.412+00:00", "elapsed_s": 3.412,
 "event": "llm.call", "status": "ok",
 "data": {"model": "deepseek-ai/DeepSeek-V4-Flash", "duration_s": 2.18,
          "input_tokens": 1830, "output_tokens": 412, "total_tokens": 2242}}
```

| Поле | Значение |
|---|---|
| `seq` | номер события в прогоне, с 1, без пропусков |
| `ts` | время UTC, ISO 8601, миллисекунды |
| `elapsed_s` | секунды от создания журнала, 3 знака |
| `event` | тип события (таблица ниже) |
| `status` | `ok`, `error`, `timeout` или `info` |
| `data` | JSON-объект с полями события; строки длиннее 2000 символов обрезаются с пометкой `…[обрезано]` |

## События

| ТЗ 4.1.8.2 | `event` | `data` | Источник |
|---|---|---|---|
| этапы агентного цикла | `run.start` | `mode` (`agent`, `generate`, `testing`, `inspect`), `contract` (как передан в CLI), `launch` (см. 4.1.8.3) | `run()` / `inspect_contract()` |
| | `stage` | `name`: `contract_loaded`, `agent_built`, `agent_started`, `agent_finished`, `report_saved`, `metrics_saved`; доп. поля по этапу (например, `operations` у `contract_loaded`) | `run()` |
| | `run.end` | `exit_code` (значение, которое вернёт `run()`, может быть `null`), `duration_s`, `outcome` (`error`, если `run()` завершается исключением, иначе `ok`) | `run()`, в `finally` |
| вызовы инструментов и результаты | `tool.call` | `tool`, `duration_s`, `summary` — краткий итог результата: поле `status` и (если есть) `tool`, число тестов, без кода и полного вывода | `JournalCallbackHandler.on_tool_start/end/error` |
| обращения к LLM | `llm.call` | `model`, `duration_s`, `input_tokens`, `output_tokens`, `total_tokens`; при ошибке `error` | `JournalCallbackHandler.on_chat_model_start/on_llm_start/on_llm_end/on_llm_error` |
| ошибки разбора контракта | `contract.error` | `stage` (`contract_input`), `error` | `_report_input_error` |
| статусы прогонов тестов | `tests.run` | `attempt`, `runner_status` (одно из 6 значений `RunStatus`), `passed`, `failed`, `errors`, `skipped`, `run_id` сохранённой версии, `tests_dir` | `record_generation_result` по `run_result` и `saved_versions` |
| (самокоррекция) | `repair` | `index`, `target`, `outcome`, `error_signature` | `record_generation_result` по `repair_log` |
| (генерация) | `generation.result` | `status`, `attempts`, `tokens`, `runnability` (доля, если есть) | `record_generation_result` |

`status` события: `llm.call` — `ok`, `error` или `timeout`; `tool.call` — `ok`
или `error`; `tests.run` — `ok` при `completed`, иначе `error`; `contract.error`
— `error`; прочие — `info`.

**Таймаут LLM** определяется по исключению: имя класса или одного из базовых
классов содержит `Timeout` (`openai.APITimeoutError`, `httpx.TimeoutException`,
`TimeoutError`). Пакет `openai` не импортируется.

**Токены** берутся из `usage_metadata` сообщения ответа
(`response.generations[0][0].message.usage_metadata`), иначе из
`response.llm_output["token_usage"]`; при отсутствии — `null`.

**Дедупликация**: обработчик хранит `run_id` начатых вызовов и пишет одно
событие на `run_id` (проверено: LangChain 1.6 не вызывает один обработчик
дважды, даже если он передан и модели, и в `invoke`; защита — на случай других
версий).

## Безопасность данных

- Тексты промптов, ответов модели, контракта и кода тестов не пишутся.
- Каждая строка `data` проходит `redact`: значения переменных окружения
  `DEEPCODE_API_KEY` и `OPENAI_API_KEY` (если заданы и не пустые) заменяются на
  `[REDACTED]`; используется `llm.diagnostics.describe_error` для исключений.
- Параметры запуска (`launch`) содержат только явный белый список полей CLI
  (см. 4.1.8.3); ключи и пути к `.env` не включаются.

## Устойчивость

- `emit` до `attach` кладёт событие в буфер в памяти; `attach(directory)`
  создаёт `journal.jsonl`, пишет буфер и дальше пишет каждое событие сразу
  (`write` + `flush`).
- Повторный `attach` в другой каталог не допускается (`ValueError`).
- Любая ошибка ввода-вывода журнала перехватывается: журнал выводит одно
  предупреждение `[JOURNAL] …` в `stderr`, помечает себя отключённым и больше не
  пишет. Прогон продолжается, код выхода не меняется.
- `close()` идемпотентен: дописывает `journal.md` из событий в памяти (журнал
  хранит копию событий) и закрывает файл. `close()` без `attach` ничего не пишет.
- В `run()` журнал закрывается в `finally`, поэтому при исключении в середине
  прогона `run.end` (с `outcome: error`) и `journal.md` всё равно появляются.

## journal.md

`render_markdown(events)` собирает:

1. Заголовок «Журнал прогона», режим, контракт, начало и длительность, итог (`run.end`).
2. Сводка LLM: число вызовов, ошибок, таймаутов, сумма токенов, суммарное время.
3. Сводка тестов: строки `tests.run` (попытка, статус runner'а, passed/failed).
4. Ошибки: все события со статусом `error`/`timeout`.
5. Хронология: таблица `seq | +сек | событие | статус | кратко`.

Пустые разделы опускаются. Текст экранируется для таблиц Markdown (`|`, переводы строк).

## Изменения в существующем коде

Только добавления; без журнала поведение прежнее.

| Файл | Изменение |
|---|---|
| `llm/model.py` | `build_model(callbacks=None)`: при непустом списке передать `callbacks` в `ChatOpenAI` |
| `__init__.py` → `build_agent` | параметр `callbacks=None`, пробрасывается в `build_model` |
| `__init__.py` → `run()` | создать `Journal` в начале; `run.start`; `stage` после ключевых шагов; `attach(run_dir)` сразу после создания `run_dir`; передать обработчик в `build_agent(callbacks=…)` и `agent.invoke(request, config={"callbacks": [handler]})`; `record_generation_result` для каждого результата генератора; `run.end` и `close()` в `finally` |
| `__init__.py` → `_report_input_error` | необязательный параметр `journal`: `contract.error`, `attach(directory)` к папке `input-error` |
| `__init__.py` → `inspect_contract` | журнал этапов в папке отчёта (без LLM) |
| `__init__.py` → `run-metadata.json` | блок `launch` (4.1.8.3) |

## 4.1.8.3 — параметры запуска

Блок `launch` (в `run.start` и в `run-metadata.json`) собирается из настроек,
которые уже есть в `run()`, белым списком: режим, `contract` (как передан),
`report_dir`, `source_code` (путь или `null`); для `--generate` — `base_url`,
`network`, `blocked_networks`, `runner_timeout_seconds`, лимиты ремонта,
`output_root`; для режима инструментов — `intent`, таймаут инструмента, URL
сервисов (без учётных данных: из URL удаляются `user:password@`). Значение,
которого нет в настройках, не записывается.

## 4.1.8.1 — роли

Раздел «Роли и права» в `prototype/ReadMe.md`:

- **Пользователь** — запускает `tester`, читает отчёты, `journal.md`, метрики;
  не нуждается в `.env`.
- **Администратор/DevOps** — заполняет `.env` (ключ, адрес и модель LLM),
  поднимает стенд и образ runner, задаёт лимиты и сети через параметры CLI.
- В MVP разграничение — средствами окружения: доступ к `.env` и к конфигурации
  есть только у администратора; кода разграничения прав нет.

## Тесты

Новые файлы, без сети, Docker и ключей LLM:

- `tests/test_journal.py` — `Journal`: нумерация `seq`, монотонный `elapsed_s`,
  буфер до `attach` и запись буфера, запись каждой строки сразу (файл читается
  до `close`), повторный `attach` → `ValueError`, вырезание ключа из
  окружения, обрезка длинных строк, ошибка записи (каталог только для чтения
  или подменённый `open`) не бросает исключение и пишет одно предупреждение,
  идемпотентный `close`, `close` без `attach` ничего не создаёт; `render_markdown`:
  разделы, пропуск пустых, экранирование `|`; импорт `prototype.journal` в
  изолированном интерпретаторе не загружает `langchain_core`.
- `tests/test_journal_langchain.py` — обработчик на `GenericFakeChatModel`:
  `llm.call` с токенами и длительностью; ошибка модели → `status: error`;
  исключение с `Timeout` в имени → `timeout`; один обработчик у модели и в
  `invoke` → одно событие; `tool.call` на простом `@tool` — ok и error.
- `tests/test_journal_summary.py` — `record_generation_result`: по одному
  `tests.run` на каждый из 6 статусов runner'а, `repair` из `repair_log`,
  `generation.result`, результат без `run_result` → без `tests.run`.
- `tests/test_journal_cli.py` — `run()` с подменённым `build_agent`
  (поддельный агент без LLM): в папке прогона есть `journal.jsonl` и
  `journal.md`, последовательность `run.start` … `run.end`, `exit_code`
  совпадает с кодом `run()`; битый контракт → `contract.error` в папке
  `input-error`; исключение внутри агента → `run.end` с `outcome: error`;
  в `run-metadata.json` есть `launch` без секретов.

Существующие тесты проекта должны проходить без изменений.

## Документация

- `docs/adr/0005-run-journal.md` — решение, формат, события, безопасность.
- `prototype/ReadMe.md` — разделы «Журнал прогона» и «Роли и права».
- `README.md` — ссылка на ADR 0005.
