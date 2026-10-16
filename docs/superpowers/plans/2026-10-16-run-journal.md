# Журнал выполнения прогона (ТЗ v3, п. 4.1.8) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Пакет `prototype.journal`, который на каждый запуск CLI `tester` пишет `journal.jsonl` и `journal.md` в папку прогона, плюс блок `launch` в `run-metadata.json` и раздел «Роли и права» в README.

**Architecture:** `Journal` (stdlib) копит события до `attach(dir)`, потом пишет каждое сразу; `JournalCallbackHandler` переводит callbacks LangChain в события и подключается контекстным хуком `journal_context`, поэтому сигнатуры `build_model`/`build_agent`/`invoke`/`stream` не меняются. `run()` становится обёрткой над прежним телом `_run`.

**Tech Stack:** Python 3.14, стандартная библиотека, `langchain_core` 1.6 (только в `journal/langchain.py`), pytest 9.1.1, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-16-run-journal-design.md`

## Global Constraints

- Импорт `prototype.journal` не загружает `langchain_core`, `prototype.llm.model`, `.env`.
- Новых зависимостей нет; `uv.lock` не меняется.
- Тексты промптов, ответов LLM, контракта и кода тестов в журнал не пишутся.
- Строки в `data` длиннее 2000 символов обрезаются с суффиксом `…[обрезано]`; значения `DEEPCODE_API_KEY`, `OPENAI_API_KEY` из окружения → `[REDACTED]`.
- `status` события ∈ {`ok`, `error`, `timeout`, `info`}.
- Ошибка ввода-вывода журнала: одно предупреждение `[JOURNAL] …` в stderr, журнал отключается, прогон продолжается.
- Существующие тесты не меняются и проходят.
- Команды из `prototype/`: `export UV_PYTHON_INSTALL_DIR=<ASCII-путь>; python -m uv run --locked python -m pytest …`.
- Ветка `feature/4.1.8-run-journal`; коммиты без строк `Co-Authored-By` и упоминаний Claude; стиль `feat(journal): …` по-русски.

## Review Focus

1. Инструменты, которые LangGraph выполняет параллельно в своих потоках, — события `tool.call` всё равно попадают в журнал (тест в Task 4 на графе с `ToolNode`).
2. Кириллица в пути папки прогона и в данных — файлы пишутся в UTF-8, `journal.md` читается (тест в Task 1).
3. Огромный результат инструмента (результат генератора > 5000 символов) — в `tool.call` только краткий итог, строка события короче 4000 символов (тест в Task 4).
4. Ctrl+C / `KeyboardInterrupt` из агента — `run.end` и `journal.md` всё равно появляются (тест в Task 5).
5. `base_url`/URL сервиса с `user:password@` — в `launch` учётные данные вырезаны (тест в Task 3).

---

### Task 1: `Journal` — события, буфер, запись, секреты

**Files:**
- Create: `prototype/src/prototype/journal/__init__.py`, `prototype/src/prototype/journal/core.py`
- Test: `prototype/src/prototype/tests/test_journal.py`

**Interfaces:**
- Produces:
  - `class Journal`: `__init__(self, *, clock=time.monotonic, now=lambda: datetime.now(timezone.utc))`; `emit(self, event: str, status: str = "info", **data) -> None`; `attach(self, directory: Path) -> None`; `close(self, render: Callable[[list[dict]], str] | None = None) -> None`; свойства `events -> list[dict]` (копия), `directory -> Path | None`, `disabled -> bool`.
  - `redact(value: Any) -> Any` — рекурсивно по dict/list/str.
  - `JSONL_NAME = "journal.jsonl"`, `MARKDOWN_NAME = "journal.md"`, `MAX_TEXT = 2000`.
  - `prototype.journal.__init__` реэкспортирует `Journal`; `__getattr__` лениво отдаёт `JournalCallbackHandler`, `journal_context` (Task 4).

- [ ] **Step 1: Тесты** в `test_journal.py`:
  - `test_events_are_numbered_and_timed`: три `emit` → `seq == [1, 2, 3]`, `elapsed_s` неубывающие, `ts` оканчивается на `+00:00`.
  - `test_events_before_attach_are_buffered_then_written`: 2 события → `attach(tmp_path)` → в `journal.jsonl` 2 строки; третье `emit` → 3 строки **до** `close()`.
  - `test_second_attach_to_another_directory_raises`: `pytest.raises(ValueError)`.
  - `test_secrets_from_environment_are_redacted`: `monkeypatch.setenv("DEEPCODE_API_KEY", "sk-private")`; `emit("x", error="failed with sk-private", nested={"a": ["sk-private"]})` → в файле нет `sk-private`, есть `[REDACTED]`.
  - `test_long_strings_are_truncated`: строка 5000 символов → длина `2000 + len("…[обрезано]")`.
  - `test_write_failure_disables_journal_without_raising`: `attach` в каталог, затем подменить `Path.open`/файл так, чтобы `write` бросал `OSError` → `emit` не бросает; `capsys` stderr содержит `[JOURNAL]` ровно один раз; `journal.disabled is True`.
  - `test_close_is_idempotent_and_close_without_attach_writes_nothing`.
  - `test_works_under_non_ascii_directory`: `attach(tmp_path / "журнал")`, событие с `contract="Каталог"` → читается как UTF-8.
  - `test_import_does_not_load_langchain`: изолированный интерпретатор (`-I -S`, как в `test_storage.py`) → `import prototype.journal`; `assert 'langchain_core' not in sys.modules`.
- [ ] **Step 2:** `pytest src/prototype/tests/test_journal.py -q` → FAIL `No module named 'prototype.journal'`.
- [ ] **Step 3:** Реализовать `core.py` и `__init__.py`. Файл открывается в `attach` в режиме `"a"`, UTF-8, каждая строка `json.dumps(event, ensure_ascii=False, allow_nan=False)` + `flush()`. `close(render)` при наличии каталога пишет `render(self.events)` в `journal.md` (по умолчанию render из Task 2 — до Task 2 `close()` без `render` пишет только закрытие файла).
- [ ] **Step 4:** тесты → PASS.
- [ ] **Step 5:** `git commit -m "feat(journal): журнал событий прогона с буфером и вырезанием секретов"`.

---

### Task 2: `journal.md`

**Files:**
- Create: `prototype/src/prototype/journal/render.py`
- Modify: `prototype/src/prototype/journal/core.py` (render по умолчанию в `close`)
- Test: `prototype/src/prototype/tests/test_journal.py`

**Interfaces:**
- Consumes: формат событий из Task 1.
- Produces: `render_markdown(events: list[dict]) -> str`; `Journal.close()` без аргумента использует `render_markdown`.

- [ ] **Step 1: Тесты:**
  - `test_markdown_has_sections_from_spec`: события `run.start`(mode=generate), 2×`llm.call`(input 10/20, output 1/2, одна `status="timeout"`), `tests.run`(runner_status=completed, passed=3, failed=1), `run.end`(exit_code=0) → в тексте есть «# Журнал прогона», «Вызовов LLM: 2», «Токены: 30 / 3», «Таймаутов: 1», строка с `completed` и `3`, раздел «Ошибки» с событием timeout, раздел «Хронология».
  - `test_empty_sections_are_omitted`: только `run.start`/`run.end` → нет «LLM», «Тесты», «Ошибки».
  - `test_table_cells_are_escaped`: `error="a|b\nc"` → в хронологии `a\|b c`.
  - `test_close_writes_markdown_by_default`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Реализовать по разделу «journal.md» спецификации (порядок разделов 1–5).
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat(journal): читаемая хронология journal.md"`.

---

### Task 3: параметры запуска и итоги генератора

**Files:**
- Create: `prototype/src/prototype/journal/launch.py`, `prototype/src/prototype/journal/summary.py`
- Test: `prototype/src/prototype/tests/test_journal_summary.py`

**Interfaces:**
- Consumes: `Journal.emit` (Task 1).
- Produces:
  - `launch_parameters(*, mode: str, contract: str, report_dir, source_code, generation_settings=None, testing_settings=None) -> dict` — белый список из спецификации (4.1.8.3); пути → `str`; URL через `strip_credentials(url: str) -> str`.
  - `record_generation_result(journal: Journal, data: dict) -> None` — события `generation.result`, `repair` (по `repair_log`), `tests.run` (по `run_result` + последняя запись `saved_versions`).

- [ ] **Step 1: Тесты:**
  - `test_launch_for_generate_mode`: `GenerationSettings(contract_path="c.yaml", base_url="http://catalogue:8080", network="n", blocked_networks=("db",), runner_timeout_seconds=30, output_root="out")` → ключи `mode, contract, report_dir, source_code, base_url, network, blocked_networks, runner_timeout_seconds, max_repair_attempts, repair_token_budget, output_root`; значения совпадают.
  - `test_launch_for_testing_mode`: `ToolSettings(...)` → `intent, timeout_seconds, schemathesis_url, microcks_url, evomaster_image`.
  - `test_credentials_are_stripped_from_urls`: `"http://user:pass@host:8080/api"` → `"http://host:8080/api"`; без учётных данных — без изменений.
  - `test_launch_never_contains_secret_like_keys`: в результате нет ключей, содержащих `key`, `token`, `password`.
  - `@pytest.mark.parametrize("status", ["completed","collection_error","infrastructure_error","timeout","no_tests","interrupted"])` `test_runner_status_becomes_tests_run_event`: `data={"status": "...", "attempts": 2, "tokens": {...}, "run_result": {"status": status, "summary": {"passed":3,"failed":1,"error":0,"skipped":0}}, "saved_versions": [{"attempt":2,"run_id":"r2","tests_dir":"t"}], "repair_log": []}` → одно `tests.run`: `runner_status == status`, `passed == 3`, `run_id == "r2"`; `status` события `ok` только для `completed`.
  - `test_repair_log_becomes_repair_events`: 2 записи → 2 события `repair` с `index`, `target`, `outcome`, `error_signature`.
  - `test_result_without_run_result_has_no_tests_run`.
- [ ] **Step 2:** FAIL. Формат `run_result.summary` (из `RunResult.to_dict()`): `{"total", "passed", "failed", "error", "skipped"}`; в событии поле `errors` берётся из `summary["error"]`.
- [ ] **Step 3:** Реализовать. `generation.result`: `status` (поле `pipeline_status`, иначе `status`), `attempts`, `tokens`, `runnability` — `runnability["runnability_percent"]` (может быть `None`) и `expected_total`/`ran_total`.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat(journal): параметры запуска и события по результату генератора"`.

---

### Task 4: обработчик LangChain

**Files:**
- Create: `prototype/src/prototype/journal/langchain.py`
- Test: `prototype/src/prototype/tests/test_journal_langchain.py`

**Interfaces:**
- Consumes: `Journal` (Task 1).
- Produces: `class JournalCallbackHandler(BaseCallbackHandler)`: `__init__(self, journal: Journal)`; методы `on_chat_model_start`, `on_llm_start`, `on_llm_end`, `on_llm_error`, `on_tool_start`, `on_tool_end`, `on_tool_error`. `@contextmanager journal_context(handler)` — `ContextVar` + `register_configure_hook(var, inheritable=True)` (хук регистрируется один раз на модуль).

- [ ] **Step 1: Тесты** (`GenericFakeChatModel`, `@tool`, внутри `with journal_context(handler)`):
  - `test_llm_call_has_model_tokens_and_duration`: ответ с `usage_metadata={"input_tokens":10,"output_tokens":3,"total_tokens":13}` → одно `llm.call`, `status ok`, токены 10/3/13, `duration_s >= 0`.
  - `test_llm_error_is_error_event`: модель, бросающая `RuntimeError("down")` → `llm.call`, `status error`, `"down"` в `error`.
  - `test_timeout_like_exception_is_timeout`: класс `class APITimeoutError(Exception)` → `status timeout`.
  - `test_explicit_config_callbacks_do_not_duplicate`: `model.invoke("q", config={"callbacks": [handler]})` внутри контекста → одно `llm.call`.
  - `test_tool_ok_and_error_events`: два инструмента → `tool.call` ok и error.
  - `test_tools_run_by_langgraph_are_journaled`: граф `StateGraph(MessagesState)` с `ToolNode([ok_tool, failing_tool], handle_tool_errors=True)`, вход `AIMessage` с двумя `tool_calls`, `app.stream(..., stream_mode="values")` → 2 события `tool.call` (ok, error).
  - `test_huge_tool_output_is_summarized`: инструмент возвращает `{"tool":"generate_tests_tool","status":"success","tests":[...]*500}` → строка события в `journal.jsonl` < 4000 символов, `summary` содержит `status` и `tests_count`.
  - `test_nothing_is_recorded_outside_context`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Реализовать. Время — `time.monotonic()` по `run_id` (полный `str(run_id)`). Модель — `serialized`/`metadata` (`ls_model_name`) или `invocation_params["model"]`. Токены — `generations[0][0].message.usage_metadata`, иначе `llm_output["token_usage"]`. Ошибки — `describe_error` из `prototype.llm.diagnostics` (только он, без импорта `llm.model`). Таймаут — `"Timeout"` в имени класса исключения или его MRO. `summary` инструмента: для `dict`/JSON-строки — `tool`, `status`, `pipeline_status`, `tests_count` (`len(tests)`), иначе `type` и длина.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat(journal): события LLM и инструментов из callbacks LangChain"`.

---

### Task 5: подключение к CLI

**Files:**
- Modify: `prototype/src/prototype/__init__.py` (`run`, `_load_inputs`, `_report_input_error`, `inspect_contract`; блок `provenance`)
- Test: `prototype/src/prototype/tests/test_journal_cli.py`

**Interfaces:**
- Consumes: `Journal`, `journal_context`, `JournalCallbackHandler`, `launch_parameters`, `record_generation_result`.
- Produces: `run()` с прежней сигнатурой и возвращаемым значением; `_run(contract_path, *, report_dir, generation_settings, source_code_path, testing_settings, journal)` — прежнее тело; `_load_inputs(..., journal=None)`; `_report_input_error(exc, report_dir=None, *, inspect=False, journal=None)`.

- [ ] **Step 1: Тесты** (паттерн `test_m3_cli.py`: `monkeypatch.chdir(tmp_path)`, контракт `openapi: 3.0.3\npaths:\n  /items:\n    get: {}`, подменённый `build_agent`):
  - `test_generate_run_writes_journal`: `build_agent=lambda **kw: SimpleNamespace(stream=..., invoke=...)` с `ToolMessage` генератора (`pipeline_status=success`, `run_result.status=completed`) → в `tmp_path/<run_dir>/journal.jsonl` события по порядку содержат `run.start`, `stage:contract_loaded`, `stage:agent_built`, `tests.run`, `run.end`; `run.end.exit_code == run(...)`; есть `journal.md`.
  - `test_default_agent_mode_journal`: `build_agent=lambda: SimpleNamespace(invoke=lambda _: {"messages": []})` (без kwargs — как в `test_run_metadata.py`) → журнал есть, `run.start.mode == "agent"`.
  - `test_broken_contract_writes_contract_error`: контракт `"::: not yaml"` → `pytest.raises(ContractInputError)`; в папке `input-error` есть `journal.jsonl` с `contract.error` и `run.end` (`outcome error`).
  - `test_agent_exception_still_closes_journal`: `build_agent` → объект, у которого `stream` бросает `KeyboardInterrupt` → `run()` возвращает 1; `run.end` записан, `journal.md` существует.
  - `test_run_metadata_has_launch_without_secrets`: `GenerationSettings(base_url="http://u:p@catalogue:8080", network="n", …)` → `run-metadata.json["launch"]["base_url"] == "http://catalogue:8080"`.
  - `test_inspect_contract_writes_journal`: `inspect_contract(...)` → в его папке `journal.jsonl` с `run.start.mode == "inspect"`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Переименовать тело `run` в `_run(..., journal)`; новый `run` с той же сигнатурой: определить `mode`, `Journal()`, `emit("run.start", mode=…, contract=str(contract_path), launch=launch_parameters(...))`, `with journal_context(JournalCallbackHandler(journal)): code = _run(...)`; `except BaseException` → `run.end` с `outcome="error"` и повторный `raise`; иначе `run.end` (`exit_code=code`, `outcome="ok"`); `finally: journal.close()`. Импорты `journal_context`/`JournalCallbackHandler` — внутри `run` (ленивый `langchain_core`). В `_run`: `journal.attach(run_dir)` сразу после вычисления `run_dir`; `stage` — `contract_loaded` (operations), `agent_built` (ok / error), `agent_started`, `agent_finished`, `report_saved`, `metrics_saved`; `record_generation_result(journal, data)` для каждого `generation_results`; `provenance["launch"] = launch_parameters(...)`. `_report_input_error`: при `journal` — `emit("contract.error", "error", stage="contract_input", error=…)` и `journal.attach(directory)` после создания папки. `inspect_contract` — собственный `Journal` с `mode="inspect"`, `attach` к его папке, `close` в `finally`.
- [ ] **Step 4:** `pytest src/prototype/tests/test_journal_cli.py -q` → PASS; затем **весь** набор `pytest -q` → нет новых падений относительно `main` (на Windows допустимы 8 известных падений `test_mutation.py`).
- [ ] **Step 5:** `git commit -m "feat(journal): журнал на каждый запуск tester и параметры запуска в метаданных"`.

---

### Task 6: документация и роли

**Files:**
- Create: `docs/adr/0005-run-journal.md`
- Modify: `prototype/ReadMe.md` (разделы «Журнал прогона», «Роли и права»), `README.md` (ссылка на ADR 0005)

- [ ] **Step 1:** ADR 0005: контекст (ТЗ v3 4.1.8.1–4.1.8.3), решение (файлы, формат события, таблица событий, подключение через `journal_context`), безопасность, устойчивость, что не входит.
- [ ] **Step 2:** `prototype/ReadMe.md`: «Журнал прогона» — где лежат файлы, пример строки, как читать `journal.md`; «Роли и права» — по разделу 4.1.8.1 спецификации.
- [ ] **Step 3:** `README.md`: строка `- [Журнал выполнения прогона](docs/adr/0005-run-journal.md).` в список ссылок.
- [ ] **Step 4:** весь набор тестов → как в Task 5 Step 4; `python -m uv run --locked tester --help` → справка без ошибок.
- [ ] **Step 5:** `git commit -m "docs: ADR 0005 о журнале прогона и роли пользователей"`.
