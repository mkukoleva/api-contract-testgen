"""Isolated integration with the saved Catalogue suite, through the unmodified runner."""
from __future__ import annotations
import argparse
import copy
from dataclasses import replace
import math
from threading import Lock
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'prototype/src'))
sys.path.insert(0, str(HERE))

from prototype.evaluate.mutation import CaseObservation, DeliveryEvidence, SuiteObservation, run_mutation_campaign
from prototype.service_tools.runner.contracts import RunConfig
from prototype.service_tools.runner.docker_runner import run_tests
import yaml

TESTS = ROOT / 'benchmark/pytest-runner/saved-sets/catalogue-2026-10-05/tests'
CONTRACT = ROOT / 'benchmark/catalogue/catalogue.swagger.json'
BASE_IMAGE = 'api-contract-pytest-runner:step5'
IMAGE = 'api-contract-mutation-catalogue:stage6'


import copy
import hashlib
import json
from pathlib import Path

SOURCE_SHA256 = 'c52810441d6f1f5530f41d83dcc47b0bae2d3be494923aab649741a081b12672'


def convert(path: Path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('Catalogue snapshot changed; review conversion before measuring')
    source = json.loads(raw)
    def refs(value):
        if isinstance(value, list): return [refs(v) for v in value]
        if isinstance(value, dict):
            return {k: v.replace('#/definitions/', '#/components/schemas/') if k == '$ref'
                    else refs(v) for k, v in value.items()}
        return value
    result = {'openapi': '3.0.3', 'info': copy.deepcopy(source['info']), 'paths': {},
              'components': {'schemas': refs(source['definitions'])}}
    for path, item in source['paths'].items():
        operation = item['get']
        parameters = []
        for param in operation.get('parameters', []):
            parameters.append({'name': param['name'], 'in': param['in'], 'required': param['required'],
                               'schema': {'type': param['type']}})
        result['paths'][path] = {'get': {
            'operationId': operation['operationId'], 'parameters': parameters,
            'responses': {code: {'description': response['description'],
                'content': {'application/json': {'schema': refs(response['schema'])}}}
                for code, response in operation['responses'].items()},
        }}
    return result


def command(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[:3]}: {result.stderr[-4000:] or result.stdout[-4000:]}')
    return result.stdout.strip()


def image_id(name):
    return command('docker', 'image', 'inspect', name, '--format', '{{.Id}}')


def build_image(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'Dockerfile').write_text('ARG BASE_IMAGE=api-contract-pytest-runner:step5\nFROM ${BASE_IMAGE}\nUSER 0:0\nRUN cp /opt/runner/pytest_worker.py /opt/runner/base_worker.py\nRUN python -m pip install --no-cache-dir openapi-spec-validator==0.9.0 openapi-schema-validator==0.9.0 jsonschema==4.26.0\nCOPY mutation.py /opt/mutation/prototype/evaluate/\nCOPY proxy.py /opt/mutation/\nCOPY worker.py /opt/runner/pytest_worker.py\nENV PYTHONPATH=/opt/mutation\nUSER 65534:65534\n')
    for name in ('proxy.py', 'worker.py'):
        shutil.copyfile(HERE / name, directory / name)
    for name in ('mutation.py',):
        shutil.copyfile(ROOT / 'prototype/src/prototype/evaluate' / name, directory / name)
    base = image_id(BASE_IMAGE)
    # BuildKit resolves FROM as an image reference, not a bare local image ID.
    pinned_tag = 'api-contract-mutation-base:' + uuid4().hex
    command('docker', 'image', 'tag', base, pinned_tag)
    try:
        if image_id(pinned_tag) != base:
            raise RuntimeError('Pinned base image differs from the selected runner')
        output = command('docker', 'build', '--pull=false', '--build-arg',
                         f'BASE_IMAGE={pinned_tag}', '-t', IMAGE, str(directory), timeout=300)
        (directory / 'build.log').write_text(output)
        if image_id(pinned_tag) != base:
            raise RuntimeError('Pinned base image changed during build')
        return image_id(IMAGE)
    finally:
        command('docker', 'image', 'rm', pinned_tag)



@contextmanager
def infrastructure(directory, image):
    project = 'mutation-catalogue-' + uuid4().hex[:10]
    compose = yaml.safe_load((ROOT / 'benchmark/catalogue/compose.yaml').read_text())
    compose['name'] = project
    compose['services']['catalogue'].pop('ports')
    compose['services']['catalogue']['networks'] = ['backend', 'database']
    compose['networks'] = {key: {'internal': True} for key in ('backend', 'database')}
    compose_path = directory / 'compose.json'
    compose_path.write_text(json.dumps(compose, indent=2))
    runner_network = project + '-runner'
    try:
        command('docker', 'compose', '-f', str(compose_path), 'up', '-d', '--wait', '--wait-timeout', '120', timeout=150)
        command('docker', 'network', 'create', '--internal', runner_network)
        # Readiness is trusted infrastructure, never generated test code on the host.
        code = "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); data=json.load(o.open('http://catalogue:8080/catalogue',timeout=10)); assert isinstance(data,list) and len(data)>0; print(len(data))"
        count = command('docker', 'run', '--rm', '--network', project + '_backend', '--entrypoint', 'python', image, '-c', code)
        print(f'Isolated Catalogue ready: {count} products', flush=True)
        yield project, runner_network
    finally:
        # Only this uniquely named temporary stack and its volumes are removed.
        command('docker', 'compose', '-f', str(compose_path), 'down', '--volumes', '--remove-orphans', timeout=90)
        result = subprocess.run(['docker', 'network', 'rm', runner_network], capture_output=True, text=True)
        if result.returncode and 'not found' not in result.stderr:
            raise RuntimeError('Cannot clean up mutation runner network: ' + result.stderr)


def adapt_result(result, events, observation):
    """Keep runner policy/errors authoritative; require additional structured evidence."""
    diagnostics = list(result.collection_errors)
    if result.error_message: diagnostics.append(result.error_message)
    if observation is None:
        diagnostics.append('Mutation observer produced no full collection evidence')
        return SuiteObservation(str(result.status), result.exit_code, diagnostics=tuple(diagnostics))
    if (result.status != 'completed' or result.exit_code != observation['exit_code']
            or {(t.nodeid, str(t.outcome), t.phase) for t in result.tests}
            != {(c['nodeid'], c['outcome'], c['phase']) for c in observation['cases']}):
        diagnostics.append('Runner and mutation observer disagree or runner did not complete')
    cases = tuple(CaseObservation(**c) for c in observation['cases'])
    diagnostics.extend(observation['diagnostics'])
    for event in events:
        preparation = event.get('preparation', {})
        if event.get('error') or preparation.get('status') in {'baseline_invalid', 'invalid', 'unsupported', 'unavailable'}:
            diagnostics.append(event.get('error') or preparation.get('reason'))
    deliveries = tuple(DeliveryEvidence(
        e['preparation']['mutant_id'], e['nodeid'], e['phase'], e['delivered'],
        e['preparation']['status'] == 'ready' and bool(e['preparation']['violations'])
        and e['body'] == e['preparation']['body'] and e['status'] == e['preparation']['mutated_status'],
        e['preparation']['value_pointer'],
    ) for e in events if e['selected'])
    return SuiteObservation(str(result.status), result.exit_code,
        tuple(observation['collected_nodeids']), cases, deliveries,
        'ready' if deliveries else None, tuple(diagnostics),
        {k: str(v) for k, v in result.report_paths.items()})


def execute(plan, directory, *, contract, image, project, network, timeout, tests=TESTS):
    print(f'Run {directory.name}: {plan.mutant_id if plan else "original"}', flush=True)
    evidence = directory / 'proxy'
    evidence.mkdir()
    evidence.chmod(0o777)
    config = directory / 'config'
    config.mkdir()
    (config / 'config.json').write_text(json.dumps({'contract': contract, 'plan': plan.to_dict() if plan else None}))
    name = project + '-proxy-' + uuid4().hex[:8]
    network = name + '-runner'
    try:
        command('docker', 'network', 'create', '--internal', network)
        command('docker', 'create', '--name', name, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--pids-limit', '64', '--memory', '256m',
                '--network', project + '_backend', '--mount', f'type=bind,source={config},target=/config,readonly',
                '--mount', f'type=bind,source={evidence},target=/evidence',
                '--entrypoint', 'python', image, '/opt/mutation/proxy.py')
        command('docker', 'network', 'connect', '--alias', 'mutation-api', network, name)
        command('docker', 'start', name)
        ready = "import socket,time\nfor i in range(50):\n try:\n  s=socket.create_connection(('127.0.0.1',8080),.1);s.close();break\n except OSError: time.sleep(.1)\nelse: raise RuntimeError('proxy not ready')"
        command('docker', 'exec', name, 'python', '-c', ready, timeout=10)
        result = run_tests(RunConfig(tests, directory / 'runner', 'http://mutation-api:8080', timeout),
                           image=image, network=network,
                           blocked_networks=(project + '_database', project + '_backend'))
        event_path = evidence / 'http.jsonl'
        events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.exists() else []
        observations = list((directory / 'runner').glob('*/mutation-observation.json'))
        observation = json.loads(observations[0].read_text()) if len(observations) == 1 else None
        return adapt_result(result, events, observation)
    finally:
        logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
        (directory / 'proxy.log').write_text(logs.stdout + logs.stderr)
        removed = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True)
        if removed.returncode and 'No such container' not in removed.stderr:
            raise RuntimeError('Cannot remove temporary mutation proxy: ' + removed.stderr)
        removed_network = subprocess.run(['docker', 'network', 'rm', network], capture_output=True, text=True)
        if removed_network.returncode and 'not found' not in removed_network.stderr:
            raise RuntimeError('Cannot remove temporary mutation network: ' + removed_network.stderr)


def fingerprint(image, timeout=60, tests=TESTS, workers=4):
    paths = [p for p in tests.rglob('*') if p.is_file() and not any(x in {'__pycache__', '.pytest_cache'} for x in p.parts)] + list(HERE.glob('*.py')) + [CONTRACT,
            ROOT / 'benchmark/catalogue/compose.yaml']
    paths += list((ROOT / 'prototype/src/prototype/evaluate').glob('mutation*.py'))
    paths += list((ROOT / 'prototype/src/prototype/service_tools/runner').rglob('*.py'))
    digest = hashlib.sha256(json.dumps({'image': image, 'timeout': timeout, 'workers': workers}).encode())
    for path in sorted(paths): digest.update(str(path).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()



def comparable_original(path, response):
    """Ignore only ordering of Catalogue's tag collections, retaining multiplicity.

    The original HTTP bytes and mutation payloads are never normalized. Product
    order, image order, every scalar and every field remain significant.
    """
    result = copy.deepcopy(response)
    body = result['body']
    def sort_tags(value, key):
        if isinstance(value, dict) and isinstance(value.get(key), list) and all(isinstance(v, str) for v in value[key]):
            value[key] = sorted(value[key])
    if path == '/catalogue' and isinstance(body, list):
        for item in body:
            sort_tags(item, 'tag')
    elif path == '/tags':
        sort_tags(body, 'tags')
    elif path.startswith('/catalogue/') and path != '/catalogue/size':
        sort_tags(body, 'tag')
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=HERE.parent / '.runs/catalogue')
    parser.add_argument('--tests', type=Path, default=TESTS)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('timeout must be finite and positive')
    tests = args.tests.resolve()
    if not tests.is_dir():
        parser.error('tests must be an existing directory')
    output = args.output_dir.resolve()
    if output.is_relative_to(tests) or tests.is_relative_to(output):
        parser.error('Tests and output must be separate, non-nested directories')
    output.mkdir(parents=True, exist_ok=True)
    contract = convert(CONTRACT)
    session = output / ('session-' + uuid4().hex[:10])
    session.mkdir()
    endpoint = os.environ.get('DOCKER_HOST') or command('docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
    plugins = json.loads(command('docker', 'info', '--format', '{{json .ClientInfo.Plugins}}'))
    plugin_dirs = sorted({str(Path(p['Path']).parent) for p in plugins})
    docker_config = session / 'docker-config'
    docker_config.mkdir()
    (docker_config / 'config.json').write_text(json.dumps({'cliPluginsExtraDirs': plugin_dirs}))
    os.environ['DOCKER_CONFIG'] = str(docker_config)
    os.environ['DOCKER_HOST'] = endpoint
    (session / 'contract.openapi.json').write_text(json.dumps(contract, indent=2))
    image = build_image(session / 'build') if args.build else image_id(IMAGE)
    originals = {}
    integrity = {'stable': True}
    integrity_lock = Lock()
    with infrastructure(session, image) as (project, network):
        def checked_execute(plan, directory):
            result = execute(plan, directory, contract=contract, image=image, project=project,
                             network=network, timeout=args.timeout, tests=tests)
            event_path = directory / 'proxy/http.jsonl'
            events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.exists() else []
            with integrity_lock:
                first = not originals
                for event in events:
                    if 'original_body' not in event:
                        continue
                    value = {'status': event['original_status'], 'body': event['original_body']}
                    path = event['path']
                    if first and path not in originals:
                        originals[path] = value
                    elif path not in originals or comparable_original(path, originals[path]) != comparable_original(path, value):
                        integrity['stable'] = False
                if not originals or not integrity['stable']:
                    result = replace(result, diagnostics=(*result.diagnostics, 'Original Catalogue responses missing or changed'))
            return result
        report = run_mutation_campaign(contract,
            execute=checked_execute,
            fingerprint=lambda: fingerprint(image, args.timeout, tests, args.workers) + str(integrity['stable']),
            output_dir=session, max_workers=args.workers)
    (session / 'original-responses.json').write_text(json.dumps({'stable': integrity['stable'], 'comparison': 'Exact except tag/tags string-array order; duplicates retained', 'responses': originals}, indent=2))
    (session / 'provenance.json').write_text(json.dumps({'image_id': image, 'contract_source': str(CONTRACT),
        'tests_source': str(tests), 'timeout': args.timeout, 'workers': args.workers, 'report_dir': report['report_dir']}, indent=2))
    print(json.dumps({'status': report['status'], 'metrics': report['metrics'], 'report_dir': report['report_dir']}, indent=2))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__': raise SystemExit(main())
