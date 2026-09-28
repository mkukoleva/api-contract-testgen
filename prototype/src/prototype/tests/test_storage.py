"""Generated-tests storage checks; no Docker, LLM credentials or network required."""

from datetime import datetime, timedelta, timezone
import hashlib
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
        "assert 'prototype.service_tools.runner.tools' not in sys.modules; "
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


COLLECT_OUTPUT = """\
test_good.py::test_a
test_good.py::TestX::test_b
test_good.py::test_p[1]

=================================== ERRORS ====================================
________________________ ERROR collecting test_bad.py _________________________
ImportError while importing test module 'C:\\tmp\\test_bad.py'.
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


def test_collect_tests_does_not_autoload_third_party_pytest_plugins(tmp_path):
    from prototype.storage.collect import collect_tests

    # langsmith (a LangChain dependency) registers a pytest11 plugin; generated
    # code must not share a process with it, and loading it slows collection.
    tests_dir = write_tests(
        tmp_path / "tests",
        test_a="import sys\nassert 'langsmith' not in sys.modules\n\ndef test_a():\n    pass\n",
    )
    assert collect_tests(tests_dir, 60)["status"] == "ok"


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
    saved = save(tmp_path, ("test_a.py", "﻿def test_a():\r\n    pass\r\n"))

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


def test_save_tests_tool_returns_compact_result(tmp_path, monkeypatch):
    pytest.importorskip("langchain")
    from prototype.service_tools.runner.tools import save_tests_tool

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
    from prototype.service_tools.runner.tools import save_tests_tool

    monkeypatch.setenv("TESTGEN_OUTPUT_DIR", str(tmp_path))
    result = save_tests_tool.invoke({"contract_path": str(contract), "files": files})
    assert result["status"] == "error"
    assert result["message"]
    assert list(tmp_path.iterdir()) == []


def test_project_pytest_does_not_collect_saved_versions(tmp_path):
    # Two versions of one contract share module names; if the project's own
    # pytest walked into generated/, it would run LLM code with the developer's
    # environment and fail with "import file mismatch".
    (tmp_path / "pyproject.toml").write_bytes((SOURCE_DIR.parent / "pyproject.toml").read_bytes())
    write_tests(tmp_path / "src" / "prototype" / "tests", test_own="def test_own():\n    pass\n")
    for run_id in ("2026-09-28_120000", "2026-09-28_120000_2"):
        write_tests(
            tmp_path / "generated" / "demo" / run_id / "tests",
            test_a="raise RuntimeError('generated code must not run')\n",
        )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                         "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "src/prototype/tests/test_own.py::test_own" in result.stdout
    assert "generated" not in result.stdout
