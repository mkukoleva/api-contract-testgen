"""Deterministic post-processing of generated test files; no LLM or network."""

from pathlib import Path

import pytest

from prototype.postprocess.fixes import (
    PreparedFile,
    as_generated_files,
    prepare_file,
    prepare_files,
    write_attempt_dir,
)
from prototype.storage import GeneratedFile

GOOD = (
    "def test_list():\n"
    "    assert 1 + 1 == 2\n\n\n"
    "class TestTags:\n"
    "    def test_tags(self):\n"
    "        assert True\n"
)


def test_prepare_file_normalizes_bom_and_crlf():
    prepared = prepare_file("test_a.py", "\ufeffdef test_a():\r\n    pass\r\n")
    assert prepared.code == "def test_a():\n    pass\n"
    assert prepared.status == "ok"
    assert prepared.test_functions == 1
    assert prepared.error is None


def test_prepare_file_reports_syntax_error_without_raising():
    prepared = prepare_file("test_a.py", "def test_a(:\n")
    assert prepared.status == "syntax_error"
    assert prepared.test_functions is None
    assert prepared.error.startswith("line 1:")


def test_prepare_file_marks_empty_module():
    prepared = prepare_file("test_a.py", "X = 1\n")
    assert prepared.status == "no_tests"
    assert prepared.test_functions == 0


def test_prepare_files_keeps_order_and_rejects_invalid_names():
    files = (
        GeneratedFile("test_good.py", GOOD),
        GeneratedFile("test_broken.py", "def test_a(:\n"),
    )
    prepared = prepare_files(files)
    assert [item.name for item in prepared] == ["test_good.py", "test_broken.py"]
    assert prepared[0].status == "ok"
    assert prepared[1].status == "syntax_error"

    with pytest.raises(ValueError):
        prepare_files((GeneratedFile("../evil.py", ""),))


def test_as_generated_files_round_trips_through_storage_boundary():
    prepared = prepare_file("test_a.py", GOOD)
    generated = as_generated_files((prepared,))[0]
    assert isinstance(generated, GeneratedFile)
    assert generated.name == "test_a.py"
    assert generated.code == prepared.code


def test_write_attempt_dir_writes_every_file_including_syntax_errors(tmp_path):
    files = (
        prepare_file("test_good.py", GOOD),
        prepare_file("test_broken.py", "def test_a(:\n"),
    )
    tests_dir = write_attempt_dir(files, tmp_path / "attempt" / "tests")
    assert sorted(path.name for path in tests_dir.iterdir()) == [
        "test_broken.py",
        "test_good.py",
    ]
    assert (tests_dir / "test_broken.py").read_text(encoding="utf-8") == "def test_a(:\n"
