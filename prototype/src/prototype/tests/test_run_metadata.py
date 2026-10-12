"""Run provenance survives storage, reports and provider failures, without LLM calls."""

import hashlib
import json
from pathlib import Path

from prototype.generator.pipeline import run_generation_pipeline
from prototype.tests.test_generator_pipeline import FakeModel, FakeSaver, ScriptedRunner, contract, settings


def test_actual_model_and_contract_metadata_reach_report_and_every_saved_version(tmp_path):
    source = contract(tmp_path)
    model = FakeModel()
    model.model_name = "Qwen3.8-Flash-Next-test"
    model.temperature = 0
    model.request_timeout = 25
    model.max_retries = 0
    saver = FakeSaver()
    run = run_generation_pipeline(settings(tmp_path, source, model="obsolete-label"), model=model,
                                  run_tests_fn=ScriptedRunner(), save_fn=saver)
    assert run.status == "success"
    assert run.model == model.model_name
    provenance = run.provenance
    assert provenance["contract"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert provenance["contract"]["version"] == "1.0.0"
    assert provenance["contract"]["operation_count"] > 0
    assert provenance["llm"] == {"model": model.model_name, "temperature": 0,
                                 "timeout_seconds": 25, "max_retries": 0}
    assert len(provenance["prompts"]["generation_sha256"]) == 64
    assert len(provenance["prompts"]["repair_sha256"]) == 64
    assert provenance["runtime"]["python"]
    assert provenance["runtime"]["dependencies"]["pytest"]
    for request in saver.requests:
        assert request.model == model.model_name
        assert request.generator_meta["provenance"] == provenance
        assert request.collect is False
    payload = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["provenance"] == provenance == run.to_dict()["provenance"]
    markdown = Path(run.report_paths["markdown"]).read_text(encoding="utf-8")
    assert model.model_name in markdown and provenance["contract"]["sha256"] in markdown


def test_metadata_survives_provider_failure_and_does_not_include_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPCODE_API_KEY", "dummy-secret-metadata-check")
    model = FakeModel()
    model.model_name = "configured-model"
    model.api_key = "dummy-secret-metadata-check"
    def fail(messages):
        raise RuntimeError("provider unavailable")
    model.invoke = fail
    run = run_generation_pipeline(settings(tmp_path, contract(tmp_path)), model=model)
    assert run.status == "generation_error"
    payload = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
    assert payload["provenance"]["llm"]["model"] == "configured-model"
    assert "dummy-secret-metadata-check" not in json.dumps(payload)


def test_contract_metadata_is_preserved_when_model_cannot_be_built(tmp_path, monkeypatch):
    from prototype.llm import model
    def fail():
        raise RuntimeError("No credentials")
    monkeypatch.setattr(model, "build_model", fail)
    run = run_generation_pipeline(settings(tmp_path, contract(tmp_path)))
    assert run.status == "generation_error"
    assert run.provenance["contract"]["operation_count"] > 0
    assert run.provenance["llm"]["model"] is None


def test_main_report_records_contract_provenance_without_copying_test_code(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import prototype
    source = contract(tmp_path)
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(invoke=lambda _: {"messages": []}))
    prototype.run(str(source), report_dir=tmp_path / "reports")
    path = next((tmp_path / "reports").rglob("run-metadata.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["contract"]["version"] == "1.0.0"
    assert data["contract"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert data["prompts"]["agent_sha256"]
