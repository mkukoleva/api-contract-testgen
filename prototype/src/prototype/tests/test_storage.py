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
