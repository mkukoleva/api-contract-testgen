import json
from types import SimpleNamespace
import pytest
from prototype.generator.input_coverage import input_coverage, supplement
from prototype.generator.contracts import GenerationSettings
from prototype.generator.pipeline import run_generation_pipeline
from prototype.service_tools.runner.contracts import RunResult, TestResult as Result
from prototype.service_tools.runner.fixtures import request_body_evidence

CONTRACT = {'openapi': '3.0.3', 'info': {'title': 'Inputs', 'version': '1'}, 'paths': {'/items': {'post': {
    'requestBody': {'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'properties': {'tags': {'type': 'array', 'minItems': 0, 'maxItems': 3,
            'items': {'type': 'string', 'enum': ['a', 'b']}}}}}}},
    'responses': {'200': {'description': 'OK'}, '422': {'description': 'Invalid'}}}}}}


def observation(value, status=200, name='test_boundary_input', path='/items'):
    return {'nodeid': 'test_initial.py::' + name, 'method': 'POST', 'path': path, 'status_code': status,
            'request_body': {'kind': 'json', 'value': value}}


def run_data(correct=False):
    events = [observation({}, name='test_normal_input'), observation({'tags': []}),
              observation({'tags': ['a', 'b', 'a']}),
              observation({'tags': 5}, 422, 'test_error_input'),
              observation({'tags': ['a', 'b', 'a', 'bad']}, 422, 'test_error_input'),
              {**observation(None, 422, 'test_error_input'), 'request_body': {'kind': 'absent'}}]
    if correct:
        events.append(observation({'tags': ['a', 'b', 'a', 'a']}, 422, 'test_error_input'))
    return {'status': 'completed', 'http_observations_complete': True, 'http_observations': events,
            'tests': [{'nodeid': node, 'outcome': 'passed'} for node in sorted({e['nodeid'] for e in events})]}


def test_mixed_violation_is_not_boundary_evidence():
    report = input_coverage(CONTRACT, run_data())
    assert report['operations'][0]['missing'] == ['tags:outside_maxItems']
    assert input_coverage(CONTRACT, run_data(True))['status'] == 'observed'


@pytest.mark.parametrize('change', ['failed', 'route', 'status', 'truncated', 'old_runner', 'large_body'])
def test_unproven_requests_do_not_close_conditions(change):
    run = run_data(True)
    if change == 'failed':
        run['tests'][-1]['outcome'] = 'failed'
        for test in run['tests']:
            test['outcome'] = 'failed'
    elif change == 'route':
        run['http_observations'][-1]['path'] = '/other'
    elif change == 'status':
        run['http_observations'][-1]['status_code'] = 200
    elif change == 'truncated':
        run['http_observations_complete'] = False
    elif change == 'old_runner':
        run['http_observations'][-1].pop('request_body')
    else:
        run['http_observations'][-1]['request_body'] = {'kind': 'unavailable'}
    assert input_coverage(CONTRACT, run)['status'] != 'observed'


def test_json_evidence_is_bounded_and_does_not_capture_headers():
    import requests
    req = requests.Request('POST', 'http://api', json={'tags': ['a']}, headers={'Authorization': 'secret'}).prepare()
    assert request_body_evidence(req) == {'kind': 'json', 'value': {'tags': ['a']}}
    req.body = 'x' * 2049
    assert request_body_evidence(req) == {'kind': 'unavailable'}
    req.body = '{'
    assert request_body_evidence(req) == {'kind': 'malformed'}


class Model:
    def __init__(self, overwrite=False):
        self.calls = 0
        self.overwrite = overwrite
    def invoke(self, messages):
        self.calls += 1
        name = 'test_initial.py' if self.calls == 1 or self.overwrite else f'test_added_{self.calls}.py'
        code = '\n'.join(f'def test_{kind}_input(base_url, api_client):\n    assert api_client.post(base_url + "/items", json={{}}).status_code == 200\n' for kind in ('normal', 'boundary', 'error'))
        return SimpleNamespace(content=json.dumps({'files': [{'name': name, 'code': code}]}),
                               usage_metadata={'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30})


@pytest.mark.parametrize('closes,limit,overwrite', [(True, 2, False), (False, 1, False), (True, 0, False), (True, 2, True)])
def test_pipeline_supplements_append_only_and_bounds_attempts(tmp_path, closes, limit, overwrite):
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(CONTRACT))
    model = Model(overwrite)
    snapshots = []
    def save(request):
        snapshots.append({file.name: file.code for file in request.files})
        return SimpleNamespace(run_id=str(len(snapshots)), tests_dir=tmp_path / str(len(snapshots)))
    executions = []
    def runner(config):
        executions.append(config)
        run = run_data(closes and len(executions) > 1)
        return RunResult('completed', 0, .1, tests=tuple(Result(t['nodeid'], 'passed') for t in run['tests']),
                         http_observations=tuple(run['http_observations']))
    result = run_generation_pipeline(GenerationSettings(path, base_url='http://api', output_dir=tmp_path / 'runs',
        max_coverage_attempts=limit), model=model, run_tests_fn=runner, save_fn=save)
    assert result.status == ('success' if closes and limit and not overwrite else 'incomplete_inputs')
    assert model.calls <= 1 + limit
    assert result.total_tokens == model.calls * 30
    assert all(s['test_initial.py'] == snapshots[0]['test_initial.py'] for s in snapshots)
    if result.status == 'success':
        assert len(snapshots) == 2 and len(snapshots[-1]) == 2
        assert result.provenance['input_coverage_history'][0]['operations'][0]['missing'] == ['tags:outside_maxItems']
