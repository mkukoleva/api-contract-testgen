"""Primary CLI -> trusted settings -> configured agent; no provider calls."""

from types import SimpleNamespace

import pytest

import prototype


def test_generate_cli_passes_only_trusted_settings(tmp_path, monkeypatch):
    captured = {}
    def run(contract, **kwargs):
        captured.update(contract=contract, **kwargs)
        return 0
    monkeypatch.setattr(prototype, "run", run)
    monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", "--generate",
        "--base-url", "http://catalogue:8080", "--network", "trusted-api",
        "--blocked-network", "database", "--runner-timeout", "20",
        "--max-repair-attempts", "2", "--report-dir", str(tmp_path / "reports"),
        "--tests-output-dir", str(tmp_path / "saved")])
    prototype.main()
    config = captured["generation_settings"]
    assert config.base_url == "http://catalogue:8080" and config.network == "trusted-api"
    assert config.blocked_networks == ("database",)
    assert config.runner_timeout_seconds == 20 and config.max_repair_attempts == 2
    assert config.output_dir == tmp_path / "reports" and config.output_root == tmp_path / "saved"


@pytest.mark.parametrize("flags", [[], ["--base-url", "http://catalogue:8080"],
    ["--base-url", "http://user:secret@catalogue:8080", "--network", "trusted-api"],
    ["--base-url", "http://catalogue:8080", "--network", "trusted-api", "--runner-timeout", "nan"]])
def test_invalid_generation_configuration_never_reaches_agent(monkeypatch, flags):
    monkeypatch.setattr(prototype, "run", lambda *a, **k: pytest.fail("No agent for invalid settings"))
    monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", "--generate", *flags])
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 2


def test_generation_failure_has_a_nonzero_cli_exit(monkeypatch):
    monkeypatch.setattr(prototype, "run", lambda *a, **k: 1)
    monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", "--generate", "--base-url",
                                  "http://catalogue:8080", "--network", "trusted-api"])
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("result,expected", [(None, 1), ({"pipeline_status": "infrastructure_error"}, 1),
                                           ({"pipeline_status": "success"}, 0)])
def test_agent_run_cannot_claim_success_without_tool_result(tmp_path, monkeypatch, result, expected):
    import json
    from prototype.generator.contracts import GenerationSettings
    messages = []
    if result is not None:
        class ToolMessage:
            content = json.dumps({"tool": "generate_tests_tool", **result})
        messages.append(ToolMessage())
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unused").write_text("openapi: 3.0.3\npaths:\n  /items:\n    get: {}", encoding="utf-8")
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: SimpleNamespace(stream=lambda *a, **k: iter([{"messages": messages}]),
                                                                             invoke=lambda _: {"messages": messages}))
    config = GenerationSettings(contract_path="unused", output_dir=tmp_path)
    assert prototype.run("unused", report_dir=tmp_path, generation_settings=config) == expected


def test_agent_failure_after_tool_keeps_m3_result_and_returns_failure(tmp_path, monkeypatch):
    from prototype.generator.contracts import GenerationSettings
    class ToolMessage:
        content = '{"tool":"generate_tests_tool","pipeline_status":"success"}'
    def stream(*args, **kwargs):
        yield {"messages": [ToolMessage()]}
        raise RuntimeError("agent summary failed")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unused").write_text("openapi: 3.0.3\npaths:\n  /items:\n    get: {}", encoding="utf-8")
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: SimpleNamespace(stream=stream))
    assert prototype.run("unused", report_dir=tmp_path,
        generation_settings=GenerationSettings(contract_path="unused")) == 1
    report = next(tmp_path.rglob("report_*.md")).read_text(encoding="utf-8")
    assert "Статус: success" in report and "agent summary failed" in report


def test_agent_initialization_failure_has_report_without_leaking_key(tmp_path, monkeypatch):
    from prototype.generator.contracts import GenerationSettings
    monkeypatch.setenv("DEEPCODE_API_KEY", "dummy-private-key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unused").write_text("openapi: 3.0.3\npaths:\n  /items:\n    get: {}", encoding="utf-8")
    def fail(**kwargs):
        raise RuntimeError("provider rejected dummy-private-key")
    monkeypatch.setattr(prototype, "build_agent", fail)
    assert prototype.run("unused", report_dir=tmp_path,
        generation_settings=GenerationSettings(contract_path="unused")) == 1
    report = next(tmp_path.rglob("report_*.md")).read_text(encoding="utf-8")
    assert "provider rejected" in report and "dummy-private-key" not in report
