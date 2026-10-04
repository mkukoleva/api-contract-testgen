"""Isolated Docker runner. Does not import the agent or call an LLM.

Service mode pins the target in /etc/hosts and uses a trusted bootstrap to
allow only the API TCP address:port with an in-container kernel firewall.
Privileges are irrevocably dropped before policy checks and test imports.

Step 5 adds the automatic pytest compatibility check. A host-side syntax
precheck (ast.parse, no execution, no Docker) is a hard gate. Inside the
container the worker runs a pytest --collect-only pass first: it imports the
Python modules, so it must run in the same isolation, and any collection
error stops the run before a single test body executes.
"""

from dataclasses import replace
import ipaddress
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .compat import precheck_syntax
from .contracts import (
    CompatibilityIssue,
    RunConfig,
    RunResult,
    RunStatus,
    TestResult,
)


DEFAULT_IMAGE = "api-contract-pytest-runner:step5"
CONTROL_TIMEOUT = 10
MAX_EVENTS_BYTES = 8 * 1024 * 1024

# Inside the container these paths are fixed by the image and the mounts.
POLICY_DIR = "/opt/runner/policy_tests"
TESTS_DIR = "/tests"
EVENTS_PATH = "/results/events.jsonl"
POLICY_EVENTS_PATH = "/results/policy_events.jsonl"
COLLECT_EVENTS_PATH = "/results/collect_events.jsonl"

_ALLOWED_HOSTNAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


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


def read_collection(collect_events_path: Path, duration_seconds: float) -> tuple[bool, RunResult | None]:
    """Inspect the in-isolation collect-only phase. Returns (ok, failure_result).

    A successful collection returns (True, None); the actual test run is read
    separately. Any collection error, interruption, zero collected tests, or a
    missing finish event is a failure whose RunResult carries the exact cause.
    This phase imports Python modules and must therefore already have run
    inside the same isolation as the tests themselves.
    """
    collection_errors = []
    exit_code = None
    interruption = None
    try:
        if collect_events_path.stat().st_size > MAX_EVENTS_BYTES:
            raise ValueError("collect event file exceeds 8 MiB")
        with collect_events_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # A forced kill can interrupt the last write.
                event = json.loads(line)
                kind = event["kind"]
                if kind == "collection_error":
                    collection_errors.append(event["message"])
                elif kind == "interrupted":
                    interruption = event["message"] or "pytest collection interrupted"
                elif kind == "finish":
                    exit_code = event["exit_code"]
                    if type(exit_code) is not int or exit_code not in range(6):
                        raise ValueError("invalid pytest exit code")
                elif kind != "start":
                    raise ValueError("unknown collect event")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, RunResult("infrastructure_error", None, duration_seconds,
                                error_message=f"Cannot read collect events: {exc}")
    if exit_code is None:
        return False, RunResult("infrastructure_error", None, duration_seconds,
                                error_message="pytest collection did not finish")
    if collection_errors:
        return False, RunResult("collection_error", exit_code, duration_seconds,
                                collection_errors=tuple(collection_errors))
    if interruption:
        return False, RunResult("interrupted", exit_code, duration_seconds,
                                error_message=interruption)
    if exit_code == 5:
        return False, RunResult("no_tests", exit_code, duration_seconds)
    if exit_code != 0:
        return False, RunResult(
            "collection_error", exit_code, duration_seconds,
            collection_errors=(f"pytest collection exited with code {exit_code}",))
    return True, None


def _format_issue(issue: CompatibilityIssue) -> str:
    location = issue.path
    if issue.line is not None:
        location += f":{issue.line}"
        if issue.column is not None:
            location += f":{issue.column}"
    return f"{location}: {issue.message}"


def read_policy_result(policy_events_path: Path) -> tuple[bool, str]:
    """Check the trusted isolation suite. (ok, message).

    The policy suite confirms that only the pinned API address:port is
    reachable from inside the same network policy that will run the generated
    tests. Any skipped/failed/error result, a collection error, a missing
    finish event, or the absence of events means isolation was not proven.
    """
    try:
        if policy_events_path.stat().st_size > MAX_EVENTS_BYTES:
            raise ValueError("policy event file exceeds 8 MiB")
        with policy_events_path.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.endswith("\n")]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Cannot read policy events: {exc}"
    if not events:
        return False, "policy suite produced no events"
    blocked = next((e for e in events if e.get("kind") == "policy_blocked"), None)
    if blocked is not None:
        return False, f"policy suite blocked the run (exit {blocked.get('exit_code')})"
    collection_errors = [e["message"] for e in events if e.get("kind") == "collection_error"]
    if collection_errors:
        return False, f"policy collection error: {collection_errors[0]}"
    interruption = next((e.get("message") for e in events
                         if e.get("kind") == "interrupted"), None)
    if interruption:
        return False, f"policy interrupted: {interruption}"
    finish = next((e for e in events if e.get("kind") == "finish"), None)
    if finish is None:
        return False, "policy suite did not finish"
    if finish.get("exit_code") != 0:
        return False, f"policy suite finished with exit code {finish.get('exit_code')}"
    phases = [e for e in events if e.get("kind") == "phase"]
    if not phases:
        return False, "policy suite ran no tests"
    for event in phases:
        if event["outcome"] != "passed":
            return False, f"policy test not passed: {event['nodeid']} ({event['outcome']})"
    return True, ""


def _network_inspect(network: str) -> list[dict]:
    proc = subprocess.run(
        ["docker", "network", "inspect", network],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=CONTROL_TIMEOUT,
    )
    if proc.returncode:
        raise RuntimeError(f"Docker network inspect failed ({network!r}): {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Docker network inspect returned invalid JSON ({network!r})") from exc


def _service_target(network_spec: list[dict]) -> tuple[str, str]:
    """Return (IPv4 address of the only container, gateway IPv4) for the runner network."""
    if len(network_spec) != 1:
        raise RuntimeError(f"runner network inspect must describe one network, got {len(network_spec)}")
    containers = network_spec[0].get("Containers") or {}
    if len(containers) != 1:
        raise RuntimeError(
            f"runner network must contain exactly one container (the API), found {len(containers)}")
    (container_id, info), = containers.items()
    ip = (info.get("IPv4Address") or "").split("/")[0]
    if not ip:
        raise RuntimeError("runner network container has no IPv4 address")
    gateway = None
    for config in network_spec[0].get("IPAM", {}).get("Config", []):
        if config.get("Gateway"):
            gateway = config["Gateway"]
            break
    if not gateway:
        raise RuntimeError("runner network has no IPv4 gateway")
    return ip, gateway


def _collect_unreachable(network_specs: list[list[dict]]) -> list[str]:
    """Collect IPv4 addresses of every container in the blocked networks."""
    addresses = []
    for spec in network_specs:
        for net in spec:
            for info in (net.get("Containers") or {}).values():
                ip = (info.get("IPv4Address") or "").split("/")[0]
                if ip:
                    addresses.append(ip)
    return sorted(set(addresses))


def _service_hostname(base_url: str) -> str:
    hostname = urlsplit(base_url).hostname or ""
    if not hostname:
        raise ValueError("base_url must contain a host name")
    if any(char not in _ALLOWED_HOSTNAME_CHARS for char in hostname):
        raise ValueError("base_url host name contains characters unsafe for --add-host")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    raise ValueError(
        "service mode requires a host name (for example http://catalogue:8080), "
        "not a bare IP; the runner resolves the API through the pinned hosts entry"
    )


def _docker_create_args(
    *,
    config: RunConfig,
    image: str,
    name: str,
    network: str | None,
    target_ip: str | None,
    service_hostname: str | None,
    gateway: str | None,
    unreachable: list[str],
    tests_dir: Path,
    run_dir: Path,
) -> list[str]:
    args = [
        "docker", "create", "--pull=never", "--name", name,
        "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "128", "--memory", "256m", "--cpus", "1",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,source={tests_dir},target=/tests,readonly",
        "--mount", f"type=bind,source={run_dir},target=/results",
    ]
    if network is None:
        args += ["--network", "none", "--user", "65534:65534"]
    else:
        # Only the trusted bootstrap has these capabilities. setpriv clears
        # every capability set before exec'ing the worker, including bounding.
        args += ["--network", network, "--user", "0:0"]
        for capability in ("NET_ADMIN", "SETUID", "SETGID", "SETPCAP"):
            args += ["--cap-add", capability]
        args += ["--add-host", f"{service_hostname}:{target_ip}"]
        args += [
            "--env", f"RUNNER_BASE_URL={config.base_url}",
            "--env", f"RUNNER_API_IP={target_ip}",
            "--env", f"RUNNER_GATEWAY_IP={gateway}",
            "--env", f"RUNNER_UNREACHABLE={','.join(unreachable)}",
            "--env", f"RUNNER_TIMEOUT_SECONDS={config.timeout_seconds:g}",
        ]
    if network is None:
        args += [image, TESTS_DIR, EVENTS_PATH, COLLECT_EVENTS_PATH]
    else:
        args += [image, POLICY_DIR, TESTS_DIR, POLICY_EVENTS_PATH,
                 EVENTS_PATH, COLLECT_EVENTS_PATH]
    return args


def run_tests(
    config: RunConfig,
    *,
    image: str = DEFAULT_IMAGE,
    network: str | None = None,
    blocked_networks: tuple[str, ...] = (),
) -> RunResult:
    """Run a prepared suite in a fresh container; image is chosen by the caller.

    A host-side syntax precheck (ast.parse, no code execution) is a hard gate:
    broken Python stops the run before Docker is even called, with an exact
    path:line:column cause in collection_errors and compatibility_issues.
    Inside the container the worker runs pytest --collect-only first (it
    imports the Python modules, so it must run in the same isolation), then
    the tests; a collection error stops the run before any test body executes.

    base_url=None requests an offline run (--network none). A base_url
    requires the name of the runner network that already contains exactly one
    container (the API); the DNS name is pinned with --add-host and blocked
    networks are inspected to enumerate addresses that must stay unreachable.
    Image building/pulling is a separate explicit operation. No fallback runs
    test code on the host. The deadline covers Docker creation, collection and
    execution; container cleanup can take up to CONTROL_TIMEOUT extra seconds.
    """
    start = time.monotonic()
    name = f"pytest-runner-{uuid4().hex}"
    paths = {}
    attempted_create = False
    override = None
    service_hostname = None
    target_ip = None
    gateway = None
    unreachable: list[str] = []
    service = config.base_url is not None
    error = None
    cleanup_error = None
    events_path = None
    collect_events_path = None
    policy_events_path = None
    try:
        if service:
            if not network:
                raise ValueError(
                    "base_url requires a runner network name (--network), "
                    "e.g. pytest-runner-catalogue_runner; offline runs stay with base_url=None")
            service_hostname = _service_hostname(config.base_url)
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
        policy_events_path = run_dir / "policy_events.jsonl"
        collect_events_path = run_dir / "collect_events.jsonl"
        paths = {"events": events_path, "log": log_path,
                 "policy_events": policy_events_path,
                 "collect_events": collect_events_path}

        # Hard gate: parse every Python file without executing it. A broken
        # file reports an exact cause and Docker is never invoked.
        issues = precheck_syntax(tests_dir)
        if issues:
            precheck_path = run_dir / "precheck.json"
            precheck_path.write_text(
                json.dumps([issue.to_dict() for issue in issues],
                           ensure_ascii=True, allow_nan=False),
                encoding="utf-8")
            paths["precheck"] = precheck_path
            return RunResult(
                RunStatus.COLLECTION_ERROR, None, time.monotonic() - start,
                collection_errors=tuple(_format_issue(issue) for issue in issues),
                compatibility_issues=issues,
                report_paths={key: path for key, path in paths.items()
                              if path.is_file()},
            )

        def remaining():
            return max(0.001, config.timeout_seconds - (time.monotonic() - start))

        if service:
            network_spec = _network_inspect(network)
            target_ip, gateway = _service_target(network_spec)
            unreachable = _collect_unreachable([
                _network_inspect(net) for net in blocked_networks])
            unreachable = sorted(set(unreachable + [gateway]))

        attempted_create = True
        created = subprocess.run(_docker_create_args(
            config=config, image=image, name=name, network=network,
            target_ip=target_ip, service_hostname=service_hostname,
            gateway=gateway, unreachable=unreachable,
            tests_dir=tests_dir, run_dir=run_dir,
        ), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=remaining())
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
    if collect_events_path is not None:
        collect_ok, collect_result = read_collection(collect_events_path, duration)
        result = collect_result if not collect_ok else (
            read_result(events_path, duration) if events_path else RunResult(
                "infrastructure_error", None, duration))
    else:  # pragma: no cover - the host always uses the two-phase worker form
        result = read_result(events_path, duration) if events_path else RunResult(
            "infrastructure_error", None, duration)
    if override:
        result = replace(result, status=override, error_message=error)
    if service and policy_events_path is not None and override is None:
        policy_ok, policy_message = read_policy_result(policy_events_path)
        if not policy_ok:
            # An isolation leak is not a user test failure and is never repaired
            # into a green run; stop here instead of trusting the results.
            result = replace(result, status=RunStatus.INFRASTRUCTURE_ERROR,
                             exit_code=None, error_message=(
                                 "Сетевая изоляция не подтверждена: " + policy_message))
    if cleanup_error:
        result = replace(result,
                         status=result.status if override else RunStatus.INFRASTRUCTURE_ERROR,
                         error_message=f"{result.error_message or ''}\nCannot remove {name}: {cleanup_error}".strip())
    return replace(result, report_paths={key: path for key, path in paths.items() if path.is_file()})
