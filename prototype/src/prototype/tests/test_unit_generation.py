"""The opt-in unit path snapshots source and runs only in the offline runner."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from prototype.generator.unit import run_unit_generation, validate_unit_source
from prototype.service_tools.runner.contracts import RunResult, TestResult as RunnerTestResult


def _model(code):
    class Model:
        model_name = "fake-unit"
        def invoke(self, messages):
            self.messages = messages
            return SimpleNamespace(content=json.dumps({"files": [{"name": "test_math.py", "code": code}]}),
                                   usage_metadata={"input_tokens": 5, "output_tokens": 8})
    return Model()


def test_unit_generation_snapshots_source_and_uses_offline_runner(tmp_path):
    source = tmp_path / "math.py"
    source.write_text("def double(value):\n    return value * 2\n", encoding="utf-8")
    model = _model("from unit_source import double\n\ndef test_double():\n    assert double(3) == 6\n")
    calls = []
    def runner(config):
        calls.append(config)
        assert config.base_url is None
        assert (config.tests_dir / "unit_source.py").read_text() == source.read_text()
        return RunResult("completed", 0, .1, tests=(RunnerTestResult("test_math.py::test_double", "passed", .1),))
    result = run_unit_generation(source, output_root=tmp_path / "reports", model=model, run_tests_fn=runner)
    assert result["status"] == "passed" and len(calls) == 1
    assert result["tokens"] == (5, 8, 13)
    assert Path(result["summary_path"]).exists()
    assert "def double" in model.messages[1]["content"]


def test_unit_failure_cannot_be_reported_as_passed(tmp_path):
    source = tmp_path / "math.py"
    source.write_text("def double(value):\n    return value * 2\n")
    model = _model("from unit_source import double\n\ndef test_double():\n    assert double(3) == 9\n")
    def runner(_):
        return RunResult("completed", 1, .1, tests=(RunnerTestResult("test_math.py::test_double", "failed", .1,
                         message="assert 6 == 9"),))
    result = run_unit_generation(source, output_root=tmp_path, model=model, run_tests_fn=runner)
    assert result["status"] == "failed" and result["runner"]["summary"]["failed"] == 1
    assert "test_math.py::test_double" in result["error"] and "assert 6 == 9" in result["error"]


def test_invalid_unit_source_stops_before_model_or_runner(tmp_path):
    with pytest.raises(ValueError, match="existing.*Python"):
        validate_unit_source(tmp_path / "missing.py")
    source = tmp_path / "empty.py"
    source.write_text("x = 1\n")
    with pytest.raises(ValueError, match="no public"):
        validate_unit_source(source)


def test_generated_file_must_import_source(tmp_path):
    source = tmp_path / "math.py"
    source.write_text("def double(value):\n    return value * 2\n")
    model = _model("def test_double():\n    assert 3 * 2 == 6\n")
    result = run_unit_generation(source, output_root=tmp_path, model=model,
                                 run_tests_fn=lambda _: pytest.fail("invalid tests reached runner"))
    assert result["status"] == "generation_error"
    assert "must call a unit_source function" in result["error"]
