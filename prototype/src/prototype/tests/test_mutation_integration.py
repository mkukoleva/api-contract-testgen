import hashlib
import json
from types import SimpleNamespace

import pytest

from prototype.evaluate import mutation_integration as integration


@pytest.fixture
def setup(tmp_path, monkeypatch):
    contract = {'openapi': '3.0.3', 'paths': {'/items': {'get': {}}}}
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(contract))
    tests = tmp_path / 'saved/tests'
    tests.mkdir(parents=True)
    (tests / 'test_api.py').write_text('def test_api(): assert True\n')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'contract': {'sha256': sha}, 'files': [
        {'name': name, 'sha256': digest, 'location': 'tests'}
        for name, digest in integration._snapshot(tests).items()]}
    (tests.parent / 'manifest.json').write_text(json.dumps(manifest))
    calls = []
    report = {'status': 'completed', 'inputs_unchanged': True, 'report_dir': 'current-report',
              'metrics': {'mutation_score': {'percent': 50.0}}}
    def campaign(**kwargs):
        calls.append(kwargs)
        return report
    adapter = SimpleNamespace(CONTRACT=path, run_catalogue=campaign)
    monkeypatch.setattr(integration, '_adapter', lambda: adapter)
    args = dict(generation={'pipeline_status': 'success', 'runner_status': 'completed',
                            'saved_versions': [{'tests_dir': 'old-version'}, {'tests_dir': str(tests)}]},
                contract=contract, contract_sha256=sha, output_dir=tmp_path / 'result')
    return args, tests, calls, report, adapter


def test_current_final_version_and_measured_score(setup):
    args, tests, calls, report, _ = setup
    result = integration.evaluate_generated_suite(**args)
    assert result['mutation_score_percent'] == 50
    assert calls[0]['tests'] == tests
    assert json.loads((args['output_dir'] / 'mutation-summary.json').read_text()) == result


@pytest.mark.parametrize('problem', ['failed', 'contract', 'hash', 'files', 'no_version'])
def test_reject_before_docker(setup, problem):
    args, tests, calls, _, _ = setup
    if problem == 'failed':
        args['generation']['pipeline_status'] = 'generation_error'
    elif problem == 'contract':
        args['contract'] = {}
    elif problem == 'hash':
        args['contract_sha256'] = 'wrong'
    elif problem == 'files':
        (tests / 'test_api.py').write_text('changed')
    else:
        args['generation']['saved_versions'] = []
    result = integration.evaluate_generated_suite(**args)
    assert result['mutation_score_percent'] is None and result['reason']
    assert not calls


def test_changed_inputs_invalidate_score(setup):
    args, _, _, report, _ = setup
    report['inputs_unchanged'] = False
    result = integration.evaluate_generated_suite(**args)
    assert result['mutation_score_percent'] is None


def test_docker_error_is_reported_without_losing_generation(setup):
    args, _, _, _, adapter = setup
    def fail(**kwargs):
        raise RuntimeError('Docker unavailable')
    adapter.run_catalogue = fail
    result = integration.evaluate_generated_suite(**args)
    assert result['status'] == 'unavailable' and result['reason'] == 'Docker unavailable'
    assert args['generation']['pipeline_status'] == 'success'


def test_adapter_restores_environment_on_failure(monkeypatch):
    # Load the real adapter, but never call Docker.
    adapter = integration._adapter()
    monkeypatch.setenv('DOCKER_HOST', 'original')
    monkeypatch.delenv('DOCKER_CONFIG', raising=False)
    def fail(args):
        import os
        os.environ.update(DOCKER_HOST='temporary', DOCKER_CONFIG='temporary')
        raise RuntimeError('failure')
    monkeypatch.setattr(adapter, '_run_catalogue', fail)
    with pytest.raises(RuntimeError):
        adapter.run_catalogue(tests='tests', output_dir='output')
    import os
    assert os.environ['DOCKER_HOST'] == 'original' and 'DOCKER_CONFIG' not in os.environ


def test_inconclusive_score_keeps_qualification(setup):
    args, _, _, report, _ = setup
    report['status'] = 'completed_with_inconclusive'
    result = integration.evaluate_generated_suite(**args)
    assert result['mutation_score_percent'] == 50
    assert 'inconclusive' in result['reason']


def test_unsupported_cli_combination_rejected_before_model(monkeypatch):
    import sys
    import prototype
    monkeypatch.setattr(sys, 'argv', ['tester', 'unused.json', '--mutation', 'catalogue'])
    with pytest.raises(SystemExit) as exc:
        prototype.main()
    assert exc.value.code == 2
