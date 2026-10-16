"""CLI `tester` writes a run journal (ТЗ v3 4.1.8); fake agents, no provider calls."""

import json
from types import SimpleNamespace

import pytest

import prototype


CONTRACT = "openapi: 3.0.3\ninfo: {title: Demo, version: '1.0'}\npaths:\n  /items:\n    get: {}\n"
GENERATION = {
    "tool": "generate_tests_tool", "pipeline_status": "success", "status": "success", "attempts": 1,
    "tokens": {"input": 10, "output": 2, "total": 12},
    "run_result": {"status": "completed", "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0}},
    "saved_versions": [{"attempt": 1, "run_id": "r1", "tests_dir": "t1"}],
    "repair_log": [],
}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unused").write_text(CONTRACT, encoding="utf-8")
    return tmp_path


def journal_in(root):
    path = next(root.rglob("journal.jsonl"))
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, events


def names(events):
    return [event["event"] if event["event"] != "stage" else "stage:" + event["data"]["name"] for event in events]


def generation_agent(stream=None):
    class ToolMessage:
        content = json.dumps(GENERATION)

    messages = [ToolMessage()]
    return lambda **kw: SimpleNamespace(stream=stream or (lambda *a, **k: iter([{"messages": messages}])),
                                        invoke=lambda _: {"messages": messages})


def test_generate_run_writes_journal(workdir, monkeypatch):
    from prototype.generator.contracts import GenerationSettings

    monkeypatch.setattr(prototype, "build_agent", generation_agent())
    code = prototype.run("unused", report_dir=workdir,
                         generation_settings=GenerationSettings(contract_path="unused", output_dir=workdir))
    run_dir, events = journal_in(workdir)
    order = names(events)
    for expected in ("run.start", "stage:contract_loaded", "stage:agent_built", "tests.run", "run.end"):
        assert expected in order
    assert order[0] == "run.start" and order[-1] == "run.end"
    assert order.index("stage:contract_loaded") < order.index("stage:agent_built") < order.index("tests.run")
    assert events[-1]["data"]["exit_code"] == code
    assert events[0]["data"]["mode"] == "generate"
    assert (run_dir / "journal.md").is_file()
    assert (run_dir / "run-metadata.json").is_file()


def test_default_agent_mode_journal(workdir, monkeypatch):
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(invoke=lambda _: {"messages": []}))
    prototype.run("unused", report_dir=workdir)
    _, events = journal_in(workdir)
    assert events[0]["event"] == "run.start" and events[0]["data"]["mode"] == "agent"
    assert events[-1]["event"] == "run.end" and events[-1]["data"]["outcome"] == "ok"


def test_broken_contract_writes_contract_error(workdir):
    from prototype.parser.contract import ContractInputError

    (workdir / "broken.yaml").write_text("::: not yaml [", encoding="utf-8")
    with pytest.raises(ContractInputError):
        prototype.run("broken.yaml", report_dir=workdir)
    run_dir, events = journal_in(workdir)
    assert (run_dir / "input-error.json").is_file()
    assert "contract.error" in names(events)
    assert events[-1]["event"] == "run.end" and events[-1]["data"]["outcome"] == "error"


def test_agent_interrupt_still_closes_journal(workdir, monkeypatch):
    from prototype.generator.contracts import GenerationSettings

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
        yield  # pragma: no cover

    monkeypatch.setattr(prototype, "build_agent", generation_agent(stream=interrupted))
    assert prototype.run("unused", report_dir=workdir,
                         generation_settings=GenerationSettings(contract_path="unused", output_dir=workdir)) == 1
    run_dir, events = journal_in(workdir)
    assert events[-1]["event"] == "run.end" and events[-1]["data"]["exit_code"] == 1
    assert (run_dir / "journal.md").is_file()


def test_exception_inside_run_is_recorded_and_reraised(workdir, monkeypatch):
    import prototype.evaluate.metrics as metrics

    def broken(**kwargs):
        raise RuntimeError("metrics crashed")

    monkeypatch.setattr(metrics, "calculate_metrics", broken)
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(invoke=lambda _: {"messages": []}))
    with pytest.raises(RuntimeError, match="metrics crashed"):
        prototype.run("unused", report_dir=workdir)
    run_dir, events = journal_in(workdir)
    assert events[-1]["event"] == "run.end" and events[-1]["data"]["outcome"] == "error"
    assert "metrics crashed" in events[-1]["data"]["error"]
    assert (run_dir / "journal.md").is_file()


def test_run_metadata_has_launch(workdir, monkeypatch):
    from prototype.generator.contracts import GenerationSettings

    monkeypatch.setattr(prototype, "build_agent", generation_agent())
    settings = GenerationSettings(contract_path="unused", base_url="http://catalogue:8080", network="n",
                                  output_dir=workdir)
    prototype.run("unused", report_dir=workdir, generation_settings=settings)
    run_dir, events = journal_in(workdir)
    launch = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))["launch"]
    assert launch["mode"] == "generate"
    assert launch["base_url"] == "http://catalogue:8080"
    assert launch["network"] == "n"
    assert events[0]["data"]["launch"] == launch


def test_inspect_contract_writes_journal(workdir):
    prototype.inspect_contract("unused", report_dir=workdir)
    run_dir, events = journal_in(workdir)
    assert (run_dir / "contract-report.json").is_file()
    assert events[0]["data"]["mode"] == "inspect"
    assert events[-1]["event"] == "run.end"
