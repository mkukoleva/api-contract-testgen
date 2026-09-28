"""Container entry point. Emit pytest lifecycle events, never parse terminal text.

This file is copied into the runner image without the agent or its dependencies.
Only controlled infrastructure tests invoke it directly on the host.
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


def main():
    tests_dir = Path(sys.argv[1]).resolve(strict=True)
    events_path = Path(sys.argv[2]).resolve()
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        os.environ.pop(name, None)
    import pytest

    os.chdir(tests_dir)
    with events_path.open("w", encoding="utf-8") as stream:
        reporter = EventReporter(stream)
        reporter.emit("start", version=1)
        code = int(pytest.main([
            str(tests_dir), "-q", "-c", os.devnull,
            "--rootdir", str(tests_dir), "--confcutdir", str(tests_dir),
            "-p", "no:cacheprovider", "--tb=short",
        ], plugins=[reporter]))
        reporter.emit("finish", exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
