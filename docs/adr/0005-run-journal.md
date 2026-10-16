# ADR 0005 — Журнал выполнения прогона

Статус: реализован пакет `prototype.journal`, журнал ведётся на каждый запуск
CLI `tester`. Основание — ТЗ ARQA v3, пп. 4.1.8.1–4.1.8.3.

## Контекст

ТЗ v3 4.1.8.2 требует журнал: этапы агентного цикла, вызовы инструментов и их
результаты, обращения к LLM (время, токены, ошибки и таймауты), ошибки разбора
контракта, статусы прогонов тестов. До этого события выводились через `print` и
терялись; свои события писал только runner (`events.jsonl` в контейнере).

## Решение

Каждый запуск `tester` (режимы агента, `--generate`, `--test-intent`,
`--inspect-contract`) пишет в папку прогона:

```
<report_dir>/<YYYY-MM-DD_HHMMSS>_<uuid>/
  journal.jsonl     одно событие на строку — для программ
  journal.md        хронология — для людей
  run-metadata.json дополнен блоком launch (4.1.8.3)
```

При ошибке входного контракта журнал пишется в папку `input-error`.

Строка `journal.jsonl`:

```json
{"seq": 7, "ts": "2026-10-16T12:00:03.412+00:00", "elapsed_s": 3.412,
 "event": "llm.call", "status": "ok",
 "data": {"model": "deepseek-ai/DeepSeek-V4-Flash", "duration_s": 2.18,
          "input_tokens": 1830, "output_tokens": 412, "total_tokens": 2242}}
```

| Требование 4.1.8.2 | Событие |
|---|---|
| этапы агентного цикла | `run.start` (режим, контракт, `launch`), `stage` (`contract_loaded`, `agent_built`, `agent_started`, `agent_finished`, `report_saved`, `metrics_saved`), `run.end` (код выхода, длительность, `outcome`) |
| вызовы инструментов и результаты | `tool.call`: имя, длительность, краткий итог (`status`, `tests_count`) |
| обращения к LLM | `llm.call`: модель, длительность, токены; `status` = `error` или `timeout` с текстом ошибки |
| ошибки разбора контракта | `contract.error` |
| статусы прогонов тестов | `tests.run`: статус runner'а (`completed`, `collection_error`, `infrastructure_error`, `timeout`, `no_tests`, `interrupted`), passed/failed/errors/skipped, сохранённая версия |
| самокоррекция и итог генерации | `repair`, `generation.result` |

## Как подключено

- `prototype.journal.Journal` (стандартная библиотека) копит события до
  `attach(папка)`, затем пишет каждое событие сразу (`flush`).
- `JournalCallbackHandler` переводит callbacks LangChain в `llm.call` и
  `tool.call`. `journal_context(handler)` ставит его через
  `langchain_core.tracers.context.register_configure_hook`: LangChain добавляет
  обработчик ко всем вызовам моделей и инструментов внутри прогона, включая
  внутренние вызовы генератора и инструменты, которые LangGraph выполняет
  параллельно. Код сборки и вызова агента не менялся.
- `run()` и `inspect_contract()` оборачиваются `_journaled(...)`: `run.start`,
  контекст обработчика, `run.end`, сборка `journal.md`.

## Безопасность

- Тексты промптов, ответов модели, контракта и кода тестов не пишутся.
- Значения `DEEPCODE_API_KEY` и `OPENAI_API_KEY` вырезаются из всех строк;
  строки длиннее 2000 символов обрезаются.
- `launch` — белый список параметров; из URL удаляются учётные данные (сами
  настройки и так отклоняют URL с паролем).

## Устойчивость

Ошибка ввода-вывода журнала не ломает прогон: одно предупреждение `[JOURNAL]` в
stderr, дальше журнал не пишется. При исключении внутри прогона записывается
`run.end` с `outcome: error`, и `journal.md` всё равно собирается.

## Роли (4.1.8.1)

В MVP роли разграничиваются средствами окружения, без кода прав: пользователь
запускает `tester` и читает отчёты и журнал; администратор/DevOps владеет `.env`
(ключ, адрес, модель LLM), стендом, образом runner, сетями и лимитами.
Подробности — в `prototype/ReadMe.md`, раздел «Роли и права».

## Не входит

Замена существующих `print`, журнал в `tools/verify.py` и CI, ротация журналов,
журнал при прямом вызове генератора или runner из Python.
