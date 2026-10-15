"""Adapters to documented Schemathesis HTTP API, EvoMaster CLI and Microcks API."""
import json
import os
import math
from pathlib import Path
import subprocess
import time
from urllib.parse import quote, quote_plus
from uuid import uuid4


def _session():
    import requests
    client = requests.Session()
    client.trust_env = False
    return client


def run_schemathesis(settings):
    with _session() as session:
        response = session.post(settings.schemathesis_url.rstrip("/") + "/test",
            params={"base_url": settings.base_url, "max_time": settings.timeout_seconds},
            data=settings.contract_path.read_bytes(), headers={"Content-Type": "application/yaml"},
            timeout=settings.timeout_seconds + 30, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Schemathesis HTTP {response.status_code}")
        raw = response.json()
    if not isinstance(raw, dict) or raw.get("status") == "error":
        raise RuntimeError("Schemathesis did not produce a completed run")
    counts = raw.get("scenarios", {})
    if not isinstance(counts, dict) or any(type(counts.get(key)) is not int or counts[key] < 0
            for key in ("total", "passed", "failed", "errors", "skipped")):
        raise ValueError("Schemathesis scenario counts are invalid")
    if counts["total"] != sum(counts[key] for key in ("passed", "failed", "errors", "skipped")):
        raise ValueError("Schemathesis scenario counts contradict total")
    return {"backend": "schemathesis", "status": "completed" if counts["total"] else "no_tests", "summary": counts,
            "success": bool(counts["total"]) and not any(counts[key] for key in ("failed", "errors")),
            "failures": raw.get("failures", []), "duration_seconds": raw.get("running_time"), "raw": raw}


def evomaster_command(settings, name):
    return ["docker", "run", "--rm", "--pull=never", "--name", name, "--network", settings.network,
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=768m", "--cpus=1",
        "--mount", f"type=bind,src={settings.contract_path.resolve()},dst=/inputs/contract.json,readonly",
        "--mount", f"type=bind,src={settings.output_dir.resolve()},dst=/generated_tests",
        settings.evomaster_image, "--blackBox", "true", "--schema", "/inputs/contract.json",
        "--base", settings.base_url, "--maxTime", f"{math.ceil(settings.timeout_seconds)}s",
        "--outputFormat", "PYTHON_UNITTEST", "--outputFolder", "/generated_tests",
        "--writeWFCReport", "true", "--writeWFCReportExcludeWebApp", "true",
        "--ratePerMinute", "120", "--createConfigPathIfMissing", "false"]


def run_evomaster(settings):
    from dataclasses import replace
    from ..parser.contract import _load_contract
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    # EvoMaster chooses a parser by extension. A CLI snapshot may contain
    # JSON bytes under .yaml, so provide an explicitly normalized JSON input.
    normalized = settings.output_dir / "evomaster-contract.json"
    normalized.write_text(json.dumps(_load_contract(settings.contract_path), ensure_ascii=False), encoding="utf-8")
    settings = replace(settings, contract_path=normalized)
    name = "api-contract-evomaster-" + uuid4().hex
    started = time.monotonic()
    try:
        process = subprocess.run(evomaster_command(settings, name), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=settings.timeout_seconds + 90)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        return {"backend": "evomaster", "status": "timeout", "duration_seconds": time.monotonic() - started}
    (settings.output_dir / "evomaster.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"EvoMaster exit {process.returncode}; see evomaster.log")
    report = settings.output_dir / "report.json"
    if not report.is_file():
        raise RuntimeError("EvoMaster exited without report.json; search is not confirmed")
    if report.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("EvoMaster report exceeds 16 MiB")
    raw = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("EvoMaster report must be an object")
    faults = raw.get("faults", {})
    if type(raw.get("totalTests")) is not int or raw["totalTests"] < 0:
        raise ValueError("EvoMaster report has no valid totalTests")
    failures = [{"operation": fault.get("operationId"), "title": "EvoMaster found a fault",
                 "categories": fault.get("faultCategories", [])} for fault in faults.get("foundFaults", [])]
    details = raw.get("problemDetails", {})
    rest = details.get("rest", {}) if isinstance(details, dict) else {}
    evidence = rest.get("coveredHttpStatus") if isinstance(rest, dict) else None
    observations = [] if isinstance(evidence, list) else None
    observations_complete = True
    for case in evidence if isinstance(evidence, list) else ():
        if not isinstance(case, dict) or not isinstance(case.get("httpStatus"), list):
            observations_complete = False
            continue
        method, separator, path = str(case.get("endpointId", "")).partition(":")
        if not separator or not path.startswith("/") or method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
            observations_complete = False
            continue
        for status in case.get("httpStatus", []):
            if type(status) is int and 100 <= status <= 599:
                observations.append({"nodeid": case.get("testCaseId", ""), "method": method,
                                     "path": path, "status_code": status})
            else:
                observations_complete = False
    return {"backend": "evomaster", "status": "completed" if raw["totalTests"] else "no_tests", "duration_seconds": time.monotonic() - started,
        "summary": {"generated_files": len(list(settings.output_dir.rglob("*.py"))),
                    "tests": raw.get("totalTests"), "faults": faults.get("totalNumber")},
        "failures": failures, "http_observations": observations, "http_observations_complete": observations_complete, "raw": raw,
        "generated_tests_executed": False}


def run_microcks(settings, *, request_fn=None, sleep_fn=time.sleep):
    from requests.exceptions import Timeout as RequestTimeout
    started = time.monotonic()
    deadline = started + settings.timeout_seconds
    session = _session() if request_fn is None else None
    token = os.getenv("MICROCKS_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else {}
    result = None
    def call(method, path, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Microcks test time limit reached")
        response = (request_fn or session.request)(method, settings.microcks_url.rstrip("/") + path,
            headers=headers, timeout=min(remaining, 15), allow_redirects=False, **kwargs)
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(f"Microcks HTTP {response.status_code}")
        return response
    try:
        service_id = settings.microcks_service_id
        service = None
        if not service_id:
            from ..parser.contract import _load_contract
            schema = _load_contract(settings.contract_path)
            info = schema.get("info", {})
            if not info.get("title") or not info.get("version"):
                raise ValueError("Microcks import requires info.title and info.version, or --microcks-service-id")
            with settings.contract_path.open("rb") as stream:
                call("POST", "/api/artifact/upload", files={"file": (settings.contract_path.name, stream, "application/octet-stream")})
            for page in range(5):
                services = call("GET", "/api/services", params={"page": page, "size": 20}).json()
                if not isinstance(services, list):
                    raise ValueError("Microcks service list is invalid")
                service = next((s for s in services if s.get("name") == info["title"] and s.get("version") == info["version"]), None)
                if service or len(services) < 20:
                    break
            if not service:
                raise RuntimeError("Imported Microcks service not found; provide --microcks-service-id for a large repository")
            service_id = service["id"]
        if settings.intent == "mock":
            service = service or call("GET", "/api/services/" + quote(service_id, safe="")).json()
            result = {"backend": "microcks", "status": "completed", "service_id": service_id,
                "summary": {"operations": len(service.get("operations", []))}, "raw": service}
            if service.get("type") in {"REST", "GENERIC_REST"}:
                result["mock_url"] = settings.microcks_url.rstrip("/") + "/rest/" + quote_plus(service["name"]) + "/" + quote_plus(service["version"])
            return result
        result = call("POST", "/api/tests", json={"serviceId": service_id,
            "testEndpoint": settings.base_url, "runnerType": settings.microcks_runner,
            "timeout": int(settings.timeout_seconds * 1000)}).json()
        test_id = result.get("id")
        if not test_id:
            raise ValueError("Microcks did not return a test id")
        while result.get("inProgress", True):
            sleep_fn(min(.25, max(0, deadline - time.monotonic())))
            result = call("GET", "/api/tests/" + quote(test_id, safe="")).json()
        if type(result.get("success")) is not bool:
            raise ValueError("Microcks completed without a valid success verdict")
        cases = result.get("testCaseResults", [])
        steps = sum(len(case.get("testStepResults", [])) for case in cases)
        failures = [{"operation": case.get("operationName"), "title": "Microcks conformance check failed"}
                    for case in cases if case.get("success") is False]
        return {"backend": "microcks", "status": "completed" if steps else "no_tests", "success": result["success"] if steps else False,
            "summary": {"cases": len(cases), "steps": steps, "passed": sum(c.get("success") is True for c in cases),
                        "failed": sum(c.get("success") is False for c in cases)}, "failures": failures,
            "duration_seconds": time.monotonic() - started, "raw": result}
    except (TimeoutError, RequestTimeout):
        payload = {"backend": "microcks", "status": "timeout", "duration_seconds": time.monotonic() - started}
        if isinstance(result, dict):
            # Preserve the last response (including remote run id); it is
            # partial evidence, not a completed verdict or invented counts.
            payload.update(partial=True, raw=result)
        return payload
    finally:
        if session:
            session.close()
