"""Generation facts -> common metrics; no Docker, provider calls or edits."""

import json

import pytest
from langchain_core.messages import ToolMessage

from prototype.evaluate.metrics import calculate_metrics, format_metrics_markdown, save_metrics_report
from prototype.evaluate.runnability import compute_runnability
from prototype.service_tools.runner.tools import _generation_result
from prototype.tests.test_generation_tool import _fake_run


def metrics_for(run):
    facts = _generation_result(run)
    # The full artifact is authoritative even if the model-visible text is clipped.
    message = ToolMessage(content='{"tool":"generate_tests_tool","summary":{"total":999}}',
                          artifact=facts, tool_call_id="one")
    return calculate_metrics(agent_result={"messages": [message]},
                             contract_summary={"operation_count": 1}, generation_time_seconds=3)


def test_parametrized_cases_and_static_runnability_have_independent_totals(tmp_path):
    run = _fake_run(run_result={"status": "completed", "summary": {
        "total": 53, "passed": 50, "failed": 1, "error": 1, "skipped": 1}},
        runnability={"expected_total": 24, "ran_total": 22,
                     "runnability_percent": 91.67, "reason": None})
    metrics = metrics_for(run)
    assert metrics.test_suite_size == 53  # Not just passed cases or HTTP calls.
    assert metrics.total_generated_tests == 24 and metrics.runnable_tests == 22
    assert metrics.runnability_percent == 91.67
    assert metrics.mutation_score_percent is None and metrics.contract_coverage_percent is None
    assert metrics.tests_without_manual_edits_percent is None
    path = save_metrics_report(metrics, tmp_path)
    assert json.loads(path.read_text())["metrics"]["test_suite_size"] == 53
    table = format_metrics_markdown(metrics)
    assert "| Test Suite Size | 53 |" in table and "| Runnability | 91.67 % |" in table


def test_assertion_failure_counts_as_ran_but_skip_and_setup_error_do_not():
    expected = [f"test_a.py::test_{i}" for i in range(4)]
    result = {"status": "completed", "summary": {"total": 4}, "tests": [
        {"nodeid": nodeid, "outcome": outcome} for nodeid, outcome in
        zip(expected, ("passed", "failed", "skipped", "error"))]}
    metrics = metrics_for(_fake_run(run_result=result,
        runnability=compute_runnability(expected, result).to_dict()))
    assert metrics.test_suite_size == 4
    assert metrics.runnable_tests == 2 and metrics.runnability_percent == 50.0


@pytest.mark.parametrize("status", ["timeout", "interrupted", "infrastructure_error", "collection_error"])
def test_partial_results_do_not_pretend_to_be_the_full_suite(status):
    expected = ["test_a.py::test_x"]
    result = {"status": status, "summary": {"total": 1}, "tests": []}
    source = compute_runnability(expected, result).to_dict()
    metrics = metrics_for(_fake_run(status="execution_failed", run_result=result, runnability=source))
    assert metrics.test_suite_size is None
    assert metrics.runnability_percent == source["runnability_percent"]
    assert metrics.runnability_reason == source["reason"]


def test_no_result_is_unknown_but_completed_empty_collection_is_zero():
    missing = metrics_for(_fake_run(status="generation_error", run_result=None,
                                    runnability=compute_runnability([], None).to_dict()))
    assert missing.test_suite_size is None and missing.runnability_percent is None
    empty = metrics_for(_fake_run(run_result={"status": "no_tests", "summary": {"total": 0}},
                                  runnability=compute_runnability([], None).to_dict()))
    assert empty.test_suite_size == 0 and empty.runnability_percent is None


def test_legacy_flat_metrics_payload_remains_supported():
    data = {"tool": "generate_tests_tool", "test_suite_size": 8,
            "total_generated_tests": 8, "runnable_tests": 6}
    message = ToolMessage(content=json.dumps(data), tool_call_id="legacy")
    metrics = calculate_metrics(agent_result={"messages": [message]},
                                 contract_summary={}, generation_time_seconds=1)
    assert metrics.test_suite_size == 8 and metrics.runnability_percent == 75.0


@pytest.mark.parametrize("mutation_score", [None, 50.0])
def test_primary_cli_writes_common_metrics_from_final_pipeline(tmp_path, monkeypatch, mutation_score):
    from types import SimpleNamespace
    import prototype
    from prototype.generator.contracts import GenerationSettings
    from prototype.service_tools.runner.tools import create_generation_tool
    contract = tmp_path / "api.yaml"
    contract.write_text("openapi: 3.0.3\npaths: {/items: {get: {}}}\n")
    run = _fake_run(run_result={"status": "completed", "summary": {"total": 53, "passed": 53}},
        runnability={"expected_total": 24, "ran_total": 24,
                     "runnability_percent": 100.0, "reason": None})
    monkeypatch.setattr("prototype.generator.pipeline.run_generation_pipeline", lambda *a, **kw: run)
    settings = GenerationSettings(contract)
    tool = create_generation_tool(settings)
    message = tool.invoke({"type": "tool_call", "name": "generate_tests_tool", "args": {}, "id": "one"})
    monkeypatch.setattr(prototype, "build_agent", lambda **kw: SimpleNamespace(
        stream=lambda *a, **kw: iter([{"messages": [message]}])))
    reports = tmp_path / "reports"
    options = {}
    if mutation_score is not None:
        def evaluate(**kwargs):
            assert kwargs['generation']['runner_status'] == 'completed'
            assert kwargs['contract_sha256']
            return {'status': 'completed', 'reason': None,
                    'mutation_score_percent': mutation_score, 'summary_path': 'current-summary.json'}
        monkeypatch.setattr('prototype.evaluate.mutation_integration.evaluate_generated_suite', evaluate)
        options['mutation_adapter'] = 'catalogue'
    assert prototype.run(str(contract), report_dir=reports, generation_settings=settings, **options) == 0
    data = json.loads(next(reports.rglob("metrics_*.json")).read_text())["metrics"]
    assert data["test_suite_size"] == 53 and data["runnability_percent"] == 100.0
    table = next(reports.rglob("report_*.md")).read_text()
    assert "| Test Suite Size | 53 |" in table and "| Runnability | 100.0 % |" in table

    assert data["mutation_score_percent"] == mutation_score
    if mutation_score is not None:
        assert data["mutation_report_path"] == "current-summary.json"
        assert "| Mutation Score | 50.0 % |" in table
