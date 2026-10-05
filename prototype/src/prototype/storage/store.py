"""Save generated pytest modules as an immutable, versioned directory (ТЗ 2.1.6).

<output_root>/<contract_slug>/<run_id>/{tests/, rejected/, manifest.json}

A version is written into a hidden temporary directory and renamed in one step,
so a half-written version never appears under its run_id. Existing versions are
never overwritten: a clash on the same second gets a _2, _3... suffix.
"""

from datetime import datetime
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import re
import shutil

from .analysis import analyze_test_file, normalize_code
from .collect import collect_tests, skipped_collection
from .contracts import SaveRequest, SavedSuite


MANIFEST_SCHEMA_VERSION = 1
# prototype/src/prototype/storage/store.py -> prototype/generated
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[3] / "generated"
_SLUG_LIMIT = 64


def _now() -> datetime:
    return datetime.now().astimezone()


def make_slug(title: str, fallback: str) -> str:
    for candidate in (title, fallback):
        slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
        slug = slug[:_SLUG_LIMIT].rstrip("-")
        if slug:
            return slug
    return "contract"


def read_contract_title(text: str) -> str:
    """Return info.title of a JSON/YAML contract, or "" if it cannot be read."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # lazy: importing prototype.storage stays stdlib-only

            data = yaml.safe_load(text)
        except Exception:
            return ""
    info = data.get("info") if isinstance(data, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return title.strip() if isinstance(title, str) else ""


def _output_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    from_env = os.environ.get("TESTGEN_OUTPUT_DIR", "").strip()
    return Path(from_env) if from_env else DEFAULT_OUTPUT_ROOT


def _reserve_run_dir(contract_dir: Path, created_at: datetime) -> tuple[str, Path]:
    base = created_at.strftime("%Y-%m-%d_%H%M%S")
    for index in count(1):
        run_id = base if index == 1 else f"{base}_{index}"
        if (contract_dir / run_id).exists():
            continue
        tmp_dir = contract_dir / f".{run_id}.tmp"
        try:
            tmp_dir.mkdir()  # atomic: a concurrent save cannot take the same tmp_dir
        except FileExistsError:
            continue
        return run_id, tmp_dir
    raise AssertionError("unreachable")


def _summary(files: list[dict], collection: dict) -> dict:
    measured = collection["status"] in {"ok", "no_tests", "errors"}
    return {
        "files_total": len(files),
        "files_ok": sum(item["status"] == "ok" for item in files),
        "files_no_tests": sum(item["status"] == "no_tests" for item in files),
        "files_rejected": sum(item["status"] == "syntax_error" for item in files),
        "test_functions": sum(item["test_functions"] or 0 for item in files),
        "collected": collection["collected"] if measured else None,
    }


def save_test_suite(request: SaveRequest) -> SavedSuite:
    contract_path = request.contract_path
    if not contract_path.is_file():
        raise FileNotFoundError(f"contract not found: {contract_path}")
    contract_bytes = contract_path.read_bytes()
    title = read_contract_title(contract_bytes.decode("utf-8", errors="replace"))
    slug = make_slug(title, contract_path.stem)

    contract_dir = _output_root(request.output_root) / slug
    contract_dir.mkdir(parents=True, exist_ok=True)
    created_at = _now()
    run_id, tmp_dir = _reserve_run_dir(contract_dir, created_at)
    try:
        (tmp_dir / "tests").mkdir()
        files = []
        for generated in request.files:
            code = normalize_code(generated.code)
            analysis = analyze_test_file(code, generated.name)
            location = "rejected" if analysis.status == "syntax_error" else "tests"
            data = code.encode("utf-8")
            (tmp_dir / location).mkdir(exist_ok=True)
            (tmp_dir / location / generated.name).write_bytes(data)
            files.append({
                "name": generated.name,
                "location": location,
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": analysis.status,
                "test_functions": analysis.test_functions,
                "test_cases": list(analysis.test_cases),
                "warnings": list(analysis.warnings),
                "error": analysis.error,
            })

        if request.collect and any(item["location"] == "tests" for item in files):
            collection = collect_tests(tmp_dir / "tests", request.collect_timeout_seconds)
        else:
            collection = skipped_collection()

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": created_at.isoformat(timespec="seconds"),
            "contract": {
                "path": str(contract_path),
                "sha256": hashlib.sha256(contract_bytes).hexdigest(),
                "title": title,
                "slug": slug,
            },
            "generator": {"model": request.model, "meta": request.generator_meta},
            "files": files,
            "collection": collection,
            "summary": _summary(files, collection),
        }
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        run_dir = contract_dir / run_id
        # Fails instead of replacing if run_dir appeared meanwhile.
        os.rename(tmp_dir, run_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return SavedSuite(
        run_id=run_id,
        run_dir=run_dir,
        tests_dir=run_dir / "tests",
        manifest_path=run_dir / "manifest.json",
        manifest=manifest,
    )
