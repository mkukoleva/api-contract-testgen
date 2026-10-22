"""Exercise the real CLI: fresh generation -> mutation -> common metrics."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[2]


def command(args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    command(['docker', 'info', '--format', '{{.ServerVersion}}'])
    folder = args.output_dir or ROOT / '.acceptance-runs' / 'catalogue' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    print('Отчёты:', folder, flush=True)
    command(['docker', 'build', '-t', 'api-contract-pytest-runner:step5',
             str(ROOT / 'prototype/src/prototype/service_tools/runner')])
    project = 'mutation-integration-' + uuid4().hex[:10]
    compose = yaml.safe_load((ROOT / 'benchmark/catalogue/compose.yaml').read_text())
    compose['name'] = project
    compose['services']['catalogue'].pop('ports', None)
    compose['services']['catalogue']['networks'].remove('host-access')
    compose['networks'].pop('host-access')
    path = folder / 'compose.json'
    path.write_text(json.dumps(compose, indent=2))
    docker = ['docker', 'compose', '-p', project, '-f', str(path)]
    try:
        command(docker + ['up', '-d', '--wait', '--wait-timeout', '120'])
        outcome = subprocess.run([
            sys.executable, str(ROOT / 'prototype/main.py'),
            str(ROOT / 'benchmark/catalogue/catalogue.swagger.json'),
            '--generate', '--mutation', 'catalogue',
            '--base-url', 'http://catalogue:8080', '--network', project + '_runner',
            '--blocked-network', project + '_database',
            '--user-story', str(ROOT / 'benchmark/generation/catalogue-user-story.json'),
            '--max-repair-attempts', '2', '--report-dir', str(folder / 'reports'),
            '--tests-output-dir', str(folder / 'saved'),
        ], cwd=ROOT)
        summary = {'status': 'failed', 'cli_exit_code': outcome.returncode}
        mutations = list((folder / 'reports').rglob('mutation-summary.json'))
        metrics = list((folder / 'reports').rglob('metrics_*.json'))
        if len(mutations) == len(metrics) == 1:
            mutation = json.loads(mutations[0].read_text())
            common = json.loads(metrics[0].read_text())['metrics']
            score = mutation['mutation_score_percent']
            campaign_path = Path(mutation['report_dir']) / 'report.json' if mutation.get('report_dir') else None
            campaign = json.loads(campaign_path.read_text()) if campaign_path else {}
            matches = score is not None and score == common['mutation_score_percent'] == campaign.get('metrics', {}).get('mutation_score', {}).get('percent')
            current = Path(mutation.get('tests_source', '/')).is_relative_to(folder / 'saved')
            summary.update(mutation_status=mutation['status'], mutation_score_percent=score,
                           common_score_matches_campaign=matches, uses_new_saved_tests=current,
                           inputs_unchanged=mutation.get('inputs_unchanged'),
                           mutation_report=str(mutations[0]), metrics_report=str(metrics[0]))
            if outcome.returncode == 0 and matches and current and mutation.get('inputs_unchanged') is True:
                summary['status'] = 'passed'
        (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        print('Итог:', folder / 'summary.json', flush=True)
        return 0 if summary['status'] == 'passed' else 1
    finally:
        command(docker + ['down', '--volumes', '--remove-orphans'])


if __name__ == '__main__':
    raise SystemExit(main())
