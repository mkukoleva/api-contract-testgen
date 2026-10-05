"""Legacy host collection for explicitly trusted, controlled fixtures only.

Collection imports every test module, so module-level code DOES execute. This is
not a sandbox and not network isolation: that belongs to the runner (ТЗ 2.2.6,
ADR 0002). Normal storage and tool flows skip this collector; generated code
must be collected inside the isolated runner. Test bodies are never run here.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


MAX_ERROR_CHARS = 4000
_HIDDEN_ENV_PREFIXES = ("DEEPCODE_", "OPENAI_", "PYTEST_ADDOPTS")
_ERROR_HEADER = re.compile(r"^_{3,} ERROR collecting (?P<path>.+?) _{3,}$")
_SECTION_BORDER = re.compile(r"^(={3,}|_{3,}|!{3,})")


def _result(status, collected, nodeids, errors, started) -> dict:
    return {
        "status": status,
        "collected": collected,
        "nodeids": nodeids,
        "errors": errors,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }


def skipped_collection() -> dict:
    return {"status": "skipped", "collected": None, "nodeids": [], "errors": [],
            "duration_seconds": 0.0}


def parse_nodeids(stdout: str) -> list[str]:
    """Read `path::name` lines that `pytest --collect-only -q` prints first."""
    nodeids = []
    for line in stdout.splitlines():
        line = line.rstrip()
        if not line or _SECTION_BORDER.match(line):
            break
        if "::" in line:
            nodeids.append(line.replace("\\", "/"))
    return nodeids


def parse_collection_errors(stdout: str) -> list[str]:
    """Return one message per module from the ERRORS section."""
    errors: list[list[str]] = []
    current: list[str] | None = None
    for line in stdout.splitlines():
        header = _ERROR_HEADER.match(line)
        if header:
            current = [f"{header['path']}:"]
            errors.append(current)
        elif current is not None and _SECTION_BORDER.match(line):
            current = None
        elif current is not None:
            current.append(line)
    return ["\n".join(lines).strip()[:MAX_ERROR_CHARS] for lines in errors]


def collect_tests(tests_dir: Path, timeout_seconds: float) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(_HIDDEN_ENV_PREFIXES)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Installed pytest11 plugins (e.g. langsmith) stay out of generated code's process.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # An empty ini outside the version keeps project pytest settings out.
    fd, empty_ini = tempfile.mkstemp(suffix=".ini")
    os.close(fd)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "--rootdir", str(tests_dir),
             "-c", empty_ini, str(tests_dir)],
            cwd=tests_dir.parent, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _result("timeout", None, [],
                       [f"collection exceeded {timeout_seconds} s"], started)
    except OSError as exc:
        return _result("unavailable", None, [], [f"cannot start pytest: {exc}"], started)
    finally:
        os.unlink(empty_ini)

    if completed.returncode != 0 and "No module named pytest" in completed.stderr:
        return _result("unavailable", None, [], [completed.stderr.strip()], started)

    nodeids = parse_nodeids(completed.stdout)
    errors = parse_collection_errors(completed.stdout)
    if completed.returncode == 0 and not errors:
        status = "ok"
    elif completed.returncode == 5 and not errors:
        status = "no_tests"
    else:
        status = "errors"
        if not errors:
            output = (completed.stdout + completed.stderr).strip()
            errors = [(output or f"pytest exited with code {completed.returncode}")[:MAX_ERROR_CHARS]]
    return _result(status, len(nodeids), nodeids, errors, started)
