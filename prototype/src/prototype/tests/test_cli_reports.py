"""CLI report persistence without calling an agent or an LLM."""

from datetime import datetime, timezone
from types import SimpleNamespace

import prototype
import pytest
from prototype.parser import contract


def test_cli_runs_at_same_time_keep_both_reports_and_metrics(tmp_path, monkeypatch):
    class FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 10, 8, tzinfo=timezone.utc if tz else None)

    outcomes = iter(("first result", "second result"))
    monkeypatch.setattr(prototype, "datetime", FrozenDatetime)
    monkeypatch.setattr(prototype, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(
        invoke=lambda _: {"value": next(outcomes), "messages": []}))
    monkeypatch.setattr(prototype, "_extract_final_message", lambda result: result["value"])
    monkeypatch.setattr(contract, "read_contract_summary", lambda _: {"operation_count": 1})

    prototype.run("contract.yaml")
    first = next(tmp_path.rglob("report_*.md"))
    original = first.read_bytes()
    prototype.run("contract.yaml")

    reports = list(tmp_path.rglob("report_*.md"))
    assert len(reports) == 2
    assert first.read_bytes() == original
    assert any("second result" in path.read_text(encoding="utf-8") for path in reports)
    assert len(list(tmp_path.rglob("metrics_*.json"))) == 2
    assert len({path.parent for path in reports}) == 2


@pytest.mark.parametrize("content", [None, "paths: [", '{"openapi":"2.0"}',
                                     '{"openapi":"3.0.3","paths":[]}'])
def test_cli_invalid_contract_exits_without_calling_agent(tmp_path, monkeypatch, capsys, content):
    path = tmp_path / "bad.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tester", str(path)])
    monkeypatch.setattr(prototype, "REPORTS_DIR", tmp_path / "reports")

    def unexpected_agent():
        pytest.fail("An invalid contract must not reach the agent")

    monkeypatch.setattr(prototype, "build_agent", unexpected_agent)
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "error:" in error
    assert "Traceback" not in error
    assert not (tmp_path / "reports").exists()


def test_cli_report_dir_keeps_results_outside_default_directory(tmp_path, monkeypatch):
    selected = tmp_path / "external reports"
    default = tmp_path / "default"
    path = tmp_path / "valid.yaml"
    path.write_text("openapi: 3.0.3\ninfo: {}\npaths: {}", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["tester", str(path), "--report-dir", str(selected)])
    monkeypatch.setattr(prototype, "REPORTS_DIR", default)
    monkeypatch.setattr(prototype, "build_agent", lambda: SimpleNamespace(
        invoke=lambda _: {"messages": []}))
    prototype.main()
    assert len(list(selected.rglob("report_*.md"))) == 1
    assert len(list(selected.rglob("metrics_*.json"))) == 1
    assert not default.exists()
