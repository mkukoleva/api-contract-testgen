"""Container entry point. Emit pytest lifecycle events, never parse terminal text.

This file is copied into the runner image without the agent or its dependencies.
Only controlled infrastructure tests invoke it directly on the host.

The worker runs three trusted phases inside the same isolation:
  1. (service mode only) the policy suite that proves network isolation;
  2. a pytest --collect-only pass that imports the test modules and records
     exact collection errors without executing any test bodies;
  3. the generated tests, only when collection succeeded.

Arguments:
  offline: TESTS_DIR EVENTS COLLECT_EVENTS
  service: POLICY_DIR TESTS_DIR POLICY_EVENTS EVENTS COLLECT_EVENTS
"""

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


class EventReporter:
    def __init__(self, stream):
        self.stream = stream
        self.calls = set()
        self.teardowns = set()
        self.unproven = False

    def emit(self, kind, **fields):
        self.stream.write(json.dumps({"kind": kind, **fields}, ensure_ascii=True) + "\n")
        self.stream.flush()  # Keep finished tests even if the container is killed.

    def pytest_collectreport(self, report):
        if report.skipped:
            self.unproven = True
        if report.failed:
            self.unproven = True
            self.emit("collection_error", message=str(report.longrepr)[:65536])

    def pytest_keyboard_interrupt(self, excinfo):
        self.unproven = True
        # pytest.exit(returncode=0) is still an interruption, not a full run.
        self.emit("interrupted", message=str(excinfo.value)[:65536])

    def pytest_runtest_logreport(self, report):
        self.unproven |= report.outcome != "passed" or hasattr(report, "wasxfail")
        if report.when == "call":
            self.calls.add(report.nodeid)
        if report.when == "teardown":
            self.teardowns.add(report.nodeid)
        self.emit(
            "phase", nodeid=report.nodeid, phase=report.when,
            outcome=report.outcome, duration_seconds=report.duration,
            message=str(report.longrepr)[:65536] if report.longrepr else None,
        )


def _run_pytest(suite_dir: str, events_path: str, plugins, *, collect_only=False,
                require_all_passed=False) -> int:
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        os.environ.pop(name, None)
    import pytest

    suite_dir = str(Path(suite_dir).resolve(strict=True))
    events_path = str(Path(events_path).resolve())
    os.chdir(suite_dir)
    args = [
        suite_dir, "-q", "-c", os.devnull,
        "--rootdir", suite_dir, "--confcutdir", suite_dir,
        "-p", "no:cacheprovider", "--tb=short",
    ]
    if collect_only:
        # --collect-only imports the modules but never runs test bodies.
        args.append("--collect-only")
    with open(events_path, "w", encoding="utf-8") as stream:
        reporter = EventReporter(stream)
        reporter.emit("start", version=1)
        code = int(pytest.main(args, plugins=[reporter, *plugins]))
        if require_all_passed and (
            reporter.unproven or not reporter.calls or reporter.calls != reporter.teardowns
        ):
            code = 1
        reporter.emit("finish", exit_code=code)
    return code


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) == 5 and args[0] == "--phase":
        _, mode, suite_dir, events, service = args
        if mode not in {"policy", "collect", "run"} or service not in {"yes", "no"}:
            raise ValueError("Invalid phase")
        return _run_pytest(suite_dir, events, _policy_plugins() if service == "yes" else [],
                           collect_only=mode == "collect", require_all_passed=mode == "policy")
    if len(args) == 5:
        policy_dir, tests_dir, policy_events, events_path, collect_events_path = args
        policy_code = _phase("policy", policy_dir, policy_events, service=True)
        if policy_code != 0:
            # The trusted policy suite must pass before generated tests run.
            with open(policy_events, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "policy_blocked",
                                         "exit_code": policy_code}) + "\n")
            return policy_code
        collect_code = _phase("collect", tests_dir, collect_events_path, service=True)
        if collect_code != 0:
            # Collection (import) problems stop the run before any test body.
            return collect_code
        return _phase("run", tests_dir, events_path, service=True)
    if len(args) == 3:
        tests_dir, events_path, collect_events_path = args
        collect_code = _phase("collect", tests_dir, collect_events_path, service=False)
        if collect_code != 0:
            return collect_code
        return _phase("run", tests_dir, events_path, service=False)
    raise SystemExit(
        "expected POLICY_DIR TESTS_DIR POLICY_EVENTS EVENTS COLLECT_EVENTS "
        "or TESTS_DIR EVENTS COLLECT_EVENTS")


def _phase(mode, suite_dir, events, *, service):
    # Fresh interpreters prevent policy module names and collection side
    # effects from contaminating the next phase. All children inherit the
    # same network namespace, firewall and unprivileged credentials.
    process = subprocess.run([
        sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--phase",
        mode, suite_dir, events, "yes" if service else "no",
    ])
    if mode == "policy" and process.returncode == 0:
        try:
            final = json.loads(Path(events).read_text(encoding="utf-8").splitlines()[-1])
            if final != {"kind": "finish", "exit_code": 0}:
                return 3
        except (OSError, ValueError, IndexError):
            return 3
    return process.returncode


def _policy_plugins():
    """Plugins that ship with the image and provide the trusted fixtures."""
    # Load only from the trusted worker directory, even under python -I.
    # The source checkout uses fixtures.py; the image uses runner_fixtures.py.
    path = Path(__file__).resolve().with_name("runner_fixtures.py")
    if not path.is_file():
        path = path.with_name("fixtures.py")
    if not path.is_file():
        raise RuntimeError("Trusted runner fixtures are missing")
    spec = importlib.util.spec_from_file_location("runner_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [module]


if __name__ == "__main__":
    raise SystemExit(main())
