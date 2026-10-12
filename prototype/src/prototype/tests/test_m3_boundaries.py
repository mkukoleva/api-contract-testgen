"""M3 component boundaries: real storage/reports, injected process/provider failures."""

import json
from pathlib import Path

import pytest

from prototype.generator import pipeline
from prototype.generator.contracts import GenerationSettings
from prototype.tests.test_generator_pipeline import FakeModel, GOOD_FILE, ScriptedRunner


def settings(tmp_path):
    contract = Path(__file__).resolve().parents[4] / "benchmark/catalogue/catalogue.swagger.json"
    return GenerationSettings(contract_path=contract, output_dir=tmp_path / "reports",
                              output_root=tmp_path / "saved")


def model():
    result = FakeModel()
    result.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE}]
    return result


def assert_report(run):
    data = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
    assert data["status"] == run.status
    assert data["generator_errors"] == list(run.generator_errors)
    return data


def fail(*args, **kwargs):
    raise OSError("simulated disk failure")


def test_bad_contract_is_reported_before_model_creation(tmp_path, monkeypatch):
    from dataclasses import replace
    import prototype.llm.model as llm
    bad = tmp_path / "bad.yaml"
    bad.write_text("paths: [", encoding="utf-8")
    monkeypatch.setattr(llm, "build_model", lambda: pytest.fail("No model before validated input"))
    run = pipeline.run_generation_pipeline(replace(settings(tmp_path), contract_path=bad))
    assert run.status == "generation_error"
    assert run.attempts == 0
    assert_report(run)


def test_model_initialization_failure_is_reported_without_runner(tmp_path, monkeypatch):
    import prototype.llm.model as llm
    def unavailable():
        raise RuntimeError("provider configuration unavailable")
    monkeypatch.setattr(llm, "build_model", unavailable)
    run = pipeline.run_generation_pipeline(settings(tmp_path), run_tests_fn=lambda _: pytest.fail("No runner"))
    assert run.status == "generation_error"
    assert run.attempts == 0
    assert_report(run)


def test_storage_failure_does_not_look_like_a_success(tmp_path):
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=model(),
                                          run_tests_fn=ScriptedRunner(), save_fn=fail)
    assert run.status == "infrastructure_error"
    assert run.run_result["summary"]["passed"] == 1
    assert not run.saved_versions
    assert_report(run)


def test_attempt_directory_failure_is_reported_without_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "write_attempt_dir", fail)
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=model(),
        run_tests_fn=lambda _: pytest.fail("No runner after a write failure"))
    assert run.status == "infrastructure_error"
    assert len(run.saved_versions) == 1
    assert_report(run)


def test_report_failure_preserves_run_facts_and_is_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "save_generation_report", fail)
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=model(), run_tests_fn=ScriptedRunner())
    assert run.status == "infrastructure_error"
    assert run.run_result["summary"]["passed"] == 1
    assert len(run.saved_versions) == 1
    assert not run.report_paths
    assert any("saving the generation report failed" in message for message in run.generator_errors)


def test_bound_tool_returns_structured_component_failure(tmp_path, monkeypatch):
    from prototype.service_tools.runner.tools import create_generation_tool
    monkeypatch.setattr(pipeline, "run_generation_pipeline", fail)
    data = create_generation_tool(settings(tmp_path)).invoke({})
    assert data["status"] == "error"
    assert "simulated disk failure" in data["message"]


def test_generation_interrupt_is_reported_and_never_reaches_runner(tmp_path):
    class InterruptedModel:
        def invoke(self, messages):
            raise KeyboardInterrupt
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=InterruptedModel(),
                                          run_tests_fn=lambda _: pytest.fail("No runner"))
    assert run.status == "interrupted" and run.attempts == 0
    assert_report(run)


def test_unwritable_run_root_never_creates_model_or_runner(tmp_path, monkeypatch):
    from dataclasses import replace
    import prototype.llm.model as llm
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(llm, "build_model", lambda: pytest.fail("No provider before output directory"))
    run = pipeline.run_generation_pipeline(replace(settings(tmp_path), output_dir=root_file))
    assert run.status == "infrastructure_error" and not run.report_paths
    assert root_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("status,exit_code", [
    ("timeout", None), ("interrupted", 2), ("no_tests", 5), ("infrastructure_error", None)])
def test_runner_lifecycle_status_survives_pipeline_reports(tmp_path, status, exit_code):
    from prototype.service_tools.runner.contracts import RunResult
    scripted = model()
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=scripted,
        run_tests_fn=lambda _: RunResult(status, exit_code, .1))
    assert run.status == status and run.attempts == 0
    assert len(scripted.calls) == 1
    payload = assert_report(run)
    assert payload["pytest"]["status"] == status
    # Existing evaluate semantics: a known empty collection is 0%, whereas
    # interrupted/unavailable execution has no trustworthy percentage.
    assert payload["runnability"]["runnability_percent"] == (0.0 if status == "no_tests" else None)


def test_provider_failure_diagnostics_do_not_leak_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCODE_API_KEY", "dummy-sensitive-value")
    class FailedModel:
        def invoke(self, messages):
            raise RuntimeError("provider rejected dummy-sensitive-value")
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=FailedModel())
    assert run.status == "generation_error"
    assert "dummy-sensitive-value" not in json.dumps(assert_report(run))


def test_duplicate_generated_names_are_rejected_before_storage_or_runner(tmp_path):
    scripted = model()
    scripted.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE},
        {"name": "test_catalogue.py", "code": GOOD_FILE.replace("test_good", "test_overwritten")}]
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=scripted,
        run_tests_fn=lambda _: pytest.fail("No runner for conflicting filenames"))
    assert run.status == "generation_error" and not run.saved_versions
    assert any("duplicate" in error for error in assert_report(run)["generator_errors"])


def test_rejected_generation_artifact_does_not_produce_a_green_overall_status(tmp_path):
    scripted = model()
    scripted.generation_files.append({"name": "../escape.py", "code": "def test_x(): pass"})
    run = pipeline.run_generation_pipeline(settings(tmp_path), model=scripted, run_tests_fn=ScriptedRunner())
    assert run.status == "generation_error"
    assert run.run_result["summary"]["passed"] == 1
    assert_report(run)
