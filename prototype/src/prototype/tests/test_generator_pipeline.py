"""Generation pipeline end-to-end checks with faked LLM and runner.

No Docker, no network, no LLM credentials: the model replies are scripted and
the runner reads the prepared attempt directories and returns scripted fits.

All tests use a temporary contract that documents 404 for /catalogue/{id}, so
a *documented* 404 observed by a test is repairable while an undocumented 500
is a suspected service defect.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from prototype.generator.contracts import GenerationSettings
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
  /catalogue/{id}:
    get:
      parameters:
        - name: id
          in: path
          required: true
          schema: {type: string}
      responses:
        "200": {description: "Товар"}
        "404": {description: "Нет товара"}
"""

GOOD_FILE = (
    "def test_good(base_url, api_client):\n"
    "    response = api_client.get(f\"{base_url}/catalogue\")\n"
    "    assert response.status_code == 200\n"
)
BAD_FUNCTION = (
    "def test_bad(base_url, api_client):\n"
    "    response = api_client.get(f\"{base_url}/catalogue/999999\")\n"
    "    assert response.status_code == 200\n"
)
FIXED_BAD = (
    "def test_bad(base_url, api_client):\n"
    "    response = api_client.get(f\"{base_url}/catalogue/1\")\n"
    "    assert response.status_code == 200  # REPAIRED\n"
)


class FakeModel:
    """Scripted model: one generation reply, then one repair reply per item."""

    def __init__(self):
        self.generation_files = [
            {"name": "test_catalogue.py", "code": GOOD_FILE + BAD_FUNCTION}
        ]
        self.repair_responses = [FIXED_BAD]
        self.usage = {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}
        self.calls = []  # (kind, messages)

    def _reply(self, kind, content):
        self.calls.append((kind, content))
        return SimpleNamespace(content=content, usage_metadata=dict(self.usage))

    def invoke(self, messages):
        system = messages[0]["content"]
        if "генератор pytest-тестов" in system:
            payload = json.dumps({"files": self.generation_files}, ensure_ascii=False)
            self.calls.append(("generation", messages))
            return SimpleNamespace(content=payload, usage_metadata=dict(self.usage))
        fixed = self.repair_responses.pop(0) if self.repair_responses else None
        payload = json.dumps({"code": fixed if fixed is not None else BAD_FUNCTION})
        self.calls.append(("repair", messages))
        return SimpleNamespace(content=payload, usage_metadata=dict(self.usage))


def _failure_message_for(code: str) -> str | None:
    """Scripted failure matching the current test_bad body."""
    if "REPAIRED" in code:
        return None
    if "assert 1 == 2" in code:
        return "assert 2 == 1\nE   assert 2 == 1"
    if "assert 3 == 4" in code:
        return "assert 4 == 3\nE   assert 4 == 3"
    return (
        "assert 404 == 200\nE   assert response.status_code == 200\n"
        "E   assert 404 == 200"
    )


class ScriptedRunner:
    """Returns passed/failed per current file content."""

    def __init__(self, *, infra=False, defect_message=None):
        self.infra = infra
        self.defect_message = defect_message
        self.configs = []

    def __call__(self, config):
        self.configs.append(config)
        if self.infra:
            return RunResult(
                RunStatus.INFRASTRUCTURE_ERROR, None, 0.5,
                error_message="Docker unavailable",
            )
        tests = []
        for path in sorted(config.tests_dir.glob("*.py")):
            code = path.read_text(encoding="utf-8")
            if "def test_good" in code:
                tests.append(RunnerTestResult("test_catalogue.py::test_good", "passed"))
            if "def test_bad" in code:
                if self.defect_message is not None:
                    message = self.defect_message
                else:
                    message = _failure_message_for(code)
                if message is None:
                    tests.append(RunnerTestResult("test_catalogue.py::test_bad", "passed"))
                else:
                    tests.append(RunnerTestResult(
                        "test_catalogue.py::test_bad", "failed", message=message,
                    ))
        if not tests:
            return RunResult(RunStatus.NO_TESTS, 5, 0.5)
        bad = any(test.outcome == "failed" for test in tests)
        return RunResult(RunStatus.COMPLETED, 1 if bad else 0, 0.5, tests=tuple(tests))


class FakeSaver:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            run_id=f"run-{len(self.requests)}",
            tests_dir=str(request.contract_path.parent / "generated" / str(len(self.requests))),
        )


def contract(tmp_path):
    path = tmp_path / "contract.yaml"
    path.write_text(CONTRACT_TEMPLATE, encoding="utf-8")
    return path


def settings(tmp_path, contract_path, **overrides):
    base = {
        "contract_path": contract_path,
        "output_dir": tmp_path,
        "max_repair_attempts": 3,
        "repair_token_budget": 30_000,
    }
    base.update(overrides)
    return GenerationSettings(**base)


def run_pipeline(tmp_path, model, runner, saver, **overrides):
    return run_generation_pipeline(
        settings(tmp_path, contract(tmp_path), **overrides),
        model=model, run_tests_fn=runner, save_fn=saver,
    )


def test_success_is_final_and_llm_is_called_only_for_generation(tmp_path):
    model = FakeModel()
    model.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE}]
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "success"
    assert result.attempts == 0
    assert [kind for kind, _ in model.calls] == ["generation"]
    assert len(saver.requests) == 1  # only attempt 0 is stored
    assert result.repair_log == ()
    assert result.suspected_defects == ()
    assert result.run_result["status"] == "completed"
    assert result.run_result["summary"]["passed"] == 1
    assert result.run_result["summary"]["failed"] == 0


def test_infrastructure_error_does_not_trigger_repair(tmp_path):
    model = FakeModel()
    runner = ScriptedRunner(infra=True)
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "infrastructure_error"
    assert result.attempts == 0
    assert [kind for kind, _ in model.calls] == ["generation"]
    assert result.repair_log == ()
    assert len(runner.configs) == 1
    assert result.generator_errors == ()


def test_service_defect_is_reported_and_assert_is_kept(tmp_path):
    model = FakeModel()
    message = (
        "assert 500 == 200\nE   assert response.status_code == 200\n"
        "E   assert 500 == 200"
    )
    runner = ScriptedRunner(defect_message=message)
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "completed_with_failures"
    assert result.attempts == 0
    # No repair call happened for the defect.
    assert [kind for kind, _ in model.calls] == ["generation"]
    assert len(result.suspected_defects) == 1
    assert result.suspected_defects[0]["nodeid"] == "test_catalogue.py::test_bad"
    assert "not documented" in result.suspected_defects[0]["reason"]
    assert result.repair_log == ()


def test_repair_fixes_only_the_failing_function(tmp_path):
    model = FakeModel()
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "success"
    assert result.attempts == 1
    assert len(result.repair_log) == 1
    assert result.repair_log[0].outcome == "repaired"
    assert result.repair_log[0].target == "test_catalogue.py::test_bad"
    assert len(saver.requests) == 2  # generation version + repaired version

    # The repair prompt received only the failing function, not the passing one.
    repair_calls = [messages for kind, messages in model.calls if kind == "repair"]
    assert len(repair_calls) == 1
    user = next(m["content"] for m in repair_calls[0] if m["role"] == "user")
    assert "def test_bad" in user
    assert "def test_good" not in user


def test_repeated_identical_error_stops_the_loop(tmp_path):
    model = FakeModel()
    model.repair_responses = [BAD_FUNCTION]  # repair does not change anything
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "completed_with_failures"
    assert result.attempts == 1
    assert result.repair_log[0].outcome == "stuck"
    # Initial run + one run after the failed repair: the loop stopped.
    assert len(runner.configs) == 2
    assert any("same error repeated" in message for message in result.generator_errors)
    assert len(model.calls) == 2  # generation + 1 repair


def test_max_repair_attempts_bounds_the_loop(tmp_path):
    model = FakeModel()
    # Each repair changes the error signature, so the loop is not stuck early.
    model.repair_responses = [
        "def test_bad(base_url, api_client):\n    assert 1 == 2\n",
        "def test_bad(base_url, api_client):\n    assert 3 == 4\n",
    ]
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver, max_repair_attempts=2)

    assert result.status == "completed_with_failures"
    assert result.attempts == 2
    assert len(result.repair_log) == 2
    assert len(runner.configs) == 3  # initial + two repair runs
    assert any("max_repair_attempts=2 reached" in message for message in result.generator_errors)


def test_token_budget_stops_the_loop(tmp_path):
    model = FakeModel()
    model.repair_responses = ["def test_bad(base_url, api_client):\n    assert 1 == 2\n"]
    runner = ScriptedRunner()
    saver = FakeSaver()

    budget_one = run_pipeline(tmp_path, model, runner, saver, repair_token_budget=5)
    assert budget_one.attempts == 1
    assert budget_one.repair_log[0].outcome == "repaired"
    assert any("token budget=5 reached" in message for message in budget_one.generator_errors)

    budget_zero = run_pipeline(tmp_path, FakeModel(), ScriptedRunner(), FakeSaver(),
                               repair_token_budget=0)
    assert budget_zero.attempts == 0
    assert budget_zero.repair_log == ()
    assert any("token budget=0 reached" in message for message in budget_zero.generator_errors)


def test_each_attempt_is_stored_as_a_version(tmp_path):
    model = FakeModel()
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert [request.generator_meta["attempt"] for request in saver.requests] == [0, 1]
    assert [version["attempt"] for version in result.saved_versions] == [0, 1]
    assert result.saved_versions[0]["run_id"] == "run-1"
    # The code of the generated tests never comes back in the result.
    assert "def test_good" not in json.dumps(result.to_dict())


def test_generation_without_tests_is_no_tests(tmp_path):
    model = FakeModel()
    model.generation_files = [{"name": "test_empty.py", "code": "X = 1\n"}]
    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_pipeline(tmp_path, model, runner, saver)

    assert result.status == "no_tests"
    assert result.attempts == 0


def test_runner_crash_is_infrastructure_without_repair(tmp_path):
    model = FakeModel()

    def boom(config):
        raise RuntimeError("docker daemon is not running")

    result = run_pipeline(tmp_path, model, boom, FakeSaver())
    assert result.status == "infrastructure_error"
    assert result.attempts == 0
    assert any("runner failed" in message for message in result.generator_errors)


def test_default_runner_receives_network_isolation_from_settings(
    tmp_path, monkeypatch
):
    """The pipeline's default runner wrapper passes service-mode network."""
    import prototype.service_tools.runner.docker_runner as docker_runner

    captured = {}
    delegate = ScriptedRunner()

    def fake_run_tests(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return delegate(config)

    monkeypatch.setattr(docker_runner, "run_tests", fake_run_tests)

    model = FakeModel()
    model.generation_files = [{"name": "test_catalogue.py", "code": GOOD_FILE}]
    saver = FakeSaver()
    result = run_pipeline(
        tmp_path,
        model,
        None,  # fall back to the default (wrapped) runner
        saver,
        base_url="http://catalogue:8080",
        network="pytest-runner-catalogue_runner",
        blocked_networks=("pytest-runner-catalogue_database",),
    )

    assert result.status == "success"
    assert captured["config"].base_url == "http://catalogue:8080"
    assert captured["kwargs"]["network"] == "pytest-runner-catalogue_runner"
    assert captured["kwargs"]["blocked_networks"] == (
        "pytest-runner-catalogue_database",
    )


def test_default_model_is_shared_by_generation_and_repair(tmp_path, monkeypatch):
    """Without a model override the pipeline builds one default and reuses it."""
    import prototype.llm.model as llm_model

    fake = FakeModel()
    monkeypatch.setattr(llm_model, "build_model", lambda: fake)

    runner = ScriptedRunner()
    saver = FakeSaver()
    result = run_generation_pipeline(
        settings(tmp_path, contract(tmp_path)),
        model=None,  # force the default build_model() path
        run_tests_fn=runner,
        save_fn=saver,
    )

    assert result.status == "success"
    assert result.attempts == 1
    assert result.repair_log[0].outcome == "repaired"
    kinds = [kind for kind, _ in fake.calls]
    assert kinds == ["generation", "repair"]
