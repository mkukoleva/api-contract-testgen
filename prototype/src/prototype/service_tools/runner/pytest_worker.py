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
import os
from pathlib import Path
import sys


class EventReporter:
    def __init__(self, stream):
        self.stream = stream

    def emit(self, kind, **fields):
        self.stream.write(json.dumps({"kind": kind, **fields}, ensure_ascii=True) + "\n")
        self.stream.flush()  # Keep finished tests even if the container is killed.

    def pytest_collectreport(self, report):
        if report.failed:
            self.emit("collection_error", message=str(report.longrepr)[:65536])

    def pytest_keyboard_interrupt(self, excinfo):
        # pytest.exit(returncode=0) is still an interruption, not a full run.
        self.emit("interrupted", message=str(excinfo.value)[:65536])

    def pytest_runtest_logreport(self, report):
        self.emit(
            "phase", nodeid=report.nodeid, phase=report.when,
            outcome=report.outcome, duration_seconds=report.duration,
            message=str(report.longrepr)[:65536] if report.longrepr else None,
        )


def _run_pytest(suite_dir: str, events_path: str, plugins, *, collect_only=False) -> int:
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
        reporter.emit("finish", exit_code=code)
    return code


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) == 5:
        policy_dir, tests_dir, policy_events, events_path, collect_events_path = args
        plugins = _policy_plugins()
        policy_code = _run_pytest(policy_dir, policy_events, plugins)
        if policy_code != 0:
            # The trusted policy suite must pass before generated tests run.
            with open(policy_events, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "policy_blocked",
                                         "exit_code": policy_code}) + "\n")
            return policy_code
        collect_code = _run_pytest(tests_dir, collect_events_path,
                                   plugins, collect_only=True)
        if collect_code != 0:
            # Collection (import) problems stop the run before any test body.
            return collect_code
        return _run_pytest(tests_dir, events_path, plugins)
    if len(args) == 3:
        tests_dir, events_path, collect_events_path = args
        collect_code = _run_pytest(tests_dir, collect_events_path,
                                   [], collect_only=True)
        if collect_code != 0:
            return collect_code
        return _run_pytest(tests_dir, events_path, [])
    raise SystemExit(
        "expected POLICY_DIR TESTS_DIR POLICY_EVENTS EVENTS COLLECT_EVENTS "
        "or TESTS_DIR EVENTS COLLECT_EVENTS")


def _policy_plugins():
    """Plugins that ship with the image and provide the trusted fixtures."""
    plugins = []
    try:
        import runner_fixtures
    except ImportError:
        pass  # Host-side control tests run without the runner image.
    else:
        plugins.append(runner_fixtures)
    return plugins


if __name__ == "__main__":
    raise SystemExit(main())
