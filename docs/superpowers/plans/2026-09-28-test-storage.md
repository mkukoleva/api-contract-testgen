# Сохранение сгенерированных тестов (ТЗ 2.1.6) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Пакет `prototype.storage`, который сохраняет pytest-код генератора как неизменяемую версию `generated/<slug>/<run_id>/` с `manifest.json`, проверкой синтаксиса и `pytest --collect-only`, плюс tool агента `save_tests_tool`.

**Architecture:** Четыре небольших модуля без сторонних зависимостей при импорте: `contracts.py` (входные/выходные структуры и валидация), `analysis.py` (статический разбор кода через `ast`), `collect.py` (сбор pytest в подпроцессе и разбор вывода), `store.py` (оркестрация, атомарная запись, манифест). `runner/tools.py` получает тонкую обёртку-tool. Спецификация делит логику на `contracts.py` + `store.py`; здесь `store.py` дополнительно разбит на `analysis.py` и `collect.py`, чтобы каждый файл имел одну ответственность — публичный API тот же.

**Tech Stack:** Python 3.14 (окружение проекта через `uv`), стандартная библиотека (`ast`, `hashlib`, `json`, `subprocess`, `tempfile`), pytest 9.1.1, PyYAML (лениво, только для title контракта), LangChain `@tool`.

**Spec:** `docs/superpowers/specs/2026-09-28-test-storage-design.md`

## Global Constraints

- Импорт `prototype.storage` использует только стандартную библиотеку: не загружает `prototype.llm.model`, `prototype.runner.tools`, `yaml`, `langchain`.
- Имя файла теста: `re.fullmatch(r"test_[a-z0-9_]+\.py", name)`; `conftest.py` запрещён.
- Каталог версии: `<output_root>/<contract_slug>/<run_id>/`, `run_id` = `YYYY-MM-DD_HHMMSS`, при совпадении `_2`, `_3`…; существующие версии никогда не перезаписываются.
- `output_root`: аргумент → env `TESTGEN_OUTPUT_DIR` → `prototype/generated`.
- slug: `[a-z0-9-]`, не длиннее 64, fallback — stem файла контракта, затем `contract`.
- Статусы файла: `ok`, `no_tests`, `syntax_error`. Статусы сбора: `ok`, `no_tests`, `errors`, `timeout`, `unavailable`, `skipped`.
- Из окружения сбора удаляются переменные с префиксами `DEEPCODE_`, `OPENAI_`, `PYTEST_ADDOPTS`; задаются `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`.
- Текст одной ошибки сбора обрезается до 4000 символов.
- `manifest.json`: UTF-8, `ensure_ascii=False`, `indent=2`, `allow_nan=False`, `schema_version: 1`.
- Tool не добавляется в `build_agent()` и `SYSTEM_PROMPT`; существующий тест «четыре tools» не меняется.
- Все команды выполняются из каталога `prototype`. Команда тестов: `uv run --locked python -m pytest …` (если `uv` не в `PATH` — `py -m uv run --locked python -m pytest …`).
- Коммиты — в ветку `feature/2.1.6-test-storage`, стиль сообщений репозитория: `feat(storage): …` на русском, с трейлером `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **CRLF и BOM в коде от LLM** — код нормализуется к `\n`, ведущий `\ufeff` удаляется; файл парсится и собирается (тест в Task 2 и Task 4).
2. **Нулевой байт / мусор вместо кода** — файл уходит в `rejected/` со статусом `syntax_error`, сохранение не падает (тест в Task 2).
3. **Кириллица в путях** (домашний каталог пользователя — `C:\Users\Хонор`) — вывод pytest декодируется как UTF-8, nodeid разбираются (тест в Task 4 с каталогом `сохранено`).
4. **Контракт без `info.title` или не-объект YAML** — slug берётся из имени файла, без исключения (тест в Task 4).
5. **Исключение посреди сохранения** — временный каталог удалён, версии нет (тест в Task 4).

---

### Task 1: Структуры данных и пакет `prototype.storage`

**Files:**
- Create: `prototype/src/prototype/storage/__init__.py`
- Create: `prototype/src/prototype/storage/contracts.py`
- Test: `prototype/src/prototype/tests/test_storage.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `GeneratedFile(name: str, code: str)` — frozen dataclass.
  - `SaveRequest(contract_path: Path | str, files: tuple[GeneratedFile, ...], model: str | None = None, generator_meta: Mapping[str, Any] = {}, output_root: Path | str | None = None, collect: bool = True, collect_timeout_seconds: float = 30.0)` — после создания `contract_path: Path`, `output_root: Path | None`, `files: tuple`, `generator_meta: dict` (глубокая JSON-копия).
  - `SavedSuite(run_id: str, run_dir: Path, tests_dir: Path, manifest_path: Path, manifest: dict)`.
  - `TEST_FILE_NAME: re.Pattern`.
  - `prototype.storage` реэкспортирует `GeneratedFile`, `SaveRequest`, `SavedSuite` (и `save_test_suite` после Task 4).

- [ ] **Step 1: Подготовить окружение и зафиксировать базовую линию**

Из каталога `prototype`:

```bash
uv sync --locked
uv run --locked python -m pytest -q
```

Если `uv` не установлен: `py -m pip install --user uv`, затем те же команды через `py -m uv`. `uv` сам скачает Python 3.14.
Expected: `38 passed`.

- [ ] **Step 2: Написать падающие тесты**

Создать `prototype/src/prototype/tests/test_storage.py`:

```python
"""Generated-tests storage checks; no Docker, LLM credentials or network required."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE_DIR = Path(__file__).resolve().parents[2]


def isolated_python(code):
    # -I -S excludes installed dependencies and ignores PYTHONPATH.
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(SOURCE_DIR)!r}); " + code],
        capture_output=True, text=True, timeout=10,
        env={key: value for key, value in os.environ.items()
             if not key.startswith(("DEEPCODE_", "OPENAI_"))},
    )


def test_storage_import_works_without_llm_or_third_party_packages():
    result = isolated_python(
        "import prototype.storage; "
        "assert 'prototype.llm.model' not in sys.modules; "
        "assert 'prototype.runner.tools' not in sys.modules; "
        "assert 'yaml' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", [
    "test_catalogue.py", "test_1.py", "test_get_catalogue_by_id.py",
])
def test_generated_file_accepts_pytest_module_names(name):
    from prototype.storage import GeneratedFile

    assert GeneratedFile(name, "").name == name


@pytest.mark.parametrize("name", [
    "", "conftest.py", "Test_a.py", "test_a.PY", "test_.py", "test-a.py",
    "catalogue.py", "../test_a.py", "a/test_b.py", "a\\test_b.py",
    "test_a.py\n", " test_a.py", "test_ä.py", None,
])
def test_generated_file_rejects_unsafe_or_non_test_names(name):
    from prototype.storage import GeneratedFile

    with pytest.raises(ValueError, match="file name"):
        GeneratedFile(name, "")


def test_generated_file_rejects_non_string_code():
    from prototype.storage import GeneratedFile

    with pytest.raises(ValueError, match="code"):
        GeneratedFile("test_a.py", b"def test_a(): pass")


def test_save_request_normalizes_paths_and_copies_meta(tmp_path):
    from prototype.storage import GeneratedFile, SaveRequest

    meta = {"prompt": "v1", "attempt": 1}
    request = SaveRequest(
        contract_path=str(tmp_path / "api.yaml"),
        files=[GeneratedFile("test_a.py", "")],
        model="m", generator_meta=meta, output_root=str(tmp_path / "out"),
    )
    meta["prompt"] = "changed"
    assert request.contract_path == tmp_path / "api.yaml"
    assert request.output_root == tmp_path / "out"
    assert request.files == (GeneratedFile("test_a.py", ""),)
    assert request.generator_meta == {"prompt": "v1", "attempt": 1}
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kwargs, match", [
    ({"files": ()}, "files"),
    ({"files": ("test_a.py",)}, "files"),
    ({"contract_path": " "}, "contract_path"),
    ({"model": 1}, "model"),
    ({"generator_meta": {"x": object()}}, "generator_meta"),
    ({"generator_meta": {"x": float("nan")}}, "generator_meta"),
    ({"generator_meta": ["x"]}, "generator_meta"),
    ({"output_root": " "}, "output_root"),
    ({"collect": "yes"}, "collect"),
    ({"collect_timeout_seconds": 0}, "collect_timeout_seconds"),
    ({"collect_timeout_seconds": float("inf")}, "collect_timeout_seconds"),
    ({"collect_timeout_seconds": True}, "collect_timeout_seconds"),
])
def test_save_request_rejects_invalid_input(kwargs, match):
    from prototype.storage import GeneratedFile, SaveRequest

    args = {"contract_path": "api.yaml", "files": (GeneratedFile("test_a.py", ""),)}
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        SaveRequest(**args)


def test_save_request_rejects_duplicate_file_names():
    from prototype.storage import GeneratedFile, SaveRequest

    with pytest.raises(ValueError, match="unique"):
        SaveRequest("api.yaml", (GeneratedFile("test_a.py", ""), GeneratedFile("test_a.py", "x = 1")))
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'prototype.storage'`.

- [ ] **Step 4: Реализовать структуры**

Создать `prototype/src/prototype/storage/contracts.py`:

```python
"""Data exchanged with the generated-tests storage. No side effects.

Creating these objects validates input only: nothing is read, written or run.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any


TEST_FILE_NAME = re.compile(r"test_[a-z0-9_]+\.py")


def _non_empty_path(value: Any, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must be a non-empty path")
    return Path(value)


@dataclass(frozen=True)
class GeneratedFile:
    """One pytest module produced by the generator after post-processing.

    Names come from an LLM and are untrusted: only flat test_*.py names are
    accepted, so a file can never escape the version directory. conftest.py is
    rejected because fixtures belong to the trusted runtime (ADR 0002).
    """

    name: str
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not TEST_FILE_NAME.fullmatch(self.name):
            raise ValueError(
                f"file name must match test_[a-z0-9_]+.py, got {self.name!r}"
            )
        if not isinstance(self.code, str):
            raise ValueError(f"code of {self.name} must be a string")


@dataclass(frozen=True)
class SaveRequest:
    """Everything save_test_suite needs to store one generation run."""

    contract_path: Path | str
    files: tuple[GeneratedFile, ...]
    model: str | None = None
    generator_meta: Mapping[str, Any] = field(default_factory=dict)
    output_root: Path | str | None = None
    collect: bool = True
    collect_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_path", _non_empty_path(self.contract_path, "contract_path")
        )
        files = tuple(self.files)
        if not files or not all(isinstance(item, GeneratedFile) for item in files):
            raise ValueError("files must contain at least one GeneratedFile")
        names = [item.name for item in files]
        if len(names) != len(set(names)):
            raise ValueError("files must have unique names")
        object.__setattr__(self, "files", files)
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")
        if not isinstance(self.generator_meta, Mapping):
            raise ValueError("generator_meta must be a JSON-serializable mapping")
        try:
            meta = json.loads(json.dumps(dict(self.generator_meta), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("generator_meta must be a JSON-serializable mapping") from exc
        object.__setattr__(self, "generator_meta", meta)
        if self.output_root is not None:
            object.__setattr__(
                self, "output_root", _non_empty_path(self.output_root, "output_root")
            )
        if not isinstance(self.collect, bool):
            raise ValueError("collect must be a boolean")
        timeout = self.collect_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("collect_timeout_seconds must be a finite positive number")


@dataclass(frozen=True)
class SavedSuite:
    """A stored, immutable version; tests_dir is what RunConfig.tests_dir expects."""

    run_id: str
    run_dir: Path
    tests_dir: Path
    manifest_path: Path
    manifest: dict
```

Создать `prototype/src/prototype/storage/__init__.py`:

```python
"""Storage of generated pytest suites as immutable, versioned directories (ТЗ 2.1.6)."""

from .contracts import GeneratedFile, SaveRequest, SavedSuite

__all__ = ["GeneratedFile", "SaveRequest", "SavedSuite"]
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: все PASS.

- [ ] **Step 6: Коммит**

```bash
git add src/prototype/storage src/prototype/tests/test_storage.py
git commit -m "feat(storage): структуры запроса и результата сохранения тестов"
```

---

### Task 2: Статический разбор файла (`analysis.py`)

**Files:**
- Create: `prototype/src/prototype/storage/analysis.py`
- Test: `prototype/src/prototype/tests/test_storage.py` (дописать)

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `normalize_code(code: str) -> str` — `\r\n`/`\r` → `\n`, удаляет ведущий `\ufeff`.
  - `FileAnalysis(status: str, test_functions: int | None, warnings: tuple[str, ...], error: str | None)` — frozen dataclass.
  - `analyze_test_file(code: str, name: str) -> FileAnalysis` — принимает уже нормализованный код; не исполняет его.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `test_storage.py`:

```python
def test_analysis_counts_functions_methods_and_async_tests():
    from prototype.storage.analysis import analyze_test_file

    code = (
        '"""Docstring."""\n'
        "import pytest\n"
        "BASE = '/catalogue'\n"
        "timeout: float = 5.0\n\n"
        "def helper():\n    pass\n\n"
        "def test_one():\n    pass\n\n"
        "async def test_two():\n    pass\n\n"
        "class TestTags:\n"
        "    def test_three(self):\n        pass\n"
        "    def helper(self):\n        pass\n\n"
        "class Helper:\n    def test_ignored(self):\n        pass\n"
    )
    result = analyze_test_file(code, "test_a.py")
    assert result.status == "ok"
    assert result.test_functions == 3
    assert result.warnings == ()
    assert result.error is None


def test_analysis_marks_file_without_tests():
    from prototype.storage.analysis import analyze_test_file

    result = analyze_test_file("import pytest\n", "test_a.py")
    assert (result.status, result.test_functions) == ("no_tests", 0)


@pytest.mark.parametrize("code", [
    "def test_a(:\n    pass\n",
    "def test_a():\npass\n",
    "x = 1\x00\n",
])
def test_analysis_reports_syntax_error_without_raising(code):
    from prototype.storage.analysis import analyze_test_file

    result = analyze_test_file(code, "test_a.py")
    assert result.status == "syntax_error"
    assert result.test_functions is None
    assert result.error


def test_analysis_reports_line_of_syntax_error():
    from prototype.storage.analysis import analyze_test_file

    result = analyze_test_file("x = 1\ndef test_a(:\n    pass\n", "test_a.py")
    assert result.error.startswith("line 2:")


@pytest.mark.parametrize("statement", [
    "requests.get('http://example.com')",
    "import time\ntime.sleep(1)",
    "if True:\n    x = 1",
    "for i in range(3):\n    pass",
    "with open('x') as f:\n    pass",
])
def test_analysis_warns_about_top_level_code(statement):
    from prototype.storage.analysis import analyze_test_file

    result = analyze_test_file(statement + "\n\ndef test_a():\n    pass\n", "test_a.py")
    assert result.status == "ok"
    assert result.warnings == ("top_level_code",)


def test_normalize_code_handles_crlf_cr_and_bom():
    from prototype.storage.analysis import analyze_test_file, normalize_code

    code = normalize_code("\ufeffdef test_a():\r\n    pass\r\rx = 1\n")
    assert code == "def test_a():\n    pass\n\nx = 1\n"
    assert analyze_test_file(code, "test_a.py").status == "ok"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q -k "analysis or normalize"`
Expected: FAIL — `ModuleNotFoundError: No module named 'prototype.storage.analysis'`.

- [ ] **Step 3: Реализовать разбор**

Создать `prototype/src/prototype/storage/analysis.py`:

```python
"""Static checks of generated test modules. Code is parsed with ast, never run."""

import ast
from dataclasses import dataclass


# Statements that do not run anything at import time beyond defining names.
_DEFINITIONS = (
    ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
    ast.ClassDef, ast.Assign, ast.AnnAssign,
)


@dataclass(frozen=True)
class FileAnalysis:
    status: str                  # "ok", "no_tests" or "syntax_error"
    test_functions: int | None   # None when the file could not be parsed
    warnings: tuple[str, ...]
    error: str | None


def normalize_code(code: str) -> str:
    """Use \\n line endings and drop a leading BOM, as LLM output may have both."""
    return code.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _count_tests(tree: ast.Module) -> int:
    # Mirrors pytest defaults: test* functions, Test* classes with test* methods.
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += node.name.startswith("test")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            count += sum(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("test")
                for item in node.body
            )
    return count


def analyze_test_file(code: str, name: str) -> FileAnalysis:
    try:
        tree = ast.parse(code, filename=name)
    except (SyntaxError, ValueError) as exc:
        lineno = getattr(exc, "lineno", None)
        message = getattr(exc, "msg", None) or str(exc)
        error = f"line {lineno}: {message}" if lineno else message
        return FileAnalysis("syntax_error", None, (), error)

    body = tree.body[1:] if tree.body and _is_docstring(tree.body[0]) else tree.body
    warnings = (
        ("top_level_code",)
        if any(not isinstance(node, _DEFINITIONS) for node in body)
        else ()
    )
    tests = _count_tests(tree)
    return FileAnalysis("ok" if tests else "no_tests", tests, warnings, None)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: все PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/prototype/storage/analysis.py src/prototype/tests/test_storage.py
git commit -m "feat(storage): статический разбор сгенерированных модулей через ast"
```

---

### Task 3: Сбор pytest в подпроцессе (`collect.py`)

**Files:**
- Create: `prototype/src/prototype/storage/collect.py`
- Test: `prototype/src/prototype/tests/test_storage.py` (дописать)

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `collect_tests(tests_dir: Path, timeout_seconds: float) -> dict` с ключами `status`, `collected: int | None`, `nodeids: list[str]`, `errors: list[str]`, `duration_seconds: float`.
  - `skipped_collection() -> dict` — тот же формат, `status="skipped"`, `collected=None`.
  - `parse_nodeids(stdout: str) -> list[str]`, `parse_collection_errors(stdout: str) -> list[str]`.
  - `MAX_ERROR_CHARS = 4000`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `test_storage.py`:

```python
COLLECT_OUTPUT = """\
test_good.py::test_a
test_good.py::TestX::test_b
test_good.py::test_p[1]

=================================== ERRORS ====================================
________________________ ERROR collecting test_bad.py _________________________
ImportError while importing test module 'C:\\\\tmp\\\\test_bad.py'.
E   ModuleNotFoundError: No module named 'nonexistent_mod'
________________________ ERROR collecting test_worse.py _______________________
E   NameError: name 'x' is not defined
=========================== short test summary info ===========================
ERROR test_bad.py
ERROR test_worse.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!
3 tests collected, 2 errors in 0.46s
"""


def test_parse_nodeids_reads_quiet_collect_output():
    from prototype.storage.collect import parse_nodeids

    assert parse_nodeids(COLLECT_OUTPUT) == [
        "test_good.py::test_a", "test_good.py::TestX::test_b", "test_good.py::test_p[1]",
    ]
    assert parse_nodeids("\nno tests collected in 0.01s\n") == []


def test_parse_collection_errors_keeps_one_entry_per_module():
    from prototype.storage.collect import parse_collection_errors

    errors = parse_collection_errors(COLLECT_OUTPUT)
    assert len(errors) == 2
    assert errors[0].startswith("test_bad.py:")
    assert "nonexistent_mod" in errors[0]
    assert errors[1].startswith("test_worse.py:")
    assert "short test summary" not in errors[1]


def test_parse_collection_errors_truncates_long_messages():
    from prototype.storage.collect import MAX_ERROR_CHARS, parse_collection_errors

    output = "____ ERROR collecting test_a.py ____\n" + "E" * 10_000 + "\n"
    assert len(parse_collection_errors(output)[0]) == MAX_ERROR_CHARS


def write_tests(directory, **files):
    directory.mkdir(parents=True, exist_ok=True)
    for name, code in files.items():
        (directory / f"{name}.py").write_text(code, encoding="utf-8")
    return directory


def test_collect_tests_lists_nodeids(tmp_path):
    from prototype.storage.collect import collect_tests

    tests_dir = write_tests(
        tmp_path / "tests",
        test_a="import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\ndef test_p(x):\n    pass\n",
    )
    result = collect_tests(tests_dir, 60)
    assert result["status"] == "ok"
    assert result["collected"] == 2
    assert result["nodeids"] == ["test_a.py::test_p[1]", "test_a.py::test_p[2]"]
    assert result["errors"] == []
    assert result["duration_seconds"] >= 0


def test_collect_tests_reports_import_errors_and_keeps_good_modules(tmp_path):
    from prototype.storage.collect import collect_tests

    tests_dir = write_tests(
        tmp_path / "tests",
        test_bad="import nonexistent_module_xyz\n\ndef test_a():\n    pass\n",
        test_good="def test_b():\n    pass\n",
    )
    result = collect_tests(tests_dir, 60)
    assert result["status"] == "errors"
    assert result["nodeids"] == ["test_good.py::test_b"]
    assert len(result["errors"]) == 1
    assert "nonexistent_module_xyz" in result["errors"][0]


def test_collect_tests_reports_modules_without_tests(tmp_path):
    from prototype.storage.collect import collect_tests

    result = collect_tests(write_tests(tmp_path / "tests", test_a="X = 1\n"), 60)
    assert (result["status"], result["collected"]) == ("no_tests", 0)


def test_collect_tests_stops_at_timeout(tmp_path):
    from prototype.storage.collect import collect_tests

    tests_dir = write_tests(
        tmp_path / "tests",
        test_a="import time\ntime.sleep(30)\n\ndef test_a():\n    pass\n",
    )
    result = collect_tests(tests_dir, 1)
    assert result["status"] == "timeout"
    assert result["collected"] is None
    assert result["duration_seconds"] < 20


def test_collect_tests_leaves_no_cache_files(tmp_path):
    from prototype.storage.collect import collect_tests

    tests_dir = write_tests(tmp_path / "tests", test_a="def test_a():\n    assert 1\n")
    collect_tests(tests_dir, 60)
    assert sorted(path.name for path in tmp_path.rglob("*")) == ["test_a.py", "tests"]


def test_collect_tests_hides_llm_credentials_from_generated_code(tmp_path, monkeypatch):
    from prototype.storage.collect import collect_tests

    monkeypatch.setenv("DEEPCODE_API_KEY", "secret")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--this-option-does-not-exist")
    tests_dir = write_tests(
        tmp_path / "tests",
        test_a="import os\nassert 'DEEPCODE_API_KEY' not in os.environ\n\ndef test_a():\n    pass\n",
    )
    assert collect_tests(tests_dir, 60)["status"] == "ok"


def test_skipped_collection_has_the_common_shape():
    from prototype.storage.collect import skipped_collection

    assert skipped_collection() == {
        "status": "skipped", "collected": None, "nodeids": [], "errors": [],
        "duration_seconds": 0.0,
    }
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q -k "collect"`
Expected: FAIL — `ModuleNotFoundError: No module named 'prototype.storage.collect'`.

- [ ] **Step 3: Реализовать сбор**

Создать `prototype/src/prototype/storage/collect.py`:

```python
"""Collect generated tests with pytest in a subprocess; test bodies are never run.

Collection imports every test module, so module-level code DOES execute. This is
not a sandbox and not network isolation: that belongs to the runner (ТЗ 2.2.6,
ADR 0002). Pass collect=False once collection happens inside the isolated runner.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


MAX_ERROR_CHARS = 4000
_HIDDEN_ENV_PREFIXES = ("DEEPCODE_", "OPENAI_", "PYTEST_ADDOPTS")
_ERROR_HEADER = re.compile(r"^_{3,} ERROR collecting (?P<path>.+?) _{3,}$")
_SECTION_BORDER = re.compile(r"^(={3,}|_{3,}|!{3,})")


def _result(status, collected, nodeids, errors, started) -> dict:
    return {
        "status": status,
        "collected": collected,
        "nodeids": nodeids,
        "errors": errors,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }


def skipped_collection() -> dict:
    return {"status": "skipped", "collected": None, "nodeids": [], "errors": [],
            "duration_seconds": 0.0}


def parse_nodeids(stdout: str) -> list[str]:
    """Read `path::name` lines that `pytest --collect-only -q` prints first."""
    nodeids = []
    for line in stdout.splitlines():
        line = line.rstrip()
        if not line or _SECTION_BORDER.match(line):
            break
        if "::" in line:
            nodeids.append(line.replace("\\", "/"))
    return nodeids


def parse_collection_errors(stdout: str) -> list[str]:
    """Return one message per module from the ERRORS section."""
    errors: list[list[str]] = []
    current: list[str] | None = None
    for line in stdout.splitlines():
        header = _ERROR_HEADER.match(line)
        if header:
            current = [f"{header['path']}:"]
            errors.append(current)
        elif current is not None and _SECTION_BORDER.match(line):
            current = None
        elif current is not None:
            current.append(line)
    return ["\n".join(lines).strip()[:MAX_ERROR_CHARS] for lines in errors]


def collect_tests(tests_dir: Path, timeout_seconds: float) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(_HIDDEN_ENV_PREFIXES)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # An empty ini outside the version keeps project pytest settings out.
    fd, empty_ini = tempfile.mkstemp(suffix=".ini")
    os.close(fd)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "--rootdir", str(tests_dir),
             "-c", empty_ini, str(tests_dir)],
            cwd=tests_dir.parent, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _result("timeout", None, [],
                       [f"collection exceeded {timeout_seconds} s"], started)
    except OSError as exc:
        return _result("unavailable", None, [], [f"cannot start pytest: {exc}"], started)
    finally:
        os.unlink(empty_ini)

    if completed.returncode != 0 and "No module named pytest" in completed.stderr:
        return _result("unavailable", None, [], [completed.stderr.strip()], started)

    nodeids = parse_nodeids(completed.stdout)
    errors = parse_collection_errors(completed.stdout)
    if completed.returncode == 0 and not errors:
        status = "ok"
    elif completed.returncode == 5 and not errors:
        status = "no_tests"
    else:
        status = "errors"
        if not errors:
            output = (completed.stdout + completed.stderr).strip()
            errors = [(output or f"pytest exited with code {completed.returncode}")[:MAX_ERROR_CHARS]]
    return _result(status, len(nodeids), nodeids, errors, started)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: все PASS (сбор на Windows занимает ~0.5 с на вызов).

- [ ] **Step 5: Коммит**

```bash
git add src/prototype/storage/collect.py src/prototype/tests/test_storage.py
git commit -m "feat(storage): сбор сгенерированных тестов через pytest --collect-only"
```

---

### Task 4: Сохранение версии (`store.py`)

**Files:**
- Create: `prototype/src/prototype/storage/store.py`
- Modify: `prototype/src/prototype/storage/__init__.py`
- Test: `prototype/src/prototype/tests/test_storage.py` (дописать)

**Interfaces:**
- Consumes: `SaveRequest`, `SavedSuite` (Task 1); `normalize_code`, `analyze_test_file`, `FileAnalysis` (Task 2); `collect_tests`, `skipped_collection` (Task 3).
- Produces:
  - `save_test_suite(request: SaveRequest) -> SavedSuite`; бросает `FileNotFoundError`, если контракта нет.
  - `make_slug(title: str, fallback: str) -> str`, `read_contract_title(text: str) -> str`.
  - `DEFAULT_OUTPUT_ROOT: Path`, `MANIFEST_SCHEMA_VERSION = 1`.
  - `_now() -> datetime` — точка подмены времени в тестах.
  - `prototype.storage.save_test_suite` — реэкспорт.

- [ ] **Step 1: Написать падающие тесты**

В блок импортов в начале `test_storage.py` добавить (сохраняя алфавитный порядок стандартных модулей):

```python
from datetime import datetime, timedelta, timezone
import hashlib
```

Дописать в конец `test_storage.py`:

```python
CONTRACT = Path(__file__).parent / "fixtures" / "demo_openapi.yaml"
GOOD = (
    "def test_list():\n    assert 1 + 1 == 2\n\n\n"
    "class TestTags:\n    def test_tags(self):\n        assert True\n"
)
FIXED_TIME = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone(timedelta(hours=7)))


def save(root, *files, contract=CONTRACT, **kwargs):
    from prototype.storage import GeneratedFile, SaveRequest, save_test_suite

    return save_test_suite(SaveRequest(
        contract_path=contract,
        files=tuple(GeneratedFile(name, code) for name, code in files),
        output_root=root, **kwargs,
    ))


def test_save_writes_version_with_manifest(tmp_path, monkeypatch):
    from prototype.storage import store

    monkeypatch.setattr(store, "_now", lambda: FIXED_TIME)
    saved = save(tmp_path / "generated", ("test_catalogue.py", GOOD),
                 model="deepseek", generator_meta={"prompt": "v1"})

    run_dir = tmp_path / "generated" / "demo-catalogue-api" / "2026-09-28_120000"
    assert saved.run_id == "2026-09-28_120000"
    assert saved.run_dir == run_dir
    assert saved.tests_dir == run_dir / "tests"
    assert saved.manifest_path == run_dir / "manifest.json"
    assert json.loads(saved.manifest_path.read_text(encoding="utf-8")) == saved.manifest

    manifest = saved.manifest
    assert manifest["schema_version"] == 1
    assert manifest["created_at"] == "2026-09-28T12:00:00+07:00"
    assert manifest["contract"] == {
        "path": str(CONTRACT),
        "sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
        "title": "Demo Catalogue API",
        "slug": "demo-catalogue-api",
    }
    assert manifest["generator"] == {"model": "deepseek", "meta": {"prompt": "v1"}}
    stored = (saved.tests_dir / "test_catalogue.py").read_bytes()
    assert manifest["files"] == [{
        "name": "test_catalogue.py", "location": "tests",
        "sha256": hashlib.sha256(stored).hexdigest(), "status": "ok",
        "test_functions": 2, "warnings": [], "error": None,
    }]
    assert manifest["collection"]["status"] == "ok"
    assert manifest["collection"]["nodeids"] == [
        "test_catalogue.py::test_list", "test_catalogue.py::TestTags::test_tags",
    ]
    assert manifest["summary"] == {
        "files_total": 1, "files_ok": 1, "files_no_tests": 0, "files_rejected": 0,
        "test_functions": 2, "collected": 2,
    }
    assert sorted(p.name for p in run_dir.rglob("*")) == [
        "manifest.json", "test_catalogue.py", "tests",
    ]


def test_save_moves_syntax_errors_to_rejected_and_collects_the_rest(tmp_path):
    saved = save(tmp_path, ("test_good.py", GOOD), ("test_broken.py", "def test_a(:\n"))

    assert (saved.run_dir / "rejected" / "test_broken.py").is_file()
    assert not (saved.tests_dir / "test_broken.py").exists()
    broken = next(f for f in saved.manifest["files"] if f["name"] == "test_broken.py")
    assert broken["location"] == "rejected"
    assert broken["status"] == "syntax_error"
    assert broken["error"].startswith("line 1:")
    assert saved.manifest["collection"]["status"] == "ok"
    assert saved.manifest["summary"]["files_rejected"] == 1
    assert saved.manifest["summary"]["collected"] == 2


def test_save_records_import_errors_for_self_repair(tmp_path):
    saved = save(tmp_path, ("test_a.py", "import nonexistent_module_xyz\n\ndef test_a():\n    pass\n"))

    assert saved.manifest["collection"]["status"] == "errors"
    assert "nonexistent_module_xyz" in saved.manifest["collection"]["errors"][0]


def test_save_marks_file_without_tests(tmp_path):
    saved = save(tmp_path, ("test_a.py", "X = 1\n"))

    assert saved.manifest["files"][0]["status"] == "no_tests"
    assert saved.manifest["collection"]["status"] == "no_tests"
    assert saved.manifest["summary"]["files_no_tests"] == 1


def test_save_skips_collection_when_disabled_or_nothing_parses(tmp_path):
    disabled = save(tmp_path / "a", ("test_a.py", GOOD), collect=False)
    assert disabled.manifest["collection"]["status"] == "skipped"
    assert disabled.manifest["summary"]["collected"] is None

    rejected_only = save(tmp_path / "b", ("test_a.py", "def test_a(:\n"))
    assert rejected_only.manifest["collection"]["status"] == "skipped"
    assert rejected_only.tests_dir.is_dir()
    assert list(rejected_only.tests_dir.iterdir()) == []


def test_save_never_overwrites_a_version_with_the_same_timestamp(tmp_path, monkeypatch):
    from prototype.storage import store

    monkeypatch.setattr(store, "_now", lambda: FIXED_TIME)
    first = save(tmp_path, ("test_a.py", GOOD), collect=False)
    before = {p: p.read_bytes() for p in first.run_dir.rglob("*") if p.is_file()}
    second = save(tmp_path, ("test_a.py", "def test_other():\n    pass\n"), collect=False)
    third = save(tmp_path, ("test_a.py", GOOD), collect=False)

    assert (first.run_id, second.run_id, third.run_id) == (
        "2026-09-28_120000", "2026-09-28_120000_2", "2026-09-28_120000_3",
    )
    assert {p: p.read_bytes() for p in first.run_dir.rglob("*") if p.is_file()} == before


def test_save_timeout_is_recorded_with_top_level_warning(tmp_path):
    saved = save(tmp_path, ("test_a.py", "import time\ntime.sleep(30)\n\ndef test_a():\n    pass\n"),
                 collect_timeout_seconds=1)

    assert saved.manifest["collection"]["status"] == "timeout"
    assert saved.manifest["files"][0]["warnings"] == ["top_level_code"]
    assert saved.manifest["summary"]["collected"] is None


def test_save_normalizes_line_endings_and_bom(tmp_path):
    saved = save(tmp_path, ("test_a.py", "\ufeffdef test_a():\r\n    pass\r\n"))

    stored = (saved.tests_dir / "test_a.py").read_bytes()
    assert stored == b"def test_a():\n    pass\n"
    assert saved.manifest["collection"]["status"] == "ok"


def test_save_works_under_non_ascii_directories(tmp_path):
    saved = save(tmp_path / "сохранено", ("test_a.py", GOOD))

    assert saved.manifest["collection"]["status"] == "ok"
    assert saved.manifest["collection"]["collected"] == 2


def test_save_requires_existing_contract_and_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        save(tmp_path / "out", ("test_a.py", GOOD), contract=tmp_path / "missing.yaml")
    assert not (tmp_path / "out").exists()


def test_save_cleans_up_when_interrupted(tmp_path, monkeypatch):
    from prototype.storage import store

    def boom(*args, **kwargs):
        raise RuntimeError("collector crashed")

    monkeypatch.setattr(store, "collect_tests", boom)
    with pytest.raises(RuntimeError, match="collector crashed"):
        save(tmp_path, ("test_a.py", GOOD))
    assert list((tmp_path / "demo-catalogue-api").iterdir()) == []


def test_save_uses_env_output_root(tmp_path, monkeypatch):
    from prototype.storage import GeneratedFile, SaveRequest, save_test_suite

    monkeypatch.setenv("TESTGEN_OUTPUT_DIR", str(tmp_path / "from-env"))
    saved = save_test_suite(SaveRequest(CONTRACT, (GeneratedFile("test_a.py", GOOD),), collect=False))
    assert saved.run_dir.parent.parent == tmp_path / "from-env"


def test_default_output_root_is_outside_the_package():
    from prototype.storage.store import DEFAULT_OUTPUT_ROOT

    assert DEFAULT_OUTPUT_ROOT == SOURCE_DIR.parent / "generated"


@pytest.mark.parametrize("contract_text, expected_slug", [
    ('{"openapi": "3.0.3", "info": {"title": "Orders API v2"}}', "orders-api-v2"),
    ("openapi: 3.0.3\ninfo:\n  title: Каталог\n", "my-contract"),
    ("openapi: 3.0.3\n", "my-contract"),
    ("- just\n- a list\n", "my-contract"),
    ("{not: [valid", "my-contract"),
])
def test_save_derives_slug_from_title_or_file_name(tmp_path, contract_text, expected_slug):
    contract = tmp_path / "My_Contract.yaml"
    contract.write_text(contract_text, encoding="utf-8")

    saved = save(tmp_path / "out", ("test_a.py", GOOD), contract=contract, collect=False)
    assert saved.manifest["contract"]["slug"] == expected_slug
    assert saved.run_dir.parent.name == expected_slug


@pytest.mark.parametrize("title, fallback, expected", [
    ("Demo Catalogue API", "x", "demo-catalogue-api"),
    ("  --Sock  Shop!!  ", "x", "sock-shop"),
    ("", "", "contract"),
    ("Каталог", "", "contract"),
])
def test_make_slug(title, fallback, expected):
    from prototype.storage.store import make_slug

    assert make_slug(title, fallback) == expected


def test_make_slug_limits_length_without_trailing_dash():
    from prototype.storage.store import make_slug

    slug = make_slug("a" * 63 + " b", "x")
    assert len(slug) <= 64
    assert not slug.endswith("-")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: новые тесты FAIL — `ImportError: cannot import name 'save_test_suite'` / `No module named 'prototype.storage.store'`.

- [ ] **Step 3: Реализовать сохранение**

Создать `prototype/src/prototype/storage/store.py`:

```python
"""Save generated pytest modules as an immutable, versioned directory (ТЗ 2.1.6).

<output_root>/<contract_slug>/<run_id>/{tests/, rejected/, manifest.json}

A version is written into a hidden temporary directory and renamed in one step,
so a half-written version never appears under its run_id. Existing versions are
never overwritten: a clash on the same second gets a _2, _3... suffix.
"""

from datetime import datetime
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import re
import shutil

from .analysis import analyze_test_file, normalize_code
from .collect import collect_tests, skipped_collection
from .contracts import SaveRequest, SavedSuite


MANIFEST_SCHEMA_VERSION = 1
# prototype/src/prototype/storage/store.py -> prototype/generated
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[3] / "generated"
_SLUG_LIMIT = 64


def _now() -> datetime:
    return datetime.now().astimezone()


def make_slug(title: str, fallback: str) -> str:
    for candidate in (title, fallback):
        slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
        slug = slug[:_SLUG_LIMIT].rstrip("-")
        if slug:
            return slug
    return "contract"


def read_contract_title(text: str) -> str:
    """Return info.title of a JSON/YAML contract, or "" if it cannot be read."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # lazy: importing prototype.storage stays stdlib-only

            data = yaml.safe_load(text)
        except Exception:
            return ""
    info = data.get("info") if isinstance(data, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return title.strip() if isinstance(title, str) else ""


def _output_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    from_env = os.environ.get("TESTGEN_OUTPUT_DIR", "").strip()
    return Path(from_env) if from_env else DEFAULT_OUTPUT_ROOT


def _reserve_run_dir(contract_dir: Path, created_at: datetime) -> tuple[str, Path]:
    base = created_at.strftime("%Y-%m-%d_%H%M%S")
    for index in count(1):
        run_id = base if index == 1 else f"{base}_{index}"
        if (contract_dir / run_id).exists():
            continue
        tmp_dir = contract_dir / f".{run_id}.tmp"
        try:
            tmp_dir.mkdir()  # atomic: a concurrent save cannot take the same tmp_dir
        except FileExistsError:
            continue
        return run_id, tmp_dir
    raise AssertionError("unreachable")


def _summary(files: list[dict], collection: dict) -> dict:
    measured = collection["status"] in {"ok", "no_tests", "errors"}
    return {
        "files_total": len(files),
        "files_ok": sum(item["status"] == "ok" for item in files),
        "files_no_tests": sum(item["status"] == "no_tests" for item in files),
        "files_rejected": sum(item["status"] == "syntax_error" for item in files),
        "test_functions": sum(item["test_functions"] or 0 for item in files),
        "collected": collection["collected"] if measured else None,
    }


def save_test_suite(request: SaveRequest) -> SavedSuite:
    contract_path = request.contract_path
    if not contract_path.is_file():
        raise FileNotFoundError(f"contract not found: {contract_path}")
    contract_bytes = contract_path.read_bytes()
    title = read_contract_title(contract_bytes.decode("utf-8", errors="replace"))
    slug = make_slug(title, contract_path.stem)

    contract_dir = _output_root(request.output_root) / slug
    contract_dir.mkdir(parents=True, exist_ok=True)
    created_at = _now()
    run_id, tmp_dir = _reserve_run_dir(contract_dir, created_at)
    try:
        (tmp_dir / "tests").mkdir()
        files = []
        for generated in request.files:
            code = normalize_code(generated.code)
            analysis = analyze_test_file(code, generated.name)
            location = "rejected" if analysis.status == "syntax_error" else "tests"
            data = code.encode("utf-8")
            (tmp_dir / location).mkdir(exist_ok=True)
            (tmp_dir / location / generated.name).write_bytes(data)
            files.append({
                "name": generated.name,
                "location": location,
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": analysis.status,
                "test_functions": analysis.test_functions,
                "warnings": list(analysis.warnings),
                "error": analysis.error,
            })

        if request.collect and any(item["location"] == "tests" for item in files):
            collection = collect_tests(tmp_dir / "tests", request.collect_timeout_seconds)
        else:
            collection = skipped_collection()

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": created_at.isoformat(timespec="seconds"),
            "contract": {
                "path": str(contract_path),
                "sha256": hashlib.sha256(contract_bytes).hexdigest(),
                "title": title,
                "slug": slug,
            },
            "generator": {"model": request.model, "meta": request.generator_meta},
            "files": files,
            "collection": collection,
            "summary": _summary(files, collection),
        }
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        run_dir = contract_dir / run_id
        # Fails instead of replacing if run_dir appeared meanwhile.
        os.rename(tmp_dir, run_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return SavedSuite(
        run_id=run_id,
        run_dir=run_dir,
        tests_dir=run_dir / "tests",
        manifest_path=run_dir / "manifest.json",
        manifest=manifest,
    )
```

Заменить `prototype/src/prototype/storage/__init__.py`:

```python
"""Storage of generated pytest suites as immutable, versioned directories (ТЗ 2.1.6)."""

from .contracts import GeneratedFile, SaveRequest, SavedSuite
from .store import save_test_suite

__all__ = ["GeneratedFile", "SaveRequest", "SavedSuite", "save_test_suite"]
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q`
Expected: все PASS, включая `test_storage_import_works_without_llm_or_third_party_packages` (проверяет, что `yaml` импортируется лениво).

- [ ] **Step 5: Коммит**

```bash
git add src/prototype/storage src/prototype/tests/test_storage.py
git commit -m "feat(storage): атомарное сохранение версии тестов с manifest.json"
```

---

### Task 5: Tool агента `save_tests_tool`

**Files:**
- Modify: `prototype/src/prototype/runner/tools.py` (импорт вверху файла; новая функция в конце)
- Test: `prototype/src/prototype/tests/test_storage.py` (дописать)

**Interfaces:**
- Consumes: `GeneratedFile`, `SaveRequest`, `save_test_suite` из `prototype.storage` (Task 4).
- Produces: `save_tests_tool` — LangChain tool с аргументами `contract_path: str`, `files: list[dict[str, str]]` (ключи `name`, `code`), `model: str | None = None`; возвращает `dict` с ключами `tool`, `status`, и при успехе `run_id`, `tests_dir`, `summary`, `collection_status`, `errors` (не более 5), при ошибке — `message`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `test_storage.py`:

```python
def test_save_tests_tool_returns_compact_result(tmp_path, monkeypatch):
    pytest.importorskip("langchain")
    from prototype.runner.tools import save_tests_tool

    monkeypatch.setenv("TESTGEN_OUTPUT_DIR", str(tmp_path))
    result = save_tests_tool.invoke({
        "contract_path": str(CONTRACT),
        "files": [
            {"name": "test_good.py", "code": GOOD},
            {"name": "test_broken.py", "code": "def test_a(:\n"},
        ],
        "model": "deepseek",
    })

    assert result["tool"] == "save_tests_tool"
    assert result["status"] == "success"
    assert Path(result["tests_dir"]).is_dir()
    assert result["collection_status"] == "ok"
    assert result["summary"]["collected"] == 2
    # SyntaxError text differs between Python versions; only the prefix is stable.
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("test_broken.py: line 1:")
    assert GOOD not in json.dumps(result)


@pytest.mark.parametrize("files, contract", [
    ([{"name": "../test_evil.py", "code": ""}], CONTRACT),
    ([{"code": "def test_a(): pass"}], CONTRACT),
    ([], CONTRACT),
    ([{"name": "test_a.py", "code": GOOD}], Path("missing.yaml")),
])
def test_save_tests_tool_reports_errors_instead_of_raising(tmp_path, monkeypatch, files, contract):
    pytest.importorskip("langchain")
    from prototype.runner.tools import save_tests_tool

    monkeypatch.setenv("TESTGEN_OUTPUT_DIR", str(tmp_path))
    result = save_tests_tool.invoke({"contract_path": str(contract), "files": files})
    assert result["status"] == "error"
    assert result["message"]
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run --locked python -m pytest src/prototype/tests/test_storage.py -q -k tool`
Expected: FAIL — `ImportError: cannot import name 'save_tests_tool'`.

- [ ] **Step 3: Реализовать tool**

В `prototype/src/prototype/runner/tools.py` после строки `from ..parser.contract import read_contract_summary` добавить:

```python
from ..storage import GeneratedFile, SaveRequest, save_test_suite
```

В конец файла добавить:

```python
@tool
def save_tests_tool(
    contract_path: str,
    files: list[dict[str, str]],
    model: str | None = None,
) -> dict[str, Any]:
    """
    Сохранить сгенерированные pytest-тесты как новую неизменяемую версию.

    Каждый файл проверяется ast.parse: корректные попадают в tests/,
    файлы с синтаксической ошибкой — в rejected/. Затем набор проверяется
    pytest --collect-only (тела тестов не выполняются). Версия сохраняется
    в generated/<контракт>/<дата_время>/ вместе с manifest.json.

    Args:
        contract_path: Путь к OpenAPI-контракту, по которому созданы тесты.
        files: Список файлов, каждый — {'name': 'test_*.py', 'code': '...'}.
        model: Имя модели, сгенерировавшей тесты (опционально).

    Returns:
        Компактная сводка: run_id, tests_dir (путь для запуска),
        summary, collection_status и до 5 ошибок синтаксиса/сбора.
        Код тестов обратно не возвращается.
    """

    print()
    print("=" * 60)
    print("[TOOL] Вызван save_tests_tool")
    print(f"[TOOL] Контракт: {contract_path}")
    print(f"[TOOL] Файлов: {len(files)}")
    print("=" * 60)
    print()

    try:
        request = SaveRequest(
            contract_path=contract_path,
            files=tuple(
                GeneratedFile(item.get("name", ""), item.get("code", ""))
                for item in files
            ),
            model=model,
        )
        saved = save_test_suite(request)
    except (ValueError, OSError) as exc:
        return {
            "tool": "save_tests_tool",
            "status": "error",
            "message": f"Не удалось сохранить тесты: {exc}",
        }

    manifest = saved.manifest
    problems = [
        f"{item['name']}: {item['error']}"
        for item in manifest["files"]
        if item["error"]
    ] + manifest["collection"]["errors"]

    return {
        "tool": "save_tests_tool",
        "status": "success",
        "run_id": saved.run_id,
        "tests_dir": str(saved.tests_dir),
        "summary": manifest["summary"],
        "collection_status": manifest["collection"]["status"],
        "errors": problems[:5],
    }
```

- [ ] **Step 4: Убедиться, что проходят все тесты проекта**

Run: `uv run --locked python -m pytest -q`
Expected: все PASS, включая существующий `test_existing_agent_keeps_its_model_prompt_and_four_tools` (tool не подключён к агенту).

- [ ] **Step 5: Коммит**

```bash
git add src/prototype/runner/tools.py src/prototype/tests/test_storage.py
git commit -m "feat(runner): tool save_tests_tool для сохранения сгенерированных тестов"
```

---

### Task 6: Документация (ADR 0003, ReadMe)

**Files:**
- Create: `docs/adr/0003-generated-tests-storage.md`
- Modify: `prototype/ReadMe.md` (новый раздел после «Интерфейс pytest-runner — этап 1»)
- Modify: `README.md` (ссылка на ADR 0003)

**Interfaces:**
- Consumes: всё из Task 1–5.
- Produces: документация; кода нет.

- [ ] **Step 1: Написать ADR 0003**

Создать `docs/adr/0003-generated-tests-storage.md`:

````markdown
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
относится к runner (ТЗ 2.2.6). Сбор запускается с таймаутом, без
переменных `DEEPCODE_*`, `OPENAI_*`, `PYTEST_ADDOPTS` и без настроек pytest
проекта. Код верхнего уровня, кроме импортов, определений и присваиваний,
помечается предупреждением `top_level_code`. Когда появится изолированный
runner, сбор следует перенести в него (`collect=False`).

## Ответственность

- Генератор/постобработка передают готовый код в `save_test_suite` или
  агент вызывает `save_tests_tool`.
- Модуль хранения не вызывает LLM, не запускает тела тестов, не считает метрики.
- Runner получает `SavedSuite.tests_dir` как `RunConfig.tests_dir`.
````

- [ ] **Step 2: Добавить раздел в `prototype/ReadMe.md`**

В конец файла `prototype/ReadMe.md` добавить:

````markdown

### Сохранение сгенерированных тестов (п. 2.1.6 ТЗ)

Модуль `prototype.storage` сохраняет pytest-код генератора как
неизменяемую версию с `manifest.json`:

```python
from prototype.storage import GeneratedFile, SaveRequest, save_test_suite

saved = save_test_suite(SaveRequest(
    contract_path="src/prototype/tests/fixtures/demo_openapi.yaml",
    files=(GeneratedFile("test_catalogue.py", code),),
    model="deepseek-ai/DeepSeek-V4-Flash",
))
print(saved.tests_dir)   # generated/demo-catalogue-api/2026-09-28_153012/tests
```

Файлы с синтаксической ошибкой попадают в `rejected/`, остальные
проверяются `pytest --collect-only`. Каталог по умолчанию —
`prototype/generated`, переопределяется `TESTGEN_OUTPUT_DIR`. Для агента
есть tool `save_tests_tool`; к `build_agent` он будет подключён вместе
с генератором тестов. Сбор выполняет код верхнего уровня тестовых модулей
и не является изоляцией. Подробности — в
[ADR 0003](../docs/adr/0003-generated-tests-storage.md).
````

- [ ] **Step 3: Добавить ссылку в корневой `README.md`**

В конец списка ссылок `README.md` добавить строку:

```markdown
- [Сохранение сгенерированных тестов](docs/adr/0003-generated-tests-storage.md).
```

- [ ] **Step 4: Финальная проверка**

Run: `uv run --locked python -m pytest -q`
Expected: все PASS (38 прежних + новые тесты `test_storage.py`).

Run: `uv run --locked tester --help`
Expected: справка CLI без ошибок.

- [ ] **Step 5: Коммит**

```bash
git add ../docs/adr/0003-generated-tests-storage.md ReadMe.md ../README.md
git commit -m "docs: ADR 0003 о сохранении сгенерированных тестов"
```
