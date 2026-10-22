"""Connect a successful generated suite to the trusted Catalogue adapter."""
import hashlib
import importlib.util
import json
from pathlib import Path
import time


def _adapter():
    path = Path(__file__).resolve().parents[4] / 'benchmark/mutation/catalogue/run.py'
    spec = importlib.util.spec_from_file_location('_catalogue_mutation_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(tests):
    files = [p for p in tests.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    if any(p.is_symlink() for p in tests.rglob('*')):
        raise ValueError('Saved tests must not contain symbolic links')
    return {p.relative_to(tests).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files}


def evaluate_generated_suite(*, generation, contract, contract_sha256, output_dir):
    """Return and persist facts; never substitute a score from an earlier run."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = {'status': 'unavailable', 'reason': None, 'mutation_score_percent': None,
              'adapter': 'catalogue', 'contract_sha256': contract_sha256}
    try:
        if generation.get('pipeline_status') != 'success' or generation.get('runner_status') != 'completed':
            raise ValueError('Generation and baseline execution must succeed before mutation testing')
        adapter = _adapter()
        from prototype.parser.contract import load_contract_document
        if contract != load_contract_document(adapter.CONTRACT).data:
            raise ValueError('Catalogue adapter supports only its pinned Catalogue contract')
        versions = generation.get('saved_versions') or []
        if not versions:
            raise ValueError('No saved test version available')
        tests = Path(versions[-1]['tests_dir']).resolve()
        manifest = json.loads((tests.parent / 'manifest.json').read_text())
        if manifest['contract']['sha256'] != contract_sha256:
            raise ValueError('Saved suite belongs to a different contract')
        before = _snapshot(tests)
        expected = {f['name']: f['sha256'] for f in manifest['files'] if f['location'] == 'tests'}
        if not before or before != expected:
            raise ValueError('Saved test files do not match their manifest')
        result.update(tests_source=str(tests), tests_sha256=before)
        report = adapter.run_catalogue(tests=tests, output_dir=output / 'campaign', build=True)
        unchanged = before == _snapshot(tests) and report.get('inputs_unchanged') is True
        result.update(status=report['status'], reason=report.get('reason'),
                      metrics=report['metrics'], report_dir=report['report_dir'], inputs_unchanged=unchanged)
        if not unchanged:
            raise ValueError('Campaign inputs changed; mutation score is unavailable')
        if report['status'] in ('completed', 'completed_with_inconclusive'):
            result['mutation_score_percent'] = report['metrics']['mutation_score']['percent']
            if report['status'] == 'completed_with_inconclusive':
                result['reason'] = 'Score excludes inconclusive mutants; see campaign counts'
    except Exception as exc:
        result.update(status='unavailable', reason=str(exc), mutation_score_percent=None)
    result['duration_seconds'] = round(time.monotonic() - started, 3)
    result['summary_path'] = str(output / 'mutation-summary.json')
    Path(result['summary_path']).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result
