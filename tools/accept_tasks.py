#!/usr/bin/env python3
"""Acceptance for 2.2.9 and HTTP 4.1.4. Calls real LLMs and Docker.

Unit generation has a separate acceptance command and is outside this HTTP scope.
No commits or publication are performed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprints():
    files = []
    for directory in ("prototype/src", "benchmark/generation", "benchmark/mutation", "tools"):
        files.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not any(part.startswith('.') or part == '__pycache__' for part in p.relative_to(ROOT).parts))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    folder = (args.output_dir or ROOT / '.acceptance-runs' /
              (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    before = source_fingerprints()
    result = {'status': 'failed', 'scope': ['2.2.9', '4.1.4 HTTP'],
              'deferred': ['4.1.4.2 unit generation'], 'steps': [], 'source_sha256': before}
    steps = [
        ('regression', [sys.executable, '-B', 'tools/verify.py', '--report-root', str(folder / 'regression')]),
        ('categories-story-regeneration', [sys.executable, '-B', 'benchmark/generation/control/acceptance.py',
                                           '--output-dir', str(folder / 'control')]),
        ('catalogue-mutation-metrics', [sys.executable, '-B', 'benchmark/generation/catalogue_acceptance.py',
                                        '--output-dir', str(folder / 'catalogue')]),
    ]
    print('Acceptance reports:', folder, flush=True)
    try:
        for name, command in steps:
            print('Starting:', name, flush=True)
            completed = subprocess.run(command, cwd=ROOT, timeout=3600)
            reports = list((folder / {'regression': 'regression', 'categories-story-regeneration': 'control',
                                      'catalogue-mutation-metrics': 'catalogue'}[name]).glob('**/summary.json'))
            if name == 'regression':
                reports = list((folder / 'regression').glob('*/summary.json'))
            else:
                reports = [folder / ('control' if name.startswith('categories') else 'catalogue') / 'summary.json']
            summaries = [json.loads(p.read_text()) for p in reports if p.is_file()]
            passed = (completed.returncode == 0 and len(summaries) == 1
                      and summaries[0].get('result', summaries[0].get('status')) in ('ok', 'passed'))
            result['steps'].append({'name': name, 'passed': passed, 'exit_code': completed.returncode,
                                    'reports': [str(p.relative_to(folder)) for p in reports]})
            if not passed:
                break
        result['sources_unchanged'] = before == source_fingerprints()
        if len(result['steps']) == len(steps) and all(s['passed'] for s in result['steps']) and result['sources_unchanged']:
            result['status'] = 'passed'
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        (folder / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        rows = ['# Приёмка 2.2.9 и HTTP-части 4.1.4', '', 'Статус: ' + result['status'],
                '', 'Unit-генерация проверяется отдельно; это не приёмка всего проекта.', '']
        rows += [f"- {s['name']}: {'passed' if s['passed'] else 'failed'} — " + ', '.join(s['reports']) for s in result['steps']]
        (folder / 'README.md').write_text('\n'.join(rows) + '\n')
        print('Result:', result['status'], folder / 'summary.json', flush=True)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
