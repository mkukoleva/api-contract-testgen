"""Controlled fixtures only. Docker checks opt in via RUN_RUNNER_DOCKER_TESTS=1."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from prototype.service_tools.runner.contracts import RunConfig


RUNNER_DIR = Path(__file__).resolve().parents[1] / "service_tools" / "runner"
# Корень с пакетами (родитель prototype/): src
SRC_DIR = Path(__file__).resolve().parents[2]
DOCKER = pytest.mark.skipif(
    os.environ.get("RUN_RUNNER_DOCKER_TESTS") != "1",
    reason="requires Docker and the prebuilt api-contract-pytest-runner:step5 image",
)


def make_suite(tmp_path, source):
    suite = tmp_path / "suite with spaces"
    suite.mkdir()
    if source:
        (suite / "test_control.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return suite


def backend():
    from prototype.service_tools.runner import docker_runner
    return docker_runner


def test_runner_imports_without_third_party_packages():
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(SRC_DIR)!r}); "
         "from prototype.service_tools.runner.docker_runner import run_tests; "
         "from prototype.service_tools.runner.__main__ import main; "
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
    collect_events = tmp_path / "collect_events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"),
         str(suite), str(events), str(collect_events)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    result = collect_result if not collect_ok else backend().read_result(events, 0.1)
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
    collect_events = tmp_path / "collect_events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"),
         str(suite), str(events), str(collect_events)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    result = collect_result if not collect_ok else backend().read_result(events, 0.1)
    assert result.status == "infrastructure_error"
    assert result.exit_code is None
    assert not result.tests


# ---------------- policy-first worker behaviour (service mode, no Docker)


def _make_policy_suite(tmp_path, *, passes):
    policy = tmp_path / "policy"
    policy.mkdir()
    body = "def test_ok(): pass" if passes else "def test_leak(): raise AssertionError('isolation leak')"
    (policy / "test_isolation.py").write_text(body, encoding="utf-8")
    return policy


def _run_worker_5args(tmp_path, policy_pass):
    policy = _make_policy_suite(tmp_path, passes=policy_pass)
    tests = make_suite(tmp_path, "def test_user(): assert True")
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


def test_worker_runs_user_tests_after_passing_policy(tmp_path):
    proc, policy_events, events, collect_events = _run_worker_5args(tmp_path, policy_pass=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    collect_ok, collect_result = backend().read_collection(collect_events, 0.1)
    assert collect_ok, collect_result
    result = backend().read_result(events, 0.1)
    assert result.status == "completed"
    assert result.tests[0].nodeid.endswith("test_user")
    # The policy suites all pass; the user event file carries the tests.
    assert backend().read_policy_result(policy_events)[0] is True


def test_worker_skips_user_tests_when_policy_fails(tmp_path):
    proc, policy_events, events, collect_events = _run_worker_5args(tmp_path, policy_pass=False)
    assert proc.returncode != 0
    # User tests must not even be collected once the isolation leak is found.
    assert not events.exists(), "user events must not exist after a policy failure"
    assert not collect_events.exists(), "collection must not run after a policy failure"
    policy_ok, message = backend().read_policy_result(policy_events)
    assert policy_ok is False
    assert any(sub in message for sub in ("blocked", "not passed"))


def test_worker_rejects_wrong_argument_count(tmp_path):
    suite = make_suite(tmp_path, "def test_ok(): pass")
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER_DIR / "pytest_worker.py"),
         str(suite), str(tmp_path / "a.jsonl")],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode != 0
    assert "expected" in (proc.stdout + proc.stderr).lower()


def test_cli_failure_returns_json_without_llm(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "prototype.service_tools.runner", str(tmp_path / "missing"),
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


def test_service_mode_requires_a_network_name(tmp_path):
    result = backend().run_tests(RunConfig(tmp_path, tmp_path / "out", base_url="http://catalogue:8080"))
    assert result.status == "infrastructure_error"
    assert "network" in result.error_message


def test_service_mode_rejects_bare_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(backend().subprocess, "run",
                        lambda *a, **k: pytest.fail("docker must not be called"))
    config = RunConfig(tmp_path, tmp_path / "out", base_url="http://10.0.0.5:8080")
    result = backend().run_tests(config, network="runner-net")
    assert result.status == "infrastructure_error"
    assert "host name" in result.error_message


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
def test_docker_runs_clean_example_unchanged(tmp_path):
    example = Path(__file__).resolve().parents[4] / "benchmark" / "pytest-runner" / "example"
    if not example.is_dir():
        pytest.skip("benchmark example suite not present")
    result = backend().run_tests(RunConfig(example, tmp_path / "out", timeout_seconds=30))
    assert result.status == "completed", result.to_dict()
    assert result.exit_code == 0
    assert result.to_dict()["summary"]["passed"] == 3
    # The example runs through the two-phase pipeline without manual edits.
    assert Path(result.report_paths["collect_events"]).is_file()


@DOCKER
def test_docker_collect_phase_runs_in_isolation(tmp_path):
    # The import happens during pytest --collect-only, which imports Python
    # modules; writing to the read-only /tests mount must fail right there,
    # before any test body executes, inside the same isolation.
    suite = make_suite(tmp_path, """
        import os
        open('/tests/collect-write', 'w').close()
        def test_never_runs(): pass
    """)
    result = backend().run_tests(RunConfig(suite, tmp_path / "out", timeout_seconds=30))
    assert result.status == "collection_error", result.to_dict()
    assert result.collection_errors
    assert not result.tests, "test bodies must not run after a collection error"
    assert not (suite / "collect-write").exists()
    assert Path(result.report_paths["collect_events"]).is_file()
    # The single-phase run event file never appears: collection stopped the
    # run before any test phase began.
    assert "events" not in result.report_paths


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


# ------------------------------------------------- service mode unit checks

def inspect_spec(containers, gateway="172.28.0.1"):
    return [{
        "Name": "pytest-runner-catalogue_runner",
        "IPAM": {"Config": [{"Subnet": "172.28.0.0/16", "Gateway": gateway}]},
        "Containers": {
            container_id: {"Name": name, "IPv4Address": ip + "/16"}
            for container_id, (name, ip) in enumerate(containers)
        },
    }]


def test_service_target_returns_single_container_ip_and_gateway():
    spec = inspect_spec([("catalogue", "172.28.0.5")])
    assert backend()._service_target(spec) == ("172.28.0.5", "172.28.0.1")


def test_service_target_rejects_empty_or_multi_container_network():
    with pytest.raises(RuntimeError, match="exactly one"):
        backend()._service_target(inspect_spec([]))
    with pytest.raises(RuntimeError, match="exactly one"):
        backend()._service_target(inspect_spec([("catalogue", "172.28.0.5"),
                                                ("stray", "172.28.0.6")]))


def test_service_target_rejects_missing_metadata():
    with pytest.raises(RuntimeError, match="IPv4"):
        backend()._service_target(inspect_spec([("catalogue", "")], gateway=""))
    with pytest.raises(RuntimeError, match="gateway"):
        backend()._service_target([{"Name": "net", "Containers": {
            "c1": {"Name": "catalogue", "IPv4Address": "172.28.0.5/16"},
        }}])


def test_collect_unreachable_flattens_blocked_networks():
    blocked = [
        [{"Containers": {
            "db": {"Name": "catalogue-db", "IPv4Address": "172.29.0.9/16"},
        }}],
        [{"Containers": {
            "x": {"Name": "other", "IPv4Address": "172.30.0.4/16"},
            "y": {"Name": "again", "IPv4Address": "172.30.0.4/16"},  # duplicate
        }}],
    ]
    assert backend()._collect_unreachable(blocked) == ["172.29.0.9", "172.30.0.4"]


def test_service_hostname_validation():
    assert backend()._service_hostname("http://catalogue:8080") == "catalogue"
    with pytest.raises(ValueError, match="host name"):
        backend()._service_hostname("http://10.0.0.5:8080")
    with pytest.raises(ValueError, match="--add-host"):
        backend()._service_hostname("http://ca%talogue:8080")
    with pytest.raises(ValueError, match="--add-host"):
        backend()._service_hostname("http://ca talogue:8080")


def test_docker_create_args_offline_vs_service(tmp_path, monkeypatch):
    from prototype.service_tools.runner import docker_runner
    monkeypatch.setattr(docker_runner, "POLICY_DIR",
                        "/opt/runner/policy_tests")

    config = RunConfig(tmp_path / "tests", tmp_path / "out")
    offline = docker_runner._docker_create_args(
        config=config, image="img", name="n1", network=None,
        target_ip=None, service_hostname=None, gateway=None,
        unreachable=[], tests_dir=Path("/tests"), run_dir=Path("/results"))
    assert "--network" in offline
    assert offline[offline.index("--network") + 1] == "none"
    assert "--env" not in offline
    assert offline[-4:] == ["img", "/tests", "/results/events.jsonl",
                            "/results/collect_events.jsonl"]

    service = docker_runner._docker_create_args(
        config=RunConfig(tmp_path / "tests", tmp_path / "out",
                         base_url="http://catalogue:8080"),
        image="img", name="n2", network="pytest-runner-catalogue_runner",
        target_ip="172.28.0.5", service_hostname="catalogue",
        gateway="172.28.0.1", unreachable=["172.28.0.1", "172.29.0.9"],
        tests_dir=Path("/tests"), run_dir=Path("/results"))
    assert "--network" in service
    assert service[service.index("--network") + 1] == "pytest-runner-catalogue_runner"
    assert service[service.index("--add-host") + 1] == "catalogue:172.28.0.5"
    assert "RUNNER_BASE_URL=http://catalogue:8080" in service
    assert "RUNNER_API_IP=172.28.0.5" in service
    assert "RUNNER_UNREACHABLE=172.28.0.1,172.29.0.9" in service
    # gateway goes first into RUNNER_UNREACHABLE at runtime, but the helper
    # receives the final list; assert the policy fixtures become identical.
    assert service[-6:] == ["img", "/opt/runner/policy_tests", "/tests",
                            "/results/policy_events.jsonl", "/results/events.jsonl",
                            "/results/collect_events.jsonl"]


def test_service_mode_without_docker_returns_infrastructure_error(tmp_path, monkeypatch):
    suite = make_suite(tmp_path, "def test_ok(): pass")
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(backend().subprocess, "run", unavailable)
    result = backend().run_tests(
        RunConfig(suite, tmp_path / "out", base_url="http://catalogue:8080"),
        network="pytest-runner-catalogue_runner",
    )
    assert result.status == "infrastructure_error"
    assert result.exit_code is None


# ------------------------------------------------- policy result reduction

def make_policy_events(path, *, omitted=None, outcomes=None, exit_code=0, blocked=False):
    lines = [{"kind": "start", "version": 1}]
    for nodeid, outcome in (outcomes or {"test_isol.py::test_ok": "passed"}).items():
        for phase in ("setup", "call", "teardown"):
            lines.append({"kind": "phase", "nodeid": nodeid, "phase": phase,
                          "outcome": outcome if phase == "call" else "passed",
                          "duration_seconds": 0.0,
                          "message": None if outcome == "passed" else f"{nodeid} failed"})
    if not omitted:
        lines.append({"kind": "finish", "exit_code": exit_code})
    if blocked:
        lines.append({"kind": "policy_blocked", "exit_code": exit_code})
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n",
                    encoding="utf-8")


def test_policy_result_passes_only_when_all_green(tmp_path):
    ok, message = backend().read_policy_result(tmp_path / "absent")
    assert ok is False
    assert message.startswith("Cannot read policy events")

    events = tmp_path / "ok.jsonl"
    make_policy_events(events)
    ok, message = backend().read_policy_result(events)
    assert ok is True
    assert message == ""

    failed = tmp_path / "failed.jsonl"
    make_policy_events(failed, outcomes={
        "test_isol.py::test_a": "passed", "test_isol.py::test_b": "failed"})
    ok, message = backend().read_policy_result(failed)
    assert ok is False
    assert "not passed" in message

    skipped = tmp_path / "skipped.jsonl"
    make_policy_events(skipped, outcomes={"test_isol.py::test_a": "skipped"})
    ok, message = backend().read_policy_result(skipped)
    assert ok is False
    assert "not passed" in message


def test_policy_result_rejects_no_finish_and_blocked_and_errors(tmp_path):
    unfinished = tmp_path / "unfinished.jsonl"
    make_policy_events(unfinished, omitted=True)
    ok, message = backend().read_policy_result(unfinished)
    assert ok is False
    assert "did not finish" in message

    blocked = tmp_path / "blocked.jsonl"
    make_policy_events(blocked, blocked=True)
    ok, message = backend().read_policy_result(blocked)
    assert ok is False
    assert "policy_blocked" in message or "blocked" in message

    nonzero = tmp_path / "nonzero.jsonl"
    make_policy_events(nonzero, exit_code=5)
    ok, message = backend().read_policy_result(nonzero)
    assert ok is False
    assert "exit code 5" in message
