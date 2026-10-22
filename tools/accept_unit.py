"""Real-model unit acceptance: primary CLI, offline Docker, faults and recovery."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def fingerprints(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    folder = (args.output_dir or ROOT / ".unit-generation-runs" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    result = {"status": "failed", "scope": "single-module Python unit generation", "checks": {}}
    try:
        proc = subprocess.run([sys.executable, "-B", "-c", "from prototype import main; main()",
            "--generation-unit", "--source-code", str(ROOT / "benchmark/generation/control/server.py"),
            "--report-dir", str(folder / "generation")], cwd=ROOT, timeout=600)
        summaries = list((folder / "generation").glob("unit-generation/*/summary.json"))
        if len(summaries) != 1:
            raise RuntimeError("Unit CLI failed; inspect generation report")
        generated = json.loads(summaries[0].read_text())
        result["generation_report"] = str(summaries[0])
        if proc.returncode or generated["status"] != "passed":
            raise RuntimeError("Generated unit tests did not pass: " +
                               str(generated.get("error") or generated["status"]))
        result["checks"]["baseline"] = generated["runner"]["summary"]
        tests = summaries[0].parent / "tests"
        before = fingerprints(tests)
        original = (tests / "unit_source.py").read_text()
        from prototype.service_tools.runner.contracts import RunConfig
        from prototype.service_tools.runner.docker_runner import run_tests
        faults = {
            "wrong_total": ("round(quantity * price, 2)", "round(quantity * price, 2) + 1"),
            "quantity_boundary": ("else 10)", "else 9)"),
            "nonfinite_float": ('raise ValueError("Nonfinite JSON number")', "return parsed"),
        }
        for name, (old, new) in faults.items():
            if original.count(old) != 1:
                raise RuntimeError(f"Fault {name}: source anchor is ambiguous")
            variant = folder / name / "tests"
            shutil.copytree(tests, variant)
            (variant / "unit_source.py").write_text(original.replace(old, new))
            run = run_tests(RunConfig(variant, folder / name / "runner"))
            data = run.to_dict()
            result["checks"][name] = data
            if (data["status"] != "completed" or data["exit_code"] != 1
                    or data["summary"]["failed"] == 0 or data["summary"]["error"]
                    or data["summary"]["skipped"]):
                raise RuntimeError(f"Fault {name} was not detected by test assertions")
        recovered = run_tests(RunConfig(tests, folder / "recovery"))
        recovery = recovered.to_dict()
        result["checks"]["recovery"] = recovery
        if (recovery["status"] != "completed" or recovery["exit_code"] != 0
                or recovery["summary"]["passed"] == 0 or recovery["summary"]["skipped"]):
            raise RuntimeError("Original source did not pass recovery")
        result["tests_and_source_unchanged"] = before == fingerprints(tests)
        if not result["tests_and_source_unchanged"]:
            raise RuntimeError("Original source or generated tests changed")
        result["status"] = "passed"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    report = folder / "summary.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Unit acceptance: {result['status']} {report}", flush=True)
    if result.get("error"):
        print(result["error"], flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
