"""Schemathesis context enrichment (TZ v3 4.1.2.3): pipeline wiring and digest.

No network: the Schemathesis fetch is injected, the LLM is scripted and the
runner returns a passing fit. Asserted: the digest content and its bounds,
the service call arguments, the never-abort policy and the provenance.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from prototype.generator.contracts import GenerationSettings
from prototype.generator.context import schemathesis_digest
from prototype.generator.generate import generate_suite
from prototype.generator.pipeline import run_generation_pipeline
from prototype.service_tools.runner.contracts import (
    RunResult,
    RunStatus,
    TestResult as RunnerTestResult,
)

CONTRACT_TEMPLATE = """\
openapi: "3.0.3"
info:
  title: Demo Catalogue API
  version: 1.0.0
paths:
  /catalogue:
    get:
      responses:
        "200": {description: OK}
"""

GOOD_FILE = (
    "def test_good(base_url, api_client):\n"
    "    response = api_client.get(f\"{base_url}/catalogue\")\n"
    "    assert response.status_code == 200\n"
)

SCHEMATHESIS_RESULT = {
    "backend": "schemathesis",
    "status": "completed",
    "summary": {"total": 40, "passed": 30, "failed": 5, "errors": 2, "skipped": 3},
    "success": False,
    "failures": [
        {"operation": "GET /catalogue/{id}", "check": "status_code_mismatch",
         "title": "Server returned 500", "message": "long message",
         "code_sample": "def probe():\n    raise Exception('secret-detail')"},
        {"operation": "GET /catalogue/{id}", "check": "status_code_mismatch",
         "title": "Server returned 500", "message": "duplicate",
         "code_sample": "def probe(): pass"},
    ],
    "duration_seconds": 12.3,
    "raw": {"huge": "x" * 5000},
}


class FakeModel:
    """Scripted single-file generation; records the prompt it received."""

    def __init__(self):
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append([message["content"] for message in messages])
        payload = json.dumps(
            {"files": [{"name": "test_catalogue.py", "code": GOOD_FILE}]},
            ensure_ascii=False)
        return SimpleNamespace(
            content=payload,
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def _passing_runner(config):
    return RunResult(RunStatus.COMPLETED, 0, 0.5, tests=(
        RunnerTestResult("test_catalogue.py::test_good", "passed"),))


class FakeSaver:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            run_id=f"run-{len(self.requests)}",
            tests_dir=str(request.contract_path.parent / "generated"))


def _settings(tmp_path, **overrides):
    path = tmp_path / "contract.yaml"
    path.write_text(CONTRACT_TEMPLATE, encoding="utf-8")
    base = {"contract_path": path, "output_dir": tmp_path, "max_repair_attempts": 1}
    base.update(overrides)
    return GenerationSettings(**base)


def _run(tmp_path, overrides=None, *, schemathesis_fn=None, model=None):
    model = model or FakeModel()
    result = run_generation_pipeline(
        _settings(tmp_path, **(overrides or {})),
        model=model,
        run_tests_fn=_passing_runner,
        save_fn=FakeSaver(),
        schemathesis_fn=schemathesis_fn,
    )
    return result, model


def test_generation_prompt_contains_bounded_schemathesis_digest(tmp_path):
    calls = []

    def fetch(contract_path, base_url, service_url, timeout):
        calls.append({"contract_path": str(contract_path), "base_url": base_url,
                      "service_url": service_url, "timeout": timeout})
        return SCHEMATHESIS_RESULT

    result, model = _run(tmp_path, {"base_url": "http://catalogue:8080"},
                         schemathesis_fn=fetch)
    assert result.status == "success"
    # The snapshot of the original bytes, the live address, the default
    # service URL and the configured budget go to the service.
    assert calls[0]["contract_path"].endswith("inputs/contract.yaml")
    assert calls[0]["base_url"] == "http://catalogue:8080"
    assert calls[0]["service_url"] == "http://127.0.0.1:8080"
    assert calls[0]["timeout"] == 60.0

    user = model.prompts[0][1]
    assert "Сводка Schemathesis (диагностический контекст" in user
    assert "Schemathesis: completed (время: 12.3с)" in user
    assert "сценарии: total 40, passed 30, failed 5, errors 2, skipped 3" in user
    assert "неудачные проверки (1):" in user  # duplicates collapsed
    assert "GET /catalogue/{id}: status_code_mismatch — Server returned 500" in user
    # Code samples and the raw payload never reach the model.
    assert "secret-detail" not in user
    assert "x" * 5000 not in user

    # The full run is stored as an immutable input artifact of the run.
    run_dir = Path(result.report_paths["json"]).parent
    artifact = run_dir / "inputs" / "schemathesis-summary.json"
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["summary"]["total"] == 40

    assert result.provenance["schemathesis"]["status"] == "completed"
    assert result.provenance["schemathesis"]["scenarios"] == {
        "total": 40, "passed": 30, "failed": 5, "errors": 2, "skipped": 3}
    assert result.provenance["schemathesis"]["failures_total"] == 2


def test_unavailable_schemathesis_never_breaks_generation(tmp_path):
    def fetch(*args):
        raise ConnectionError("connection refused")

    result, model = _run(tmp_path, {"base_url": "http://catalogue:8080"},
                         schemathesis_fn=fetch)
    assert result.status == "success"
    assert result.provenance["schemathesis"]["status"] == "unavailable"
    assert "connection refused" in result.provenance["schemathesis"]["reason"]
    assert "Сводка Schemathesis" not in model.prompts[0][1]


def test_disabled_enrichment_never_calls_the_service(tmp_path):
    def fetch(*args):
        pytest.fail("the service must not be called when enrichment is disabled")

    result, model = _run(tmp_path, {
        "base_url": "http://catalogue:8080",
        "enrich_with_schemathesis": False,
    }, schemathesis_fn=fetch)
    assert result.status == "success"
    assert result.provenance["schemathesis"] == {
        "status": "skipped", "reason": "enrichment disabled"}
    assert "Сводка Schemathesis" not in model.prompts[0][1]


def test_unit_mode_without_base_url_skips_the_service(tmp_path):
    def fetch(*args):
        pytest.fail("there is no live service in unit mode")

    result, model = _run(tmp_path, {}, schemathesis_fn=fetch)
    assert result.status == "success"
    assert result.provenance["schemathesis"]["status"] == "skipped"
    assert "no base_url" in result.provenance["schemathesis"]["reason"]
    assert "Сводка Schemathesis" not in model.prompts[0][1]


def test_invalid_schemathesis_result_is_recorded_without_digest(tmp_path):
    result, model = _run(tmp_path, {"base_url": "http://catalogue:8080"},
                         schemathesis_fn=lambda *args: {"status": "error"})
    assert result.status == "success"
    assert result.provenance["schemathesis"]["status"] == "invalid_result"
    assert "Сводка Schemathesis" not in model.prompts[0][1]


def test_custom_service_url_and_timeout_are_passed_to_the_service(tmp_path):
    calls = []

    def fetch(contract_path, base_url, service_url, timeout):
        calls.append((service_url, timeout))
        return {"backend": "schemathesis", "status": "no_tests",
                "summary": {"total": 0, "passed": 0, "failed": 0,
                            "errors": 0, "skipped": 0},
                "failures": [], "duration_seconds": 1.0}

    _run(tmp_path, {
        "base_url": "http://catalogue:8080",
        "schemathesis_service_url": "http://10.0.0.5:9000",
        "schemathesis_timeout_seconds": 25,
    }, schemathesis_fn=fetch)
    assert calls == [("http://10.0.0.5:9000", 25)]


def test_generate_suite_forwards_schemathesis_context():
    contract = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"},
                "paths": {"/catalogue": {"get": {"responses": {"200": {
                    "description": "OK"}}}}}}
    model = FakeModel()
    result = generate_suite(contract, GenerationSettings(contract_path="unused"),
                            model=model, schemathesis_context="Schemathesis: completed")
    assert result["calls"] == 1
    user = model.prompts[0][1]
    assert "Сводка Schemathesis" in user
    assert "Schemathesis: completed" in user

    bare = FakeModel()
    generate_suite(contract, GenerationSettings(contract_path="unused"), model=bare)
    assert "Сводка Schemathesis" not in bare.prompts[0][1]


def test_schemathesis_digest_bounded_and_deduplicated():
    failures = [{"operation": f"GET /op/{index}", "check": "check", "title": f"title-{index}"}
                for index in range(12)]
    digest = schemathesis_digest({
        "status": "completed",
        "summary": {"total": 12, "passed": 0, "failed": 12, "errors": 0, "skipped": 0},
        "failures": failures * 3,
        "raw": {"huge": "y" * 5000},
    })
    assert "неудачные проверки (12):" in digest  # duplicates collapsed
    assert "GET /op/0: check — title-0" in digest
    assert "… и ещё 2 (подробности в отчёте)" in digest
    assert "code_sample" not in digest
    assert "y" * 5000 not in digest


def test_schemathesis_digest_stays_within_the_token_budget():
    failures = [{"operation": f"GET /op/{index}", "check": "check", "title": "t" * 300}
                for index in range(12)]
    digest = schemathesis_digest({
        "status": "completed",
        "summary": {"total": 12, "passed": 0, "failed": 12, "errors": 0, "skipped": 0},
        "failures": failures,
    })
    assert len(digest) <= 1500
    assert digest.endswith("(сводка обрезана)")


def test_schemathesis_digest_ignores_unusable_input():
    assert schemathesis_digest(None) == ""
    assert schemathesis_digest("nope") == ""
    assert schemathesis_digest({}) == "Schemathesis: unknown"


def test_schemathesis_settings_validation():
    with pytest.raises(ValueError, match="schemathesis_timeout_seconds"):
        GenerationSettings(contract_path="unused", schemathesis_timeout_seconds=0)
    with pytest.raises(ValueError, match="schemathesis_timeout_seconds"):
        GenerationSettings(contract_path="unused", schemathesis_timeout_seconds=601)
    with pytest.raises(ValueError, match="enrich_with_schemathesis"):
        GenerationSettings(contract_path="unused", enrich_with_schemathesis="yes")
    with pytest.raises(ValueError, match="schemathesis_service_url"):
        GenerationSettings(contract_path="unused", schemathesis_service_url="  ")
    defaults = GenerationSettings(contract_path="unused")
    assert defaults.enrich_with_schemathesis is True
    assert defaults.schemathesis_service_url is None
    assert defaults.schemathesis_timeout_seconds == 60.0
