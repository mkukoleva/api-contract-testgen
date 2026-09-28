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
