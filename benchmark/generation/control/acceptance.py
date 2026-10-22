"""Live primary CLI and full regeneration check (4.1.4.1/.2/.4/.5).

Uses two real generations and isolated Docker control services. No compact
override: model options come from the project's usual configuration. This is
an experiment, not a substitute for reviewing assertions or unit generation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "prototype/src"))


def command(*args):
    proc = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=300)
    if proc.returncode:
        raise RuntimeError(f"{args[:3]}: {proc.stderr[-4000:] or proc.stdout[-4000:]}")
    return proc.stdout



def fingerprints(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def write_contracts(folder):
    original = json.loads((HERE / "contract.json").read_text())
    confirm = json.loads(json.dumps(original["paths"]["/quote"]))
    confirm["post"]["operationId"] = "confirmQuote"
    confirm["post"]["summary"] = "Confirm a quote by recalculating its input"
    original["paths"]["/quote/confirm"] = confirm
    changed = json.loads(json.dumps(original))
    changed["info"]["version"] = "2.0.0"
    operation = changed["paths"].pop("/quote")
    changed["paths"]["/quote-v2"] = operation
    changed["paths"]["/quote-v2/confirm"] = changed["paths"].pop("/quote/confirm")
    operation["post"]["description"] += " Currency is required, must be RUB, and is echoed in the response. Quantity is now limited to 5. The previous /quote route is removed."
    changed["paths"]["/quote-v2/confirm"]["post"]["description"] = operation["post"]["description"]
    for name in ("QuoteInput", "Quote"):
        schema = changed["components"]["schemas"][name]
        schema["properties"]["quantity"]["maximum"] = 5
        schema["properties"]["currency"] = {"type": "string", "enum": ["RUB"]}
        schema["required"].append("currency")
    changed["components"]["schemas"]["Quote"]["properties"]["total"]["maximum"] = 5000
    paths = []
    for version, spec in enumerate((original, changed), 1):
        path = folder / f"contract-v{version}.json"
        path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
        paths.append(path)
    return paths


def audit_inputs(spec, events, route=None):
    """Check distinct control-API conditions, including isolated violations.

    Uses server observations of the final saved suite. It cannot prove the
    meaning of generated assertions, which remains a separate review.
    """
    from jsonschema import Draft4Validator
    schema = spec["components"]["schemas"]["QuoteInput"]
    validator = Draft4Validator(schema)
    route = route or next(iter(spec["paths"]))
    events = [event for event in events if event.get("path") == route and event.get("method") == "POST"]
    valid = [event["body"] for event in events
             if event.get("status") == 200 and validator.is_valid(event.get("body"))]
    invalid = [event["body"] for event in events
               if event.get("status") == 422 and not validator.is_valid(event.get("body"))]
    baseline = {"quantity": 1, "unit_price": 10, "note": "", "tags": []}
    if "currency" in schema["properties"]:
        baseline["currency"] = "RUB"

    def isolated(field, condition):
        return any(isinstance(body, dict) and condition(body)
                   and validator.is_valid({**body, field: baseline[field]}) for body in invalid)

    checks = {"valid_request": bool(valid), "missing_body": None in invalid,
              "malformed_json": any(e.get("status") == 400 for e in events)}
    for field, prop in schema["properties"].items():
        if field in schema["required"]:
            checks[f"{field}:omit_required"] = isolated(field, lambda body: field not in body)
        else:
            checks[f"{field}:omit_optional"] = any(field not in body for body in valid)
        type_check = Draft4Validator({"type": prop["type"]})
        checks[f"{field}:invalid_type"] = isolated(field,
            lambda body: field in body and not type_check.is_valid(body[field]))
        for bound in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
            if bound not in prop:
                continue
            value = prop[bound]
            def measure(body):
                current = body.get(field)
                if bound in ("minimum", "maximum"):
                    return current if type(current) in (int, float) else None
                return len(current) if type_check.is_valid(current) else None
            checks[f"{field}:{bound}"] = any(measure(body) == value for body in valid)
            if bound in ("minLength", "minItems") and value == 0:
                continue
            def outside(body):
                measured = measure(body)
                if measured is None:
                    return False
                # Every other constraint on this property must still hold;
                # in particular overlong arrays must have valid enum items.
                remaining = {key: item for key, item in prop.items() if key != bound}
                return (Draft4Validator(remaining).is_valid(body[field])
                        and (measured < value if bound.startswith("min") else measured > value))
            checks[f"{field}:outside_{bound}"] = isolated(field, outside)
    return {"passed": all(checks.values()), "checks": checks,
            "missing": [name for name, passed in checks.items() if not passed],
            "scope": "Actual input conditions; not proof of response assertions or coverage on other APIs."}


def check_response_assertions(network, version, tests, folder, before, expected):
    """Replay unchanged tests against seeded faults; never repair the suite."""
    from prototype.service_tools.runner.contracts import RunConfig
    from prototype.service_tools.runner.docker_runner import run_tests
    rows = []
    for fault in ("wrong_total", "missing_required", "wrong_type", "error_fields_type", "story_goal"):
        fault_network = network + "-" + fault.replace("_", "-")
        container = fault_network + "-api"
        command("docker", "network", "create", "--internal", fault_network)
        created = False
        try:
            command("docker", "create", "--name", container, "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "128m",
                    "--network", fault_network, "--env", f"CONTROL_API_VERSION={version}",
                    "--env", f"CONTROL_API_FAULT={fault}",
                    "--mount", f"type=bind,source={HERE / 'server.py'},target=/srv/server.py,readonly",
                    "--entrypoint", "python", "api-contract-pytest-runner:step5", "-u", "/srv/server.py")
            created = True
            command("docker", "start", container)
            command("docker", "exec", container, "python", "-c",
                    "import urllib.request,time\nfor _ in range(30):\n try:\n  urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=1);break\n except OSError:time.sleep(.2)\nelse:raise RuntimeError('API not ready')")
            result = run_tests(RunConfig(tests, folder / "faults" / fault,
                                        f"http://{container}:8080", 60), network=fault_network)
            failed = [t.nodeid for t in result.tests if t.outcome == "failed" and t.phase == "call"]
            if fault == "story_goal":
                failed = [name for name in failed if name.split("::")[-1].startswith("test_story_")]
            detected = (result.status == "completed" and result.exit_code == 1 and bool(failed)
                        and {t.nodeid for t in result.tests} == expected
                        and all(t.outcome in ("passed", "failed") for t in result.tests))
            rows.append({"fault": fault, "detected": detected, "failed_tests": failed,
                         "report_paths": result.to_dict()["report_paths"]})
            print(f"Response check v{version}: {fault}: {'detected' if detected else 'FAILED'}", flush=True)
        finally:
            try:
                if created:
                    command("docker", "container", "rm", "-f", container)
            finally:
                command("docker", "network", "rm", fault_network)
    recovery = run_tests(RunConfig(tests, folder / "faults" / "recovery",
                                  "http://categories-api:8080", 60), network=network)
    unchanged = fingerprints(tests) == before
    restored = (recovery.status == "completed" and recovery.exit_code == 0 and bool(recovery.tests)
                and {t.nodeid for t in recovery.tests} == expected
                and all(t.outcome == "passed" for t in recovery.tests))
    return {"passed": all(row["detected"] for row in rows) and unchanged and restored,
            "faults": rows, "inputs_unchanged": unchanged, "recovery_passed": restored}


def run_version(folder, version, contract):
    report_root = folder / f"v{version}"
    report_root.mkdir()
    route = "/quote-v2" if version == 2 else "/quote"
    story = {"story": "Calculate a quote and confirm the same price using returned input values.",
        "steps": [{"endpoint": "POST " + route, "expected_status": 200,
                   "expected_result": "Valid nonzero quote with quantity 2, unit_price 7.5; total 15."},
                  {"endpoint": "POST " + route + "/confirm", "expected_status": 200,
                   "expected_result": "Confirmed quote echoes the returned quantity and unit_price.",
                   "bindings": [{"from_step": 0, "pointer": "/quantity", "target": "body:/quantity"},
                                {"from_step": 0, "pointer": "/unit_price", "target": "body:/unit_price"}]}],
        "final_goal": "The confirmation total equals the original total and 15; quantity and unit_price match. Include currency RUB in both requests for version 2."}
    story_path = report_root / "user-story.json"
    story_path.write_text(json.dumps(story, indent=2) + "\n")
    network = "generation-acceptance-" + uuid4().hex[:10]
    container = network + "-api"
    command("docker", "network", "create", "--internal", network)
    created = False
    try:
        command("docker", "create", "--name", container, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "128m",
            "--network", network, "--network-alias", "categories-api", "--env", f"CONTROL_API_VERSION={version}",
            "--mount", f"type=bind,source={HERE / 'server.py'},target=/srv/server.py,readonly",
            "--entrypoint", "python", "api-contract-pytest-runner:step5", "-u", "/srv/server.py")
        created = True
        command("docker", "start", container)
        command("docker", "exec", container, "python", "-c",
            "import urllib.request,time\nfor _ in range(30):\n try:\n  urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=1);break\n except OSError:time.sleep(.2)\nelse:raise RuntimeError('API not ready')")
        args = [sys.executable, "-B", "-c", "from prototype import main; main()", str(contract), "--generate",
                "--base-url", "http://categories-api:8080", "--network", network,
                "--report-dir", str(report_root), "--tests-output-dir", str(folder / "saved"),
                "--max-repair-attempts", "2", "--user-story", str(story_path)]
        # Stream progress so the terminal is not silent during provider calls.
        proc = subprocess.run(args, cwd=ROOT, timeout=900)
        reports = list(report_root.rglob("generation-report.json"))
        if len(reports) != 1:
            raise RuntimeError(f"Version {version}: expected one generation report, got {len(reports)}; inspect {report_root}")
        data = json.loads(reports[0].read_text())
        categories = data["category_evidence"]
        input_audit = {"passed": False, "reason": "Generation or CLI did not succeed"}
        if proc.returncode == 0 and data["status"] == "success" and data["saved_versions"]:
            from prototype.service_tools.runner.contracts import RunConfig
            from prototype.service_tools.runner.docker_runner import run_tests
            tests = Path(data["saved_versions"][-1]["tests_dir"])
            before_files = fingerprints(tests)
            before_log = command("docker", "logs", container)
            print("Проверка входных условий на конечном сохранённом наборе (без LLM)", flush=True)
            final = run_tests(RunConfig(tests, report_root / "final-suite", "http://categories-api:8080", 60), network=network)
            after_log = command("docker", "logs", container)
            if not after_log.startswith(before_log):
                raise RuntimeError("Control server log changed unexpectedly")
            requests = after_log[len(before_log):]
            (report_root / "final-requests.jsonl").write_text(requests)
            spec = json.loads(contract.read_text())
            events = [json.loads(line) for line in requests.splitlines() if line]
            per_operation = {route: audit_inputs(spec, events, route) for route in spec["paths"]}
            input_audit = {"passed": all(row["passed"] for row in per_operation.values()),
                           "operations": per_operation}
            input_audit["inputs_unchanged"] = fingerprints(tests) == before_files
            input_audit["runner_summary"] = final.to_dict()["summary"]
            input_audit["passed"] = (input_audit["passed"] and input_audit["inputs_unchanged"]
                and final.status == "completed" and final.exit_code == 0 and bool(final.tests)
                and all(test.outcome == "passed" for test in final.tests))
            input_audit["response_checks"] = check_response_assertions(
                network, version, tests, report_root, before_files, {t.nodeid for t in final.tests})
            input_audit["passed"] = input_audit["passed"] and input_audit["response_checks"]["passed"]
            (report_root / "input-audit.json").write_text(json.dumps(input_audit, indent=2) + "\n")
        return {"cli_exit_code": proc.returncode, "report": str(reports[0]),
                "pipeline_status": data["status"], "categories": categories,
                "input_audit": input_audit,
                "contract": data["provenance"]["contract"], "saved_versions": data["saved_versions"]}
    finally:
        try:
            if created:
                (report_root / "api-requests.jsonl").write_text(command("docker", "logs", container))
        finally:
            try:
                if created:
                    command("docker", "container", "rm", "-f", container)
            finally:
                command("docker", "network", "rm", network)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="Write both contracts without Docker or LLM")
    parser.add_argument("--output-dir", type=Path, help="New directory for this experiment")
    args = parser.parse_args()
    folder = args.output_dir or ROOT / ".acceptance-runs" / "control" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    print("Отчёты:", folder, flush=True)
    contracts = write_contracts(folder)
    if args.prepare_only:
        print("Контракты подготовлены. Генерация и Docker не запускались.")
        return 0
    summary = {"result": "failed", "versions": [], "scope": "Primary CLI, two operations, categories, response fault checks, user story and full regeneration. Unit generation excluded."}
    try:
        print("1. Проверка Docker и сборка runner", flush=True)
        command("docker", "info", "--format", "{{.ServerVersion}}")
        command("docker", "build", "-t", "api-contract-pytest-runner:step5",
                str(ROOT / "prototype/src/prototype/service_tools/runner"))
        before = None
        for version, contract in enumerate(contracts, 1):
            print(f"{version + 1}. Основной агент: генерация и Docker, контракт v{version}", flush=True)
            result = run_version(folder, version, contract)
            summary["versions"].append(result)
            if result["cli_exit_code"] or result["categories"]["status"] != "observed":
                raise RuntimeError(f"Version {version} did not pass; see its report")
            if not result["input_audit"]["passed"]:
                raise RuntimeError(f"Version {version}: input coverage incomplete; see input-audit.json")
            if version == 1:
                before = fingerprints(folder / "saved")
                first_reports = fingerprints(folder / "v1")
        after = fingerprints(folder / "saved")
        summary["old_versions_unchanged"] = all(after.get(name) == value for name, value in before.items())
        summary["first_reports_unchanged"] = first_reports == fingerprints(folder / "v1")
        one, two = summary["versions"]
        summary["different_contracts"] = one["contract"]["sha256"] != two["contract"]["sha256"]
        summary["new_saved_version"] = one["saved_versions"][-1]["tests_dir"] != two["saved_versions"][-1]["tests_dir"]
        summary["result"] = "passed" if all(summary[key] for key in (
            "old_versions_unchanged", "first_reports_unchanged", "different_contracts", "new_saved_version")) else "failed"
    except Exception as exc:
        from prototype.llm.diagnostics import describe_error
        summary["error"] = describe_error(exc)
    finally:
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
