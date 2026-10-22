"""Regression cases confirmed by the independent adversarial audits."""
import copy
import json
from pathlib import Path

import pytest

from prototype.evaluate.mutation import generate_response_plans
from prototype.generator.contracts import GenerationRun
from prototype.generator.stories import check_story_code
from prototype.generator.transport_examples import normalize_transport_examples
from prototype.generator.unit import _targets_source
from prototype.reports.tracing import build_result_trace
from prototype.storage import GeneratedFile


@pytest.mark.parametrize('body', [
    'def helper():\n    return double(3)\ndef test_x():\n    assert 1 == 1',
    'def test_x():\n    double = lambda x: 6\n    assert double(3) == 6',
    'def test_x():\n    try:\n        double(3)\n    except RuntimeError:\n        pass\n    assert 1 == 1',
    'def test_x():\n    double(3)\n    assert 1 == 1',
    'def test_x():\n    return\n    assert double(3) == 6',
    'def test_x():\n    result = double(3)\n    result = 6\n    assert result == 6',
    'def test_x():\n    result = double(3)\n    assert result == result',
    'def test_x():\n    assert True or double(3) == 6',
])
def test_unit_rejects_no_executed_source_result_check(body):
    assert not _targets_source('from unit_source import double\n' + body, ('double',))


@pytest.mark.parametrize('body', [
    'def test_x():\n    assert double(3) == 6',
    'def test_x():\n    result = double(3)\n    assert result == 6',
    'def test_x():\n    status, result = double(3)\n    assert status == 200\n    assert result["total"] == 6',
    'class TestMath:\n    def test_x(self):\n        assert double(3) == 6',
    'def test_x():\n    with pytest.raises(ValueError):\n        double(-1)',
])
def test_unit_retains_direct_result_and_exception_tests(body):
    assert _targets_source('import pytest\nfrom unit_source import double\n' + body, ('double',))


def test_unit_checks_each_test_not_just_one_good_function():
    code = 'from unit_source import double\ndef test_good():\n    assert double(3) == 6\ndef test_bad():\n    assert 1 == 1'
    assert not _targets_source(code, ('double',))


def test_equivalent_percent_encoded_ref_has_identical_mutations():
    contract = {'openapi': '3.0.3', 'info': {'title': 'Price', 'version': '1'},
                'paths': {'/price': {'get': {'responses': {'200': {
                    'description': 'OK', 'content': {'application/json': {
                        'schema': {'$ref': '#/components/schemas/Price'}}}}}}}},
                'components': {'schemas': {'Price': {'type': 'object', 'required': ['price'],
                    'properties': {'price': {'type': 'number'}}}}}}
    encoded = copy.deepcopy(contract)
    encoded['paths']['/price']['get']['responses']['200']['content']['application/json']['schema']['$ref'] = '#/components/schemas/%50rice'
    plain = generate_response_plans(contract)
    assert len(plain.plans) == 4
    assert generate_response_plans(encoded).to_dict() == plain.to_dict()


@pytest.mark.parametrize('finalbody', ['return', 'raise RuntimeError()', 'if True:\n            return'])
def test_story_cannot_validate_steps_after_finally_control_flow(finalbody):
    story = {'story': 'Two steps', 'steps': [
        {'endpoint': 'GET /a', 'expected_status': 200, 'expected_result': 'a'},
        {'endpoint': 'GET /b', 'expected_status': 200, 'expected_result': 'b'}], 'final_goal': 'b.id=1'}
    code = f'''def test_story_chain(base_url, api_client):
    try:
        first = api_client.get(base_url + '/a')
        assert first.status_code == 200
    finally:
        {finalbody}
    second = api_client.get(base_url + '/b')
    assert second.status_code == 200
    assert second.json()['id'] == 1
'''
    assert check_story_code((GeneratedFile('test_story.py', code),), story) is not None


@pytest.mark.parametrize('label', ['missing body, not malformed JSON', 'not malformed JSON',
                                   'no malformed JSON', 'missing body (malformed JSON)'])
def test_transport_preserves_missing_or_negated_malformed_examples(label):
    code = f'''import pytest
@pytest.mark.parametrize('payload, desc', [('', {label!r})])
def test_error(base_url, api_client, payload, desc):
    response = api_client.post(base_url, data=payload)
    assert response.status_code == 422
'''
    assert normalize_transport_examples(code) == (code, [])


def test_transport_ignores_uncalled_raw_helper():
    code = '''import pytest
@pytest.mark.parametrize('payload, desc', [('', 'malformed JSON')])
def test_error(base_url, api_client, payload, desc):
    def never_called():
        if isinstance(payload, str):
            return api_client.post(base_url, data=payload)
    response = api_client.post(base_url, json=payload)
    assert response.status_code == 422
'''
    assert normalize_transport_examples(code) == (code, [])


@pytest.mark.parametrize('class_name', ['', 'TestRoutes::'])
def test_tracing_matches_file_class_and_all_parameterized_results(tmp_path, class_name):
    contract = {'openapi': '3.0.3', 'info': {'title': 'Trace', 'version': '1'},
                'paths': {path: {'get': {'responses': {'200': {'description': 'OK'}}}}
                          for path in ('/a', '/b')}}
    source = tmp_path / 'contract.json'
    source.write_text(json.dumps(contract))
    tests = tmp_path / 'tests'
    tests.mkdir()
    results = []
    for file, route, outcome in [('test_a.py', '/a', 'passed'), ('test_b.py', '/b', 'failed')]:
        code = f'def test_shared(base_url, api_client):\n    response = api_client.get(f"{{base_url}}{route}")\n    assert response.status_code == 200\n'
        if class_name:
            code = 'class TestRoutes:\n' + '\n'.join('    ' + line for line in code.splitlines())
        (tests / file).write_text(code)
        for parameter in ('one', 'two'):
            results.append({'nodeid': f'{file}::{class_name}test_shared[{parameter}]', 'outcome': outcome})
    run = GenerationRun('suspected_defect', source, 'scripted', 0, 0, 0, 0,
                        saved_versions=({'tests_dir': str(tests)},),
                        run_result={'status': 'completed', 'exit_code': 1, 'tests': results})
    trace = build_result_trace(run)
    assert 'trace_error' not in trace
    assert len(trace['tests']) == 4
    for row in trace['tests']:
        expected = ('GET /a', 'passed') if row['nodeid'].startswith('test_a.py') else ('GET /b', 'failed')
        assert (row['operations'], row['outcome']) == ([expected[0]], expected[1])


@pytest.mark.parametrize('extra, covered, uncovered', [
    ('', 2, ['GET /b -> 200', 'GET /b -> 404']),
    ('    second = api_client.get(f"{base_url}/b")\n    assert second.json()["value"] == 404\n',
     2, ['GET /b -> 200', 'GET /b -> 404']),
    ('    second = api_client.get(f"{base_url}/b")\n    assert second.status_code == 200\n',
     3, ['GET /b -> 404']),
    ('    second = api_client.get(f"{base_url}/b")\n    assert second.status_code == 404\n',
     3, ['GET /b -> 200']),
])
def test_response_code_coverage_counts_operation_status_pairs(tmp_path, extra, covered, uncovered):
    contract = {'openapi': '3.0.3', 'info': {'title': 'Pairs', 'version': '1'},
                'paths': {path: {'get': {'responses': {code: {'description': 'Response'}
                    for code in ('200', '404')}}} for path in ('/a', '/b')}}
    source = tmp_path / 'contract.json'
    source.write_text(json.dumps(contract))
    tests = tmp_path / 'tests'
    tests.mkdir()
    code = ('def test_routes(base_url, api_client):\n'
            '    first = api_client.get(f"{base_url}/a")\n'
            '    assert first.status_code == 200\n'
            '    first = api_client.get(f"{base_url}/a")\n'
            '    assert 404 == first.status_code\n' + extra)
    (tests / 'test_routes.py').write_text(code)
    run = GenerationRun('success', source, 'scripted', 0, 0, 0, 0,
                        saved_versions=({'tests_dir': str(tests)},),
                        run_result={'status': 'completed', 'exit_code': 0, 'tests': [
                            {'nodeid': 'test_routes.py::test_routes', 'outcome': 'passed'}]})
    trace = build_result_trace(run)
    assert 'trace_error' not in trace
    summary = trace['summary']
    assert summary['response_codes_total'] == 4
    assert summary['response_codes_covered'] == covered
    assert summary['response_codes_uncovered'] == uncovered
    assert 'static' in summary['response_codes_basis']
    from prototype.reports.generation_report import save_generation_report
    reports = save_generation_report(run, tmp_path / 'reports')
    markdown = Path(reports['markdown']).read_text()
    assert f'Пары операция + HTTP-код (статические проверки) | 4 | {covered} |' in markdown
