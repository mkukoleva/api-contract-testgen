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
        "--tests-output-dir", str(tmp_path / "saved"), "--user-story", str(tmp_path / "story.json")])
    prototype.main()
    config = captured["generation_settings"]
    assert config.base_url == "http://catalogue:8080" and config.network == "trusted-api"
    assert config.blocked_networks == ("database",)
    assert config.runner_timeout_seconds == 20 and config.max_repair_attempts == 2
    assert config.output_dir == tmp_path / "reports" and config.output_root == tmp_path / "saved"
    assert config.user_story_path == tmp_path / "story.json"


def test_user_story_cli_requires_generation(monkeypatch):
    monkeypatch.setattr(prototype, "run", lambda *a, **k: pytest.fail("No agent for invalid mode"))
    monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", "--user-story", "story.json"])
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 2


def test_unit_cli_requires_source(monkeypatch):
    monkeypatch.setattr(prototype, "run", lambda *a, **k: pytest.fail("invalid mode reached agent"))
    for flags in (["--generation-unit"], ["--generate", "--generation-unit",
                  "--base-url", "http://catalogue:8080", "--network", "trusted-api"]):
        monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", *flags])
        with pytest.raises(SystemExit) as exc:
            prototype.main()
        assert exc.value.code == 2


@pytest.mark.parametrize("status,exit_code", [("passed", 0), ("failed", 1)])
def test_standalone_unit_needs_no_contract_or_http(tmp_path, monkeypatch, status, exit_code):
    from prototype.generator import unit
    source = tmp_path / "module.py"
    source.write_text("def double(value):\n    return value * 2\n")
    monkeypatch.setattr(prototype, "run", lambda *a, **k: pytest.fail("unit-only must not start HTTP agent"))
    monkeypatch.setattr(unit, "run_unit_generation", lambda *a, **k:
                        {"status": status, "summary_path": str(tmp_path / "summary.json")})
    monkeypatch.setattr("sys.argv", ["tester", "--generation-unit", "--source-code", str(source)])
    if exit_code:
        with pytest.raises(SystemExit) as exc:
            prototype.main()
        assert exc.value.code == exit_code
    else:
        prototype.main()


def test_unit_cli_runs_only_after_successful_http_generation(tmp_path, monkeypatch):
    from prototype.generator import unit
    source = tmp_path / "math.py"
    source.write_text("def double(value):\n    return value * 2\n")
    calls = []
    monkeypatch.setattr(unit, "run_unit_generation", lambda *a, **k: calls.append((a, k)) or
                        {"status": "passed", "summary_path": str(tmp_path / "summary.json")})
    monkeypatch.setattr("sys.argv", ["tester", "contract.yaml", "--generate", "--generation-unit",
        "--source-code", str(source), "--base-url", "http://catalogue:8080", "--network", "trusted-api"])
    monkeypatch.setattr(prototype, "run", lambda *a, **k: 0)
    prototype.main()
    assert calls and calls[0][0] == (source,)
    monkeypatch.setattr(prototype, "run", lambda *a, **k: 1)
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 1 and len(calls) == 1


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
