#!/usr/bin/env python3
"""One command: environment check, demo stand, readiness, tests, reports, cleanup.

Runs from the repository root:

    uv run --project prototype --locked --no-sync python tools/verify.py [--offline] [...]

The script uses only the Python standard library. It never calls the LLM,
never edits project files and never installs anything at run time: the
environment must already be prepared (one-time `uv sync --locked` in
prototype/ and one-time build of the runner image), otherwise it fails with a
diagnostic instead of doing the work automatically.

Full mode (default):  env-check -> stand up -> readiness -> pytest (unit +
integration flags) -> runner offline example -> runner saved-snapshot suite
(service mode) -> summary report -> teardown of the stand and temporary files.
Offline mode (--offline):  env-check -> pytest (unit, no flags) -> runner
offline example (unless --skip-runner-set) -> summary report. No stand.

Exit code is 0 only when every step (including cleanup) succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE_DIR = ROOT / "prototype"
COMPOSE_FILE = ROOT / "benchmark" / "catalogue" / "compose.yaml"
CHECK_READY = ROOT / "benchmark" / "catalogue" / "check_ready.py"
OFFLINE_EXAMPLE = ROOT / "benchmark" / "pytest-runner" / "example"
DEFAULT_SNAPSHOT = ROOT / "benchmark" / "pytest-runner" / "saved-sets" / "catalogue-2026-10-05"
RUNNER_DOCKERFILE = PROTOTYPE_DIR / "src" / "prototype" / "service_tools" / "runner"

RUNNER_IMAGE = "api-contract-pytest-runner:step5"
RUNNER_NETWORK = "pytest-runner-catalogue_runner"
BLOCKED_NETWORK = "pytest-runner-catalogue_database"
API_SERVICE_URL = "http://catalogue:8080"

PYTHON_TARGET = "3.14.7"
TARGET_VERSION = tuple(int(part) for part in PYTHON_TARGET.split("."))

STEP_OK, STEP_FAIL, STEP_SKIP = "ok", "fail", "skip"


class Failure(Exception):
    """A step failed; the message is safe to print."""


def run(cmd: list[Any], *, cwd: Path = ROOT, env: dict[str, str] | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a command; raise Failure with output on non-zero exit."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            [str(part) for part in cmd],
            cwd=str(cwd),
            env=full_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise Failure(f"timed out after {timeout:g}s: {' '.join(map(str, cmd))}") from exc
    if proc.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (proc.stdout or "", proc.stderr or "") if part.strip()
        )[-6000:]
        raise Failure(
            f"command failed (exit {proc.returncode}): {' '.join(map(str, cmd))}"
            + (f"\n{detail}" if detail else "")
        )
    return proc


def run_quiet(cmd: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess | None:
    """Run a check command without raising; None on timeout."""
    try:
        return subprocess.run(
            [str(part) for part in cmd],
            cwd=str(ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


class Verify:
    """Records steps and writes a per-run summary report."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        now = datetime.now(timezone.utc)
        self.run_id = now.strftime("%Y-%m-%d_%H%M%S") + "_" + uuid4().hex
        self.started_at = now.isoformat()
        report_root = Path(args.report_root)
        if not report_root.is_absolute():
            report_root = ROOT / report_root
        self.report_dir = report_root / self.run_id
        self.report_dir.mkdir(parents=True, exist_ok=False)
        self.steps: list[dict[str, Any]] = []
        self.ok = True

    def step(self, name: str, fn, *, detail_default: str = "") -> str:
        started = time.monotonic()
        detail = detail_default
        try:
            result = fn()
            if result:
                detail = str(result)
            status = STEP_OK
            print(f"  ok     {name}" + (f"   —  {detail}" if detail else ""))
        except Failure as exc:
            status = STEP_FAIL
            self.ok = False
            detail = str(exc)
            print(f"  FAIL   {name}   —  {exc}", file=sys.stderr)
        except Exception as exc:  # unexpected, but still a failed step
            status = STEP_FAIL
            self.ok = False
            detail = repr(exc)
            print(f"  FAIL   {name}   —  unexpected error: {exc!r}", file=sys.stderr)
        self.steps.append({
            "name": name,
            "status": status,
            "detail": detail,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        })
        return status

    def write_summary(self, mode: str) -> dict[str, Any]:
        summary = {
            "run_id": self.run_id,
            "mode": mode,
            "python": PYTHON_TARGET,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "llm": "not used",
            "result": "ok" if self.ok else "failed",
            "report_dir": str(self.report_dir),
            "steps": self.steps,
        }
        tmp = self.report_dir / ".summary.json.tmp"
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.report_dir / "summary.json")
        return summary


# --------------------------------------------------------------------------- steps

def env_check_common() -> str:
    if sys.version_info[:3] != TARGET_VERSION:
        raise Failure(
            f"expected Python {PYTHON_TARGET}, running "
            f"{'.'.join(map(str, sys.version_info[:3]))}; "
            f"project uses {PROTOTYPE_DIR / '.python-version'} — run "
            f"`uv sync --locked` once in prototype/"
        )
    if os.environ.get("DEEPCODE_API_KEY"):
        return "python 3.14.7 ok (DEEPCODE_API_KEY present but tests never call the LLM)"
    return "python 3.14.7 ok"


def env_check_docker() -> str:
    if not shutil.which("docker"):
        raise Failure("docker is not installed or not in PATH")
    probe = run_quiet(["docker", "info"])
    if probe is None or probe.returncode != 0:
        raise Failure("docker daemon is not reachable (`docker info` failed)")
    compose = run_quiet(["docker", "compose", "version"])
    if compose is None or compose.returncode != 0:
        raise Failure("docker compose is not available")
    run(["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"], timeout=60)
    image = run_quiet(["docker", "image", "inspect", RUNNER_IMAGE])
    if image is None or image.returncode != 0:
        raise Failure(
            f"runner image {RUNNER_IMAGE} is missing; build it once with:\n"
            f"  docker build -t {RUNNER_IMAGE} {RUNNER_DOCKERFILE.relative_to(ROOT)}"
        )
    return f"docker ok, image {RUNNER_IMAGE} present"


def stand_up(args: argparse.Namespace) -> str:
    run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d",
         "--wait", "--wait-timeout", str(args.readiness_timeout)],
        timeout=args.readiness_timeout + 120)
    return "compose up --wait ok"


def wait_ready(args: argparse.Namespace) -> str:
    proc = run([sys.executable, str(CHECK_READY), "--timeout", str(args.readiness_timeout)],
               timeout=args.readiness_timeout + 60)
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise Failure(f"readiness probe returned invalid JSON: {proc.stdout.strip()[:500]}") from exc
    if payload.get("status") != "ready":
        raise Failure(f"stand not ready: {payload}")
    return f"ready at {payload['base_url']}, {payload['product_count']} products"


def run_pytest(args: argparse.Namespace, report_dir: Path) -> str:
    junit = report_dir / "junit.xml"
    env: dict[str, str] = {}
    if not args.offline:
        env.update(RUN_RUNNER_DOCKER_TESTS="1", RUN_RUNNER_CATALOGUE_TESTS="1")
    cmd = [sys.executable, "-m", "pytest", "-q", "--junitxml", str(junit)]
    cmd.extend(args.pytest_extra)
    proc = run(cmd, cwd=PROTOTYPE_DIR, env=env or None, timeout=args.pytest_timeout)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no summary line"
    return f"{tail}; junit.xml saved"


def run_runner_set(tests_dir: Path, output_dir: Path, *,
                   service: bool, runner_timeout: float) -> str:
    if not tests_dir.is_dir():
        raise Failure(f"test suite directory not found: {tests_dir.relative_to(ROOT) if tests_dir.is_relative_to(ROOT) else tests_dir}")
    cmd = [sys.executable, "-m", "prototype.service_tools.runner",
           str(tests_dir), "--output-dir", str(output_dir),
           "--timeout", str(runner_timeout)]
    if service:
        cmd += ["--base-url", API_SERVICE_URL,
                "--network", RUNNER_NETWORK,
                "--blocked-network", BLOCKED_NETWORK]
    proc = run(cmd, timeout=runner_timeout + 240)
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise Failure(f"runner printed invalid JSON: {proc.stdout.strip()[:500]}") from exc
    status = payload.get("status")
    exit_code = payload.get("exit_code")
    passed = sum(1 for t in payload.get("tests", []) if t.get("outcome") == "passed")
    if status != "completed" or exit_code != 0:
        raise Failure(f"runner: status={status} exit_code={exit_code} passed={passed}")
    paths = payload.get("report_paths") or {}
    reported = paths.get("json") or paths.get("markdown")
    if reported:
        report_path = output_dir / reported
        if report_path.exists() and report_path.is_relative_to(ROOT):
            reported = str(report_path.relative_to(ROOT))
    return f"completed, exit 0, {passed} passed; report: {reported}"


def resolve_saved_set(args: argparse.Namespace) -> Path:
    candidate = Path(args.saved_set) if args.saved_set else DEFAULT_SNAPSHOT
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    tests = candidate if any(candidate.glob("test_*.py")) else candidate / "tests"
    if not tests.is_dir():
        raise Failure(
            f"saved set not found: expected tests in {tests.relative_to(ROOT)}; "
            f"commit one under benchmark/pytest-runner/saved-sets/"
        )
    return tests


def stand_down() -> str:
    run(["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
        timeout=180)
    return "compose down ok (volumes preserved)"


def clean_temp() -> str:
    cache = PROTOTYPE_DIR / ".pytest_cache"
    if cache.is_symlink() or cache.is_junction():
        raise Failure(f"refusing to remove linked cache: {cache}")
    prototype = PROTOTYPE_DIR.resolve()
    resolved_cache = cache.resolve()
    if resolved_cache == prototype or not resolved_cache.is_relative_to(prototype):
        raise Failure(f"cache resolves outside prototype directory: {resolved_cache}")
    if cache.is_dir():
        shutil.rmtree(resolved_cache)
    return "removed .pytest_cache"


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--offline", action="store_true",
                        help="unit tests without the stand and without integration flags")
    parser.add_argument("--skip-runner-set", action="store_true",
                        help="do not run a saved suite through the runner container")
    parser.add_argument("--keep-stand", action="store_true",
                        help="leave the Catalogue stand running after the run")
    parser.add_argument("--saved-set", default=None, metavar="PATH",
                        help="path to a saved generated suite (dir with tests/); "
                             f"default: {DEFAULT_SNAPSHOT.relative_to(ROOT)}")
    parser.add_argument("--report-root", default=".verify-runs", metavar="DIR",
                        help="directory for per-run reports (default: .verify-runs)")
    parser.add_argument("--readiness-timeout", type=float, default=120,
                        help="seconds to wait for the stand (default: 120)")
    parser.add_argument("--runner-timeout", type=float, default=60,
                        help="runner per-suite timeout in seconds (default: 60)")
    parser.add_argument("--pytest-timeout", type=float, default=1800,
                        help="hard timeout for the pytest step (default: 1800)")
    parser.add_argument("--pytest-extra", action="append", default=[],
                        help="extra argument for the pytest step (repeatable)")
    parser.add_argument("--skip-env-check", action="store_true", help="skip the environment check")
    args = parser.parse_args(argv)

    verify = Verify(args)
    mode = "offline" if args.offline else "full"
    print(f"verify ({mode})  run {verify.run_id}  report {verify.report_dir.relative_to(ROOT)}")

    owns_stand = False

    def start_stand() -> str:
        nonlocal owns_stand
        existing = run(["docker", "compose", "-f", str(COMPOSE_FILE),
                        "ps", "--all", "--quiet"], timeout=60)
        # Mark ownership before up, since a failed up can leave partial resources.
        # Stopped containers also count as an existing user-owned stand.
        owns_stand = not existing.stdout.strip()
        return stand_up(args)

    try:
        # Each phase requires the preceding phase to have succeeded.
        if not args.skip_env_check:
            verify.step("env-check", env_check_common)
            if verify.ok and not args.offline:
                verify.step("env-docker", env_check_docker)

        if verify.ok and not args.offline:
            verify.step("stand-up", start_stand)
            if verify.ok:
                verify.step("readiness", lambda: wait_ready(args))

        if verify.ok:
            verify.step("pytest", lambda: run_pytest(args, verify.report_dir))

        if verify.ok and not args.skip_runner_set:
            example_out = verify.report_dir / "runner-offline-example"
            verify.step("runner-offline-example",
                        lambda: run_runner_set(OFFLINE_EXAMPLE, example_out,
                                               service=False, runner_timeout=args.runner_timeout))
        if verify.ok and not args.skip_runner_set and not args.offline:
            saved_out = verify.report_dir / "runner-saved-suite"
            verify.step("runner-saved-suite",
                        lambda: run_runner_set(resolve_saved_set(args), saved_out,
                                               service=True, runner_timeout=args.runner_timeout))
    finally:
        # Reports and volumes are retained; only this run's stand is torn down.
        if owns_stand and not args.keep_stand:
            verify.step("stand-down", stand_down)
        verify.step("clean-temp", clean_temp)

    summary = verify.write_summary(mode)
    result = "ok" if verify.ok else "failed"
    print(json.dumps({
        "run_id": verify.run_id,
        "mode": mode,
        "result": result,
        "llm": "not used",
        "report": str((Path(summary["report_dir"]) / "summary.json").relative_to(ROOT)),
    }, indent=2, ensure_ascii=False))
    return 0 if verify.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
