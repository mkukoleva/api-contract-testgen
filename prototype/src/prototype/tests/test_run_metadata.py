"""Run provenance survives storage, reports and provider failures, without LLM calls."""

import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import copy
from types import SimpleNamespace

import pytest

from prototype.generator.pipeline import run_generation_pipeline
from prototype.tests.test_generator_pipeline import FakeModel, FakeSaver, ScriptedRunner, contract, settings


@pytest.mark.parametrize("bump_api_version", [False, True])
def test_changed_contract_generates_a_fresh_complete_suite_and_preserves_old_inputs(tmp_path, bump_api_version):
    from prototype.service_tools.runner.contracts import RunResult, TestResult as RunnerTestResult
    from prototype.storage.analysis import analyze_test_file
    spec = {"openapi": "3.0.3", "info": {"title": "Regeneration Demo", "version": "1.0"},
        "paths": {"/items": {"get": {"responses": {"200": {"description": "Item",
            "content": {"application/json": {"schema": {"type": "object", "required": ["id"],
                "properties": {"id": {"type": "string"}}}}}}}}},
            "/obsolete": {"get": {"responses": {"200": {"description": "OK"}}}}}}
    source = tmp_path / "api.json"
    source.write_text(json.dumps(spec), encoding="utf-8")
    old_bytes = source.read_bytes()
    requests, attempted_files = [], []
    class ContractAwareControlModel:
        model_name = "scripted-regeneration-model"
        def invoke(self, messages):
            requests.append(messages)
            prompt = messages[1]["content"]
            changed = "name*:string" in prompt
            extra_path = "/tags" if changed else "/obsolete"
            assert f"GET {extra_path}" in prompt
            item_code = ("def test_normal_items(base_url, api_client):\n"
                         "    response = api_client.get(base_url + '/items')\n"
                         "    assert response.status_code == 200\n"
                         "    body = response.json()\n"
                         "    assert isinstance(body, dict)\n"
                         "    assert 'id' in body\n")
            if changed:
                item_code += "    assert 'name' in body\n    assert isinstance(body['name'], str)\n"
            files = [{"name": "test_items.py", "code": item_code},
                     {"name": "test_tags.py" if changed else "test_obsolete.py", "code":
                      "def test_normal_extra(base_url, api_client):\n"
                      f"    assert api_client.get(base_url + '{extra_path}').status_code == 200\n"}]
            return SimpleNamespace(content=json.dumps({"files": files}), usage_metadata={
                "input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    def runner(config):
        # Execution is scripted: this test verifies fresh inputs and storage,
        # not service behavior or whether an actual LLM follows instructions.
        paths = sorted(config.tests_dir.glob("test_*.py"))
        attempted_files.append({path.name for path in paths})
        tests = tuple(RunnerTestResult(nodeid, "passed") for path in paths
            for nodeid in analyze_test_file(path.read_text(encoding="utf-8"), path.name).test_cases)
        return RunResult("completed", 0, 0.1, tests=tests)
    model = ContractAwareControlModel()
    config = settings(tmp_path / "reports", source, output_root=tmp_path / "saved")
    first = run_generation_pipeline(config, model=model, run_tests_fn=runner)
    assert first.status == "success", first.to_dict()
    first_dir = Path(first.saved_versions[0]["tests_dir"]).parent
    old_files = {path.relative_to(first_dir): path.read_bytes() for path in first_dir.rglob("*") if path.is_file()}
    first_snapshot = Path(first.provenance["contract"]["snapshot_path"])
    first_report = Path(first.report_paths["json"]).read_bytes()

    changed = copy.deepcopy(spec)
    changed["info"]["version"] = "2.0" if bump_api_version else "1.0"
    del changed["paths"]["/obsolete"]
    changed["paths"]["/tags"] = {"get": {"responses": {"200": {"description": "Tags"}}}}
    schema = changed["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    schema["required"].append("name")
    schema["properties"]["name"] = {"type": "string"}
    source.write_text(json.dumps(changed), encoding="utf-8")
    second = run_generation_pipeline(config, model=model, run_tests_fn=runner)
    assert second.status == "success", second.to_dict()
    assert len(requests) == 2
    assert "GET /obsolete" not in requests[1][1]["content"]
    assert "name*:string" not in requests[0][1]["content"]
    assert "полный актуальный набор заново" in requests[1][1]["content"]
    assert attempted_files == [{"test_items.py", "test_obsolete.py"}, {"test_items.py", "test_tags.py"}]
    new_dir = Path(second.saved_versions[0]["tests_dir"])
    assert "assert 'name' in body" in (new_dir / "test_items.py").read_text()
    assert not (new_dir / "test_obsolete.py").exists()
    assert first_dir != new_dir.parent and first.report_paths != second.report_paths
    assert first_snapshot.read_bytes() == old_bytes
    assert Path(first.report_paths["json"]).read_bytes() == first_report
    assert {path.relative_to(first_dir): path.read_bytes() for path in first_dir.rglob("*") if path.is_file()} == old_files
    assert first.provenance["contract"]["sha256"] != second.provenance["contract"]["sha256"]
    assert second.provenance["contract"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert second.provenance["contract"]["version"] == ("2.0" if bump_api_version else "1.0")


def test_real_storage_keeps_repeated_generation_versions_with_required_identity(tmp_path, monkeypatch):
    from prototype.storage import store
    from prototype.tests.test_generator_pipeline import GOOD_FILE
    fixed = datetime(2026, 10, 14, 13, 0, tzinfo=timezone(timedelta(hours=7)))
    monkeypatch.setattr(store, "_now", lambda: fixed)
    source = contract(tmp_path)
    model = FakeModel()
    model.model_name = "scripted-model-v1"
    model.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE}]
    config = settings(tmp_path / "reports", source, output_root=tmp_path / "saved")
    first = run_generation_pipeline(config, model=model, run_tests_fn=ScriptedRunner())
    assert first.status == "success"
    first_dir = Path(first.saved_versions[0]["tests_dir"]).parent
    before = {path.relative_to(first_dir): path.read_bytes()
              for path in first_dir.rglob("*") if path.is_file()}

    model.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE + "# second generation\n"}]
    second = run_generation_pipeline(config, model=model, run_tests_fn=ScriptedRunner())
    assert second.status == "success"
    second_dir = Path(second.saved_versions[0]["tests_dir"]).parent
    assert first_dir != second_dir
    assert first_dir.name == "2026-10-14_130000"
    assert second_dir.name == "2026-10-14_130000_2"
    assert {path.relative_to(first_dir): path.read_bytes()
            for path in first_dir.rglob("*") if path.is_file()} == before
    assert first.report_paths != second.report_paths

    for run, directory in ((first, first_dir), (second, second_dir)):
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        metadata = manifest["generator"]["meta"]["provenance"]
        assert manifest["generator"]["model"] == "scripted-model-v1"
        assert metadata["contract"]["version"] == "1.0.0"
        assert metadata["contract"]["specification"] == "OpenAPI 3.0.3"
        assert manifest["created_at"] == fixed.isoformat(timespec="seconds")
        assert manifest["contract"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        file = manifest["files"][0]
        assert file["sha256"] == hashlib.sha256((directory / file["location"] / file["name"]).read_bytes()).hexdigest()
        assert manifest["collection"]["status"] == "skipped"
        report = json.loads(Path(run.report_paths["json"]).read_text(encoding="utf-8"))
        report_provenance = dict(report["provenance"])
        trace = report_provenance.pop("result_trace")
        assert report_provenance == metadata == run.provenance
        assert "trace_error" not in trace
        assert trace["summary"]["operations_total"] == 2
        assert trace["summary"]["operations_covered"] == 1


def test_parallel_real_saves_in_one_second_keep_every_distinct_version(tmp_path, monkeypatch):
    from prototype.storage import GeneratedFile, SaveRequest, save_test_suite, store
    fixed = datetime(2026, 10, 14, 13, 0, tzinfo=timezone(timedelta(hours=7)))
    monkeypatch.setattr(store, "_now", lambda: fixed)
    source = contract(tmp_path)
    def save(index):
        code = f"def test_case():\n    assert {index} == {index}\n"
        suite = save_test_suite(SaveRequest(source, (GeneratedFile("test_case.py", code),),
            model="scripted-model", generator_meta={"generation": index}, output_root=tmp_path / "saved"))
        return suite, code
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(save, range(8)))
    assert len({suite.run_id for suite, _ in versions}) == 8
    for suite, code in versions:
        assert (suite.tests_dir / "test_case.py").read_text(encoding="utf-8") == code
        manifest = json.loads(suite.manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == suite.run_id
        assert manifest["generator"]["meta"] == suite.manifest["generator"]["meta"]
        assert manifest["created_at"] == fixed.isoformat(timespec="seconds")
    assert not list((tmp_path / "saved").rglob("*.tmp"))


def test_version_published_between_exists_and_reservation_is_not_reused(tmp_path, monkeypatch):
    from prototype.storage import GeneratedFile, SaveRequest, save_test_suite, store
    fixed = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_now", lambda: fixed)
    source = contract(tmp_path)
    final = tmp_path / "saved" / "demo-catalogue-api" / "2026-10-14_130000"
    temporary = final.with_name("." + final.name + ".tmp")
    mkdir = Path.mkdir

    def publish_just_before_reserving(path, *args, **kwargs):
        if path == temporary and not final.exists():
            mkdir(final)
            (final / "manifest.json").write_text("previous immutable version")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", publish_just_before_reserving)
    suite = save_test_suite(SaveRequest(source,
        (GeneratedFile("test_case.py", "def test_case(): assert True\n"),), output_root=tmp_path / "saved"))
    assert suite.run_id == "2026-10-14_130000_2"
    assert (final / "manifest.json").read_text() == "previous immutable version"
    assert not temporary.exists()


def test_failed_new_version_does_not_change_previously_saved_suite(tmp_path, monkeypatch):
    from prototype.storage import GeneratedFile, SaveRequest, save_test_suite, store
    source = contract(tmp_path)
    request = SaveRequest(source, (GeneratedFile("test_case.py", "def test_case(): assert True\n"),),
                          output_root=tmp_path / "saved")
    first = save_test_suite(request)
    before = {path.relative_to(first.run_dir): path.read_bytes()
              for path in first.run_dir.rglob("*") if path.is_file()}
    original = Path.write_text
    def fail_manifest(path, *args, **kwargs):
        if path.name == "manifest.json":
            raise OSError("controlled manifest write failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_manifest)
    import pytest
    with pytest.raises(OSError, match="manifest write failure"):
        save_test_suite(request)
    assert {path.relative_to(first.run_dir): path.read_bytes()
            for path in first.run_dir.rglob("*") if path.is_file()} == before
    assert len(list(first.run_dir.parent.iterdir())) == 1


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
    report_provenance = dict(payload["provenance"])
    trace = report_provenance.pop("result_trace")
    assert "trace_error" not in trace
    assert report_provenance == provenance == run.to_dict()["provenance"]
    markdown = Path(run.report_paths["markdown"]).read_text(encoding="utf-8")
    assert model.model_name in markdown and provenance["contract"]["sha256"] in markdown


def test_report_combines_trace_categories_inputs_and_story_without_mutating_run(tmp_path):
    from dataclasses import replace
    from prototype.reports.generation_report import save_generation_report
    from prototype.tests.test_generator_pipeline import GOOD_FILE

    source = contract(tmp_path)
    model = FakeModel()
    model.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE}]
    run = run_generation_pipeline(settings(tmp_path / "generation", source,
        output_root=tmp_path / "saved"), model=model, run_tests_fn=ScriptedRunner())
    assert run.status == "success"
    provenance = {**run.provenance,
        "input_coverage": {"scope": "observed request bodies", "status": "observed",
            "operations": [{"operation": "GET /catalogue", "status": "observed", "missing": []}],
            "limitations": []},
        "user_story": {"story": "Browse catalogue", "steps": [
            {"endpoint": "GET /catalogue", "expected_result": "List returned"}],
            "final_goal": "Selected product is present"}}
    run = replace(run, provenance=provenance)
    original = copy.deepcopy(run.to_dict())
    paths = save_generation_report(run, tmp_path / "combined-report")
    payload = json.loads(Path(paths["json"]).read_text())
    markdown = Path(paths["markdown"]).read_text()

    assert run.to_dict() == original
    assert payload["category_evidence"] == run.category_evidence
    report_provenance = dict(payload["provenance"])
    trace = report_provenance.pop("result_trace")
    assert report_provenance == provenance
    assert "trace_error" not in trace
    assert trace["summary"]["operations_covered"] == 1
    assert any("GET /catalogue" in test["operations"] for test in trace["tests"])
    for heading in ("Категории по операциям", "Фактические входные условия",
                    "Цепочечный сценарий", "Объяснение и трассировка результатов"):
        assert heading in markdown


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
