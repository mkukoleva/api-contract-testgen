"""Guard the independent acceptance oracle against false coverage claims."""
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft4Validator

ROOT = Path(__file__).resolve().parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = load('control_acceptance', 'benchmark/generation/control/acceptance.py')
server = load('control_server', 'benchmark/generation/control/server.py')


def test_input_audit_does_not_accept_other_route_or_mixed_violation(tmp_path):
    contract = json.loads(acceptance.write_contracts(tmp_path)[0].read_text())
    base = {'quantity': 1, 'unit_price': 10, 'tags': []}
    events = [
        {'method': 'POST', 'path': '/quote', 'status': 200, 'body': base},
        {'method': 'POST', 'path': '/quote', 'status': 422,
         'body': {**base, 'tags': ['bad'] * 4}},
        {'method': 'POST', 'path': '/quote/confirm', 'status': 422,
         'body': {**base, 'tags': ['gift'] * 4}},
    ]
    result = acceptance.audit_inputs(contract, events, '/quote')
    assert result['checks']['valid_request']
    assert not result['checks']['tags:outside_maxItems']
    assert not result['passed']
    events.append({'method': 'POST', 'path': '/quote', 'status': 422,
                   'body': {**base, 'tags': ['gift'] * 4}})
    assert acceptance.audit_inputs(contract, events, '/quote')['checks']['tags:outside_maxItems']


@pytest.mark.parametrize('version', [1, 2])
def test_changed_contract_matches_control_service_and_requires_new_fields(tmp_path, version):
    contract = json.loads(acceptance.write_contracts(tmp_path)[version - 1].read_text())
    schema = contract['components']['schemas']['QuoteInput']
    route = '/quote' if version == 1 else '/quote-v2'
    assert set(contract['paths']) == {route, route + '/confirm'}
    body = {'quantity': 2, 'unit_price': 7.5}
    if version == 2:
        body['currency'] = 'RUB'
    status, result = server.calculate_quote(body, version)
    assert status == 200 and result['total'] == 15
    assert Draft4Validator(schema).is_valid(body)
    assert Draft4Validator(contract['components']['schemas']['Quote']).is_valid(result)
    bad = {**body, 'quantity': 11 if version == 1 else 6}
    assert not Draft4Validator(schema).is_valid(bad)
    assert server.calculate_quote(bad, version)[0] == 422
    if version == 2:
        body.pop('currency')
        assert not Draft4Validator(schema).is_valid(body)
        assert server.calculate_quote(body, version)[0] == 422
