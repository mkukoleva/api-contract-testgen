"""Host-side pytest compatibility checks and the two-phase worker contract.

No Docker, LLM credentials or network required. The host precheck (ast.parse,
no code execution) must agree with the in-isolation collect-only phase about
what breaks a suite, and correct suites must pass through unchanged.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from prototype.service_tools.runner.compat import precheck_syntax
from prototype.service_tools.runner.contracts import (
    CompatibilityIssue,
    IssueCategory,
    RunConfig,
    RunResult,
)

RUNNER_DIR = Path(__file__).resolve().parents[1] / "service_tools" / "runner"


def write(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def backend():
    from prototype.service_tools.runner import docker_runner
    return docker_runner


# -------------------------------------------------------- host precheck


@pytest.mark.parametrize("source", [
    "def test_ok(): assert True\n",
    "import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n): assert n > 0\n",
])
def test_precheck_accepts_valid_python(tmp_path, source):
    suite = tmp_path / "suite"
    write(suite / "test_ok.py", source)
    write(suite / "conftest.py", "import pytest\n")
    write(suite / "helpers.py", "def helper(): return 42\n")
    assert precheck_syntax(suite) == ()


def test_precheck_reports_exact_syntax_location(tmp_path):
    suite = tmp_path / "suite"
    write(suite / "test_broken.py", "def test_ok(:\n    pass\n")
    issues = precheck_syntax(suite)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == IssueCategory.SYNTAX
    assert issue.path.endswith("test_broken.py")
    assert issue.line == 1
    assert issue.column is not None
    assert issue.auto_fixable is False
    assert "invalid syntax" in issue.message
    assert issue.to_dict()["category"] == "syntax"


def test_precheck_catches_broken_helper_module(tmp_path):
    suite = tmp_path / "suite"
    write(suite / "test_ok.py", "def test_ok(): pass\n")
    # The test file is fine but the imported helper is truncated.
    write(suite / "helper.py", "value = 1\nx = (\n")
    issues = precheck_syntax(suite)
    assert len(issues) == 1
    assert issues[0].path.endswith("helper.py")
    assert "never closed" in issues[0].message.lower()
    assert issues[0].auto_fixable is True


def test_precheck_marks_unterminated_string_as_auto_fixable(tmp_path):
    suite = tmp_path / "suite"
    write(suite / "test_bad.py", 'def test_bad():\n    s = "unterminated\n')
    issues = precheck_syntax(suite)
    assert len(issues) == 1
    assert issues[0].category == IssueCategory.SYNTAX
    assert "unterminated" in issues[0].message.lower()
    assert issues[0].auto_fixable is True


def test_precheck_reports_non_utf8_file(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_latin.py").write_bytes(
        b"def test_ok():\n    # \xe9\n    assert True\n")
    issues = precheck_syntax(suite)
    assert len(issues) == 1
    assert issues[0].path.endswith("test_latin.py")
    assert "UTF-8" in issues[0].message
    assert issues[0].auto_fixable is False


def test_precheck_requires_directory(tmp_path):
    with pytest.raises(ValueError, match="directory"):
        precheck_syntax(tmp_path / "missing")


def test_run_tests_rejects_syntax_broken_suite_before_docker(tmp_path, monkeypatch):
    from prototype.service_tools.runner import docker_runner
    suite = tmp_path / "suite"
    write(suite / "test_broken.py", "def test_ok(:\n")
    monkeypatch.setattr(
        docker_runner.subprocess, "run",
        lambda *a, **k: pytest.fail("docker must not be called for a broken suite"))
    result = docker_runner.run_tests(RunConfig(suite, tmp_path / "out"))
    assert result.status == "collection_error"
    assert result.exit_code is None
    assert len(result.compatibility_issues) == 1
    issue = result.compatibility_issues[0]
    assert issue.path.endswith("test_broken.py")
    assert result.collection_errors
    assert "test_broken.py" in result.collection_errors[0]
    precheck = Path(result.report_paths["precheck"])
    assert precheck.is_file()
    payload = json.loads(precheck.read_text(encoding="utf-8"))
    assert payload[0]["category"] == "syntax"
    assert payload[0]["auto_fixable"] is False
    # No container artifacts were produced.
    assert "collect_events" not in result.report_paths
    assert "events" not in result.report_paths


def test_valid_suite_passes_precheck_and_reaches_docker(tmp_path, monkeypatch):
    from prototype.service_tools.runner import docker_runner
    suite = tmp_path / "suite"
    write(suite / "test_ok.py", "def test_ok(): assert 1 + 1 == 2\n")
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if "create" in args:
            return SimpleNamespace(returncode=42, stdout="", stderr="create failed")
        if "rm" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        pytest.fail(f"unexpected subprocess call: {args}")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)
    result = docker_runner.run_tests(RunConfig(suite, tmp_path / "out"))
    assert result.status == "infrastructure_error"
    assert "Docker create failed" in result.error_message
    assert calls, "docker create must be attempted for a valid suite"
    assert result.compatibility_issues == ()


# -------------------------------------------------------- issue contract


def test_compatibility_issue_validation():
    with pytest.raises(ValueError, match="IssueCategory"):
        CompatibilityIssue("nonsense", "test.py", "msg")
    with pytest.raises(ValueError, match="path"):
        CompatibilityIssue(IssueCategory.SYNTAX, " ", "msg")
    with pytest.raises(ValueError, match="message"):
        CompatibilityIssue(IssueCategory.SYNTAX, "test.py", "")
    with pytest.raises(ValueError, match="line"):
        CompatibilityIssue(IssueCategory.SYNTAX, "test.py", "msg", line=-1)
    with pytest.raises(ValueError, match="column"):
        CompatibilityIssue(IssueCategory.SYNTAX, "test.py", "msg", column="x")
    with pytest.raises(ValueError, match="auto_fixable"):
        CompatibilityIssue(IssueCategory.SYNTAX, "test.py", "msg", auto_fixable=1)


def test_run_result_serializes_compatibility_issues():
    issue = CompatibilityIssue(
        IssueCategory.SYNTAX, "test_bad.py", "unexpected EOF while parsing",
        line=2, column=5, auto_fixable=True)
    result = RunResult("collection_error", None, 0.5,
                       collection_errors=("test_bad.py: unexpected EOF while parsing",),
                       compatibility_issues=(issue,))
    payload = result.to_dict()["compatibility_issues"]
    assert payload == [{
        "category": "syntax", "path": "test_bad.py", "line": 2, "column": 5,
        "message": "unexpected EOF while parsing", "auto_fixable": True,
    }]
    with pytest.raises(ValueError, match="compatibility_issues"):
        RunResult("collection_error", None, 0.5, compatibility_issues=("not an issue",))


# ------------------------------------------------ two-phase worker (host)


def _run_two_phase(tmp_path, files):
    suite = tmp_path / "suite"
    suite.mkdir()
    for name, source in files.items():
        (suite / name).write_text(textwrap.dedent(source), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    collect_events = tmp_path / "collect_events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"),
         str(suite), str(events), str(collect_events)],
        capture_output=True, text=True, timeout=15,
    )
    return proc, suite, events, collect_events


def _collect_or_run(tmp_path):
    events = tmp_path / "events.jsonl"
    collect_events = tmp_path / "collect_events.jsonl"
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    return collect_result if not collect_ok else backend().read_result(events, 0.1)


def test_correct_suite_runs_unchanged_two_phase(tmp_path):
    proc, suite, events, collect_events = _run_two_phase(tmp_path, {
        "test_ok.py": "def test_ok(): assert 2 + 2 == 4\n",
    })
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = _collect_or_run(tmp_path)
    assert result.status == "completed"
    assert result.to_dict()["summary"] == {
        "total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0}
    assert collect_events.is_file()


def test_import_error_stops_before_any_body_runs(tmp_path):
    marker = tmp_path / "bodies-ran.txt"
    write_ = f"def test_marker():\n    open({str(marker)!r}, 'w').close()\n"
    proc, suite, events, collect_events = _run_two_phase(tmp_path, {
        "test_a.py": "import missing_collect_control_module\n",
        "test_b.py": write_,
    })
    assert proc.returncode != 0
    result = _collect_or_run(tmp_path)
    assert result.status == "collection_error"
    assert result.collection_errors
    # The single-phase run event file must not exist: collection stopped the
    # worker before test bodies could execute.
    assert not events.exists()
    assert not marker.exists(), "test bodies must never run after a collection error"


def test_empty_suite_is_no_tests_at_collection(tmp_path):
    proc, suite, events, collect_events = _run_two_phase(tmp_path, {
        "not_a_test.py": "def helper(): return 1\n",
    })
    assert proc.returncode == 5
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    assert not collect_ok
    assert collect_result.status == "no_tests"
    assert collect_result.exit_code == 5
    assert not events.exists()


def test_import_crash_during_collection_is_infrastructure_error(tmp_path):
    proc, suite, events, collect_events = _run_two_phase(tmp_path, {
        "test_bad.py": "import os\nos._exit(0)\n",
    })
    assert proc.returncode != 0
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    assert not collect_ok
    assert collect_result.status == "infrastructure_error"
    assert collect_result.exit_code is None
    assert not events.exists()


@pytest.mark.parametrize("files, expected_status", [
    ({"test_stop.py": "import pytest\npytest.exit('stop collection', returncode=0)\n"},
     "collection_error"),
    ({"conftest.py": "import pytest\ndef pytest_sessionstart(session):\n"
                     "    pytest.exit('stop collection', returncode=0)\n",
      "test_ok.py": "def test_ok(): pass\n"}, "interrupted"),
])
def test_zero_exit_interruption_during_collection_blocks_execution(tmp_path, files, expected_status):
    proc, suite, events, collect_events = _run_two_phase(tmp_path, files)
    assert proc.returncode != 0
    collect_ok, result = backend().read_collection(collect_events, 0.1)
    assert not collect_ok
    assert result.status == expected_status
    assert not events.exists()


# ----------------------------------------- five-arg service worker (host)


def _run_two_phase_service(tmp_path, policy_pass):
    policy = tmp_path / "policy"
    policy.mkdir()
    body = "def test_ok(): pass" if policy_pass else "def test_leak(): raise AssertionError('isolation leak')"
    (policy / "test_isolation.py").write_text(body, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_user.py").write_text(
        "def test_user(): assert True\n", encoding="utf-8")
    policy_events = tmp_path / "policy_events.jsonl"
    events = tmp_path / "events.jsonl"
    collect_events = tmp_path / "collect_events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"),
         str(policy), str(tests), str(policy_events), str(events),
         str(collect_events)],
        capture_output=True, text=True, timeout=15,
    )
    return proc, policy_events, events, collect_events


def test_service_worker_collects_then_runs_after_policy(tmp_path):
    proc, policy_events, events, collect_events = _run_two_phase_service(tmp_path, True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert collect_events.is_file()
    assert events.is_file()
    result = _collect_or_run(tmp_path)
    assert result.status == "completed"


def test_service_worker_skips_collect_and_run_when_policy_fails(tmp_path):
    proc, policy_events, events, collect_events = _run_two_phase_service(tmp_path, False)
    assert proc.returncode != 0
    assert not events.exists(), "user events must not exist after a policy failure"
    assert not collect_events.exists(), "collection must not run when isolation is unproven"


@pytest.mark.parametrize("policy_source", [
    "import pytest\n@pytest.mark.skip(reason='unavailable')\ndef test_policy(): pass",
    "import pytest\n@pytest.mark.xfail(reason='unavailable')\ndef test_policy(): assert False",
    "import pytest\ndef test_policy(): pytest.exit('aborted', returncode=0)",
])
def test_unproven_policy_blocks_even_imports(tmp_path, policy_source):
    policy = tmp_path / "policy"
    suite = tmp_path / "suite"
    marker = tmp_path / "imported"
    write(policy / "test_policy.py", policy_source)
    write(suite / "test_user.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef test_ok(): pass")
    proc = subprocess.run([
        sys.executable, "-I", "-B", str(RUNNER_DIR / "pytest_worker.py"),
        str(policy), str(suite), str(tmp_path / "policy.jsonl"),
        str(tmp_path / "events.jsonl"), str(tmp_path / "collect.jsonl"),
    ], capture_output=True, text=True, timeout=15)
    assert not marker.exists(), proc.stdout + proc.stderr
    assert proc.returncode != 0


def test_isolated_worker_loads_trusted_fixtures(tmp_path):
    policy = tmp_path / "policy"
    suite = tmp_path / "suite"
    write(policy / "test_policy.py", "def test_plugin(api_client): assert api_client.trust_env is False")
    write(suite / "test_user.py", "def test_url(base_url): assert base_url == 'http://catalogue:8080'")
    import os
    proc = subprocess.run([
        sys.executable, "-I", "-B", str(RUNNER_DIR / "pytest_worker.py"),
        str(policy), str(suite), str(tmp_path / "policy.jsonl"),
        str(tmp_path / "events.jsonl"), str(tmp_path / "collect.jsonl"),
    ], capture_output=True, text=True, timeout=15,
        env={**os.environ, "RUNNER_BASE_URL": "http://catalogue:8080"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_service_syntax_precheck_precedes_docker(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    write(suite / "test_broken.py", "def test_bad(:")
    monkeypatch.setattr(backend().subprocess, "run",
                        lambda *a, **k: pytest.fail("syntax precheck must not require Docker"))
    result = backend().run_tests(RunConfig(suite, tmp_path / "out", base_url="http://catalogue:8080"),
                                 network="runner-net")
    assert result.status == "collection_error"
    assert result.compatibility_issues


@pytest.mark.parametrize("contents", [
    b'\xef\xbb\xbfdef test_ok(): pass\n',
    b'# coding: latin-1\ndef test_ok(): assert "\xe9"\n',
])
def test_precheck_accepts_python_source_encodings(tmp_path, contents):
    (tmp_path / "test_valid.py").write_bytes(contents)
    assert precheck_syntax(tmp_path) == ()


def test_policy_and_user_modules_may_have_the_same_name(tmp_path):
    policy = tmp_path / "policy"
    suite = tmp_path / "suite"
    write(policy / "test_same.py", "def test_policy(): pass")
    write(suite / "test_same.py", "def test_user(): pass")
    proc = subprocess.run([
        sys.executable, "-I", "-B", str(RUNNER_DIR / "pytest_worker.py"),
        str(policy), str(suite), str(tmp_path / "policy.jsonl"),
        str(tmp_path / "events.jsonl"), str(tmp_path / "collect.jsonl"),
    ], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_non_text_source_encoding_returns_diagnostic(tmp_path):
    (tmp_path / "test_bad_encoding.py").write_bytes(b"# coding: base64_codec\npass\n")
    issues = precheck_syntax(tmp_path)
    assert len(issues) == 1
    assert issues[0].category == IssueCategory.SYNTAX
