"""Minimal offline Docker runner. Does not import the agent or call an LLM."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from .contracts import RunConfig, RunResult, RunStatus, TestResult


DEFAULT_IMAGE = "api-contract-pytest-runner:step3"
CONTROL_TIMEOUT = 10
MAX_EVENTS_BYTES = 8 * 1024 * 1024


def read_result(events_path: Path, duration_seconds: float) -> RunResult:
    """Reduce worker events to one result per finished pytest nodeid.

    Event files are diagnostic output, not proof that arbitrary test code is
    honest. Incomplete tests are not turned into passed results on timeout.
    """
    groups = {}
    collection_errors = []
    exit_code = None
    interruption = None
    try:
        if events_path.stat().st_size > MAX_EVENTS_BYTES:
            raise ValueError("worker event file exceeds 8 MiB")
        with events_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # A forced kill can interrupt the last write.
                event = json.loads(line)
                kind = event["kind"]
                if kind == "phase":
                    phase = event["phase"]
                    if phase not in {"setup", "call", "teardown"}:
                        raise ValueError("invalid pytest phase")
                    if event["outcome"] not in {"passed", "failed", "skipped"}:
                        raise ValueError("invalid pytest outcome")
                    groups.setdefault(event["nodeid"], {})[phase] = event
                elif kind == "collection_error":
                    collection_errors.append(event["message"])
                elif kind == "interrupted":
                    interruption = event["message"] or "pytest interrupted"
                elif kind == "finish":
                    exit_code = event["exit_code"]
                    if type(exit_code) is not int or exit_code not in range(6):
                        raise ValueError("invalid pytest exit code")
                elif kind != "start":
                    raise ValueError("unknown worker event")
        tests = []
        for nodeid, phases in groups.items():
            if "teardown" not in phases:
                continue
            ordered = [phases[p] for p in ("setup", "call", "teardown") if p in phases]
            errors = [p for p in ordered if p["phase"] != "call" and p["outcome"] == "failed"]
            failures = [p for p in ordered if p["outcome"] == "failed"]
            skipped = [p for p in ordered if p["outcome"] == "skipped"]
            selected = (errors or failures or skipped or [phases.get("call")])[-1]
            if selected is None:
                continue
            tests.append(TestResult(
                nodeid=nodeid, outcome="error" if errors else selected["outcome"],
                phase=selected["phase"],
                duration_seconds=sum(p["duration_seconds"] for p in ordered),
                message="\n".join(
                    f"{p['phase']}: {p['message']}" for p in ordered if p.get("message")
                ) or None,
            ))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return RunResult("infrastructure_error", None, duration_seconds,
                         error_message=f"Cannot read pytest events: {exc}")
    if exit_code is None:
        status = RunStatus.INFRASTRUCTURE_ERROR
    elif collection_errors:
        status = RunStatus.COLLECTION_ERROR
    elif interruption:
        status = RunStatus.INTERRUPTED
    elif exit_code == 0 and not tests:
        status = RunStatus.NO_TESTS
    else:
        status = {0: RunStatus.COMPLETED, 1: RunStatus.COMPLETED,
                  2: RunStatus.INTERRUPTED, 5: RunStatus.NO_TESTS}.get(
                      exit_code, RunStatus.INFRASTRUCTURE_ERROR)
    error = interruption if status == RunStatus.INTERRUPTED else None
    if status == RunStatus.INFRASTRUCTURE_ERROR:
        error = "pytest did not finish" if exit_code is None else f"pytest internal/usage error ({exit_code})"
    return RunResult(status, exit_code, duration_seconds, tests=tuple(tests),
                     collection_errors=tuple(collection_errors), error_message=error)


def run_tests(config: RunConfig, *, image: str = DEFAULT_IMAGE) -> RunResult:
    """Run a prepared suite in a fresh container; image is chosen by the caller.

    Step 3 supports only base_url=None (--network none). Image building/pulling
    is a separate explicit operation. No fallback runs test code on the host.
    The deadline covers Docker creation, pytest collection and execution;
    container cleanup can take up to CONTROL_TIMEOUT additional seconds.
    """
    start = time.monotonic()
    name = f"pytest-runner-{uuid4().hex}"
    paths = {}
    attempted_create = False
    override = None
    error = None
    cleanup_error = None
    events_path = None
    try:
        if config.base_url is not None:
            raise ValueError("base_url support requires the service network policy (step 4)")
        tests_dir = config.tests_dir.resolve(strict=True)
        output_dir = config.output_dir.resolve()
        if not tests_dir.is_dir():
            raise ValueError("tests_dir must be a directory")
        if output_dir.is_relative_to(tests_dir) or tests_dir.is_relative_to(output_dir):
            raise ValueError("tests_dir and output_dir must be separate, non-nested directories")
        if any(',' in str(p) or '\n' in str(p) or '\r' in str(p) for p in (tests_dir, output_dir)):
            raise ValueError("Docker mount paths cannot contain commas or newlines")
        run_dir = output_dir / name
        run_dir.mkdir(parents=True)
        # The unprivileged container needs write access on Linux as well as Windows.
        run_dir.chmod(0o777)
        events_path = run_dir / "events.jsonl"
        log_path = run_dir / "pytest.log"
        paths = {"events": events_path, "log": log_path}

        def remaining():
            return max(0.001, config.timeout_seconds - (time.monotonic() - start))

        attempted_create = True
        created = subprocess.run([
            "docker", "create", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "65534:65534",
            "--pids-limit", "128", "--memory", "256m", "--cpus", "1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount", f"type=bind,source={tests_dir},target=/tests,readonly",
            "--mount", f"type=bind,source={run_dir},target=/results",
            image,
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=remaining())
        if created.returncode:
            raise RuntimeError(f"Docker create failed: {created.stderr.strip()}")
        with log_path.open("wb") as log:
            attached = subprocess.run(["docker", "start", "--attach", name],
                                      stdout=log, stderr=subprocess.STDOUT, timeout=remaining())
        # start --attach returns the container code, not necessarily pytest's code.
        if attached.returncode not in range(6):
            override = RunStatus.INFRASTRUCTURE_ERROR
            error = f"Container exited with code {attached.returncode}; see pytest.log"
    except subprocess.TimeoutExpired:
        override = RunStatus.TIMEOUT
        error = f"Run exceeded {config.timeout_seconds:g} seconds"
    except KeyboardInterrupt:
        override = RunStatus.INTERRUPTED
        error = "Run interrupted by caller"
    except (OSError, ValueError, RuntimeError) as exc:
        override = RunStatus.INFRASTRUCTURE_ERROR
        error = str(exc)
    finally:
        if attempted_create:
            try:
                removed = subprocess.run(["docker", "rm", "--force", name],
                                         capture_output=True, text=True, timeout=CONTROL_TIMEOUT)
                if removed.returncode and "No such container" not in removed.stderr:
                    cleanup_error = removed.stderr.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_error = str(exc)

    duration = time.monotonic() - start
    result = read_result(events_path, duration) if events_path else RunResult(
        "infrastructure_error", None, duration)
    if override:
        result = replace(result, status=override, error_message=error)
    if cleanup_error:
        result = replace(result,
                         status=result.status if override else RunStatus.INFRASTRUCTURE_ERROR,
                         error_message=f"{result.error_message or ''}\nCannot remove {name}: {cleanup_error}".strip())
    return replace(result, report_paths={key: path for key, path in paths.items() if path.is_file()})
