"""Runnability calculation checks; no Docker, no LLM, no network."""

from prototype.evaluate.runnability import compute_runnability, nodeid_matches


def _run(status="completed", tests=()):
    return {"status": status, "tests": list(tests)}


def _test(nodeid, outcome, **extra):
    return {"nodeid": nodeid, "outcome": outcome, **extra}


def test_passed_and_failed_count_as_ran():
    result = compute_runnability(
        ["test_a.py::test_ok", "test_a.py::test_bad"],
        _run(tests=[
            _test("test_a.py::test_ok", "passed"),
            _test("test_a.py::test_bad", "failed"),
        ]),
    )
    assert result.runnability_percent == 100.0
    assert result.ran_total == 2
    assert result.not_ran_nodeids == ()


def test_service_defect_failure_is_a_run():
    """A test that surfaced an undocumented 500 is runnable, not broken."""
    result = compute_runnability(
        ["test_catalogue.py::test_unknown_item"],
        _run(tests=[
            _test("test_catalogue.py::test_unknown_item", "failed",
                  message="assert 500 == 200"),
        ]),
    )
    assert result.ran_nodeids == ("test_catalogue.py::test_unknown_item",)
    assert result.runnability_percent == 100.0


def test_skipped_and_error_do_not_count_as_ran():
    result = compute_runnability(
        ["test_a.py::a", "test_a.py::b", "test_a.py::c", "test_a.py::d"],
        _run(tests=[
            _test("test_a.py::a", "passed"),
            _test("test_a.py::b", "skipped"),
            _test("test_a.py::c", "error"),
            _test("test_a.py::d", "passed"),
        ]),
    )
    assert result.ran_nodeids == ("test_a.py::a", "test_a.py::d")
    assert result.runnability_percent == 50.0
    assert result.not_ran_nodeids == ("test_a.py::b", "test_a.py::c")


def test_missing_expected_produces_no_percentage():
    result = compute_runnability([], _run(tests=[_test("x", "passed")]))
    assert result.expected_total == 0
    assert result.runnability_percent is None
    assert "нет ожидаемых тестовых случаев" in result.reason


def test_missing_run_result_produces_no_percentage():
    result = compute_runnability(["test_a.py::a"], None)
    assert result.runnability_percent is None
    assert "результатов прогона" in result.reason
    assert result.not_ran_nodeids == ("test_a.py::a",)


def test_timeout_keeps_counts_but_no_percentage():
    result = compute_runnability(
        ["test_a.py::a", "test_a.py::b"],
        _run(status="timeout", tests=[_test("test_a.py::a", "passed")]),
    )
    assert result.ran_total == 1
    assert result.runnability_percent is None
    assert "неполные (timeout)" in result.reason


def test_collection_error_with_known_expected_is_zero_percent():
    """Denominator is known, so 0% is accurate, not fabricated."""
    result = compute_runnability(["test_a.py::a"], _run(status="collection_error"))
    assert result.runnability_percent == 0.0
    assert result.reason is None


def test_parametrised_nodeids_match_static_expected():
    result = compute_runnability(
        ["test_a.py::test_p"],
        _run(tests=[
            _test("test_a.py::test_p[1]", "passed"),
            _test("test_a.py::test_p[2]", "failed"),
        ]),
    )
    assert result.ran_total == 1
    assert result.runnability_percent == 100.0


def test_interrupted_run_gives_no_percentage():
    result = compute_runnability(["test_a.py::a"], _run(status="interrupted"))
    assert result.runnability_percent is None
    assert "неполные (interrupted)" in result.reason


def test_infrastructure_error_gives_no_percentage():
    """Docker/API unavailable: the tests never ran, so 0% would be misleading."""
    result = compute_runnability(
        ["test_a.py::a"], _run(status="infrastructure_error")
    )
    assert result.runnability_percent is None
    assert "тесты не выполнялись" in result.reason


def test_expected_unseen_by_runner_is_not_ran():
    result = compute_runnability(
        ["test_a.py::a", "test_a.py::missing"],
        _run(tests=[_test("test_a.py::a", "passed")]),
    )
    assert result.not_ran_nodeids == ("test_a.py::missing",)
    assert result.runnability_percent == 50.0


def test_duplicate_expected_nodeids_are_deduplicated():
    result = compute_runnability(
        ["test_a.py::a", "test_a.py::a"],
        _run(tests=[_test("test_a.py::a", "passed")]),
    )
    assert result.expected_total == 1
    assert result.runnability_percent == 100.0


def test_nodeid_matches_handles_params_and_class_lines():
    assert nodeid_matches("test_a.py::test_p", "test_a.py::test_p[1]")
    assert nodeid_matches("test_a.py::T::m", "test_a.py::T::m")
    assert not nodeid_matches("test_a.py::test_p", "test_a.py::test_q")
