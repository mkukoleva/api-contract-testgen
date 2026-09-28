"""Controlled fixtures only. Docker checks opt in via RUN_RUNNER_DOCKER_TESTS=1."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from prototype.runner.contracts import RunConfig


RUNNER_DIR = Path(__file__).resolve().parents[1] / "runner"
DOCKER = pytest.mark.skipif(
    os.environ.get("RUN_RUNNER_DOCKER_TESTS") != "1",
    reason="requires Docker and the prebuilt api-contract-pytest-runner:step3 image",
)


def make_suite(tmp_path, source):
    suite = tmp_path / "suite with spaces"
    suite.mkdir()
    if source:
        (suite / "test_control.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return suite


def backend():
    from prototype.runner import docker_runner
    return docker_runner


def test_runner_imports_without_third_party_packages():
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(RUNNER_DIR.parents[1])!r}); "
         "from prototype.runner.docker_runner import run_tests; "
         "from prototype.runner.__main__ import main; "
         "assert not {'pytest', 'openai', 'langchain'} & sys.modules.keys()"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("source,status,code,counts", [
    ("def test_ok(): assert 2 + 2 == 4", "completed", 0, {"passed": 1}),
    ("def test_bad(): assert 2 + 2 == 5", "completed", 1, {"failed": 1}),
    ("import pytest\n@pytest.mark.skip(reason='control')\ndef test_skip(): pass",
     "completed", 0, {"skipped": 1}),
    ("import missing_runner_control_module", "collection_error", 2, {}),
    ("def test_broken(:", "collection_error", 2, {}),
    ("", "no_tests", 5, {}),
    ("import pytest\n@pytest.fixture\ndef broken(): raise RuntimeError('setup failure')\n"
     "def test_setup(broken): pass", "completed", 1, {"error": 1}),
    ("import pytest\n@pytest.fixture\ndef broken():\n yield\n raise RuntimeError('teardown failure')\n"
     "def test_teardown(broken): assert False, 'call failure'",
     "completed", 1, {"error": 1}),
    ("import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n): assert n > 0",
     "completed", 0, {"passed": 2}),
    ("def test_interrupt(): raise KeyboardInterrupt", "interrupted", 2, {}),
    ("import pytest\ndef test_exit(): pytest.exit('nothing finished', returncode=0)", "interrupted", 0, {}),
])
def test_real_pytest_results(tmp_path, source, status, code, counts):
    # This host subprocess runs only the literal infrastructure fixtures above.
    suite = make_suite(tmp_path, source)
    events = tmp_path / "events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"), str(suite), str(events)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr
    result = backend().read_result(events, 0.1)
    assert result.status == status
    assert result.exit_code == code
    summary = result.to_dict()["summary"]
    assert summary == {"total": sum(counts.values()), **dict.fromkeys(
        ("passed", "failed", "error", "skipped"), 0), **counts}
    assert bool(result.collection_errors) == (status == "collection_error")
    if "teardown failure" in source:
        assert result.tests[0].phase == "teardown"
        assert "call failure" in result.tests[0].message
        assert "teardown failure" in result.tests[0].message


def test_missing_event_file_is_not_success(tmp_path):
    result = backend().read_result(tmp_path / "absent", 0.1)
    assert result.status == "infrastructure_error"
    assert result.exit_code is None


@pytest.mark.parametrize("source", [
    "import os\ndef test_exit(): os._exit(0)",
    "import pytest\n@pytest.fixture\ndef broken():\n yield\n import os; os._exit(0)\n"
    "def test_incomplete_teardown(broken): pass",
])
def test_abrupt_exit_does_not_report_success(tmp_path, source):
    suite = make_suite(tmp_path, source)
    events = tmp_path / "events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"), str(suite), str(events)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    result = backend().read_result(events, 0.1)
    assert result.status == "infrastructure_error"
    assert result.exit_code is None
    assert not result.tests


def test_cli_failure_returns_json_without_llm(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "prototype.runner", str(tmp_path / "missing"),
         "--output-dir", str(tmp_path / "out")],
        capture_output=True, text=True, timeout=15,
    )
    import json
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "infrastructure_error"


def test_docker_unavailable_returns_infrastructure_error(tmp_path, monkeypatch):
    suite = make_suite(tmp_path, "def test_ok(): pass")
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(backend().subprocess, "run", unavailable)
    result = backend().run_tests(RunConfig(suite, tmp_path / "out"))
    assert result.status == "infrastructure_error"
    assert result.exit_code is None
    assert "docker unavailable" in result.error_message


def test_service_mode_refused_until_network_policy_exists(tmp_path):
    result = backend().run_tests(RunConfig(tmp_path, tmp_path / "out", base_url="http://catalogue:8080"))
    assert result.status == "infrastructure_error"
    assert "base_url" in result.error_message


def test_missing_test_directory_is_not_created(tmp_path):
    missing = tmp_path / "missing"
    result = backend().run_tests(RunConfig(missing, tmp_path / "out"))
    assert result.status == "infrastructure_error"
    assert not missing.exists()


@DOCKER
@pytest.mark.parametrize("source,status,code", [
    ("def test_ok(): assert True", "completed", 0),
    ("def test_bad(): assert False", "completed", 1),
    ("import missing_runner_control_module", "collection_error", 2),
    ("", "no_tests", 5),
])
def test_docker_lifecycle(tmp_path, source, status, code):
    suite = make_suite(tmp_path, source)
    result = backend().run_tests(RunConfig(suite, tmp_path / "out", timeout_seconds=30))
    assert result.status == status, result.to_dict()
    assert result.exit_code == code
    assert Path(result.report_paths["log"]).is_file()


@DOCKER
@pytest.mark.parametrize("during_import", [False, True])
def test_docker_timeout_and_cleanup(tmp_path, during_import):
    source = "import time\ntime.sleep(300)" if during_import else """
        import subprocess
        import sys
        import time
        def test_before(): pass
        def test_hang():
            subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
            time.sleep(300)
    """
    suite = make_suite(tmp_path, source)
    result = backend().run_tests(RunConfig(suite, tmp_path / "out", timeout_seconds=5))
    assert result.status == "timeout", result.to_dict()
    assert result.exit_code is None
    assert result.duration_seconds < 20
    assert len(result.tests) == (0 if during_import else 1)
    name = Path(result.report_paths["log"]).parent.name
    containers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert not containers.stdout.strip()


@DOCKER
def test_docker_does_not_inherit_llm_keys_or_host_access(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "runner-control-secret")
    suite = make_suite(tmp_path, """
        import os
        import socket
        import pytest
        def test_environment():
            assert 'OPENAI_API_KEY' not in os.environ
            assert os.getuid() != 0
            with pytest.raises(OSError):
                open('/tests/host-write', 'w')
            with pytest.raises(OSError):
                socket.create_connection(('1.1.1.1', 443), timeout=0.5)
    """)
    result = backend().run_tests(RunConfig(suite, tmp_path / "out", timeout_seconds=30))
    assert result.status == "completed", result.to_dict()
    assert result.exit_code == 0, result.to_dict()
    assert not (suite / "host-write").exists()


@DOCKER
def test_missing_image_is_infrastructure_error(tmp_path):
    suite = make_suite(tmp_path, "def test_ok(): pass")
    result = backend().run_tests(
        RunConfig(suite, tmp_path / "out"), image="api-contract-pytest-runner:nonexistent-control",
    )
    assert result.status == "infrastructure_error"
    assert result.exit_code is None
    assert result.error_message
