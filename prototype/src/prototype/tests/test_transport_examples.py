import ast
from prototype.generator.transport_examples import normalize_transport_examples

CODE = '''import pytest
@pytest.mark.parametrize("payload, expected_status, desc", [
    (None, 422, "missing body"),
    ("", 400, "empty string body (malformed json)"),
    ("{", 400, "malformed json"),
    ("", 422, "missing body"),
])
def test_error_example(base_url, api_client, payload, expected_status, desc):
    if payload is None:
        response = api_client.post(base_url)
    elif isinstance(payload, str):
        response = api_client.post(base_url, data=payload)
    else:
        response = api_client.post(base_url, json=payload)
    assert response.status_code == expected_status
'''


def test_raw_malformed_example_fixed_without_changing_assertions_or_missing_body():
    fixed, changes = normalize_transport_examples(CODE)
    assert fixed == CODE.replace('("", 400,', '("{", 400,')
    assert len(changes) == 1
    assert ast.dump(ast.parse(fixed).body[1].body[-1]) == ast.dump(ast.parse(CODE).body[1].body[-1])
    assert normalize_transport_examples(fixed) == (fixed, [])


def test_json_serialized_empty_string_must_not_be_changed():
    code = CODE.replace('data=payload', 'json=payload')
    assert normalize_transport_examples(code) == (code, [])


def test_other_or_unproven_transport_is_not_rewritten():
    for code in (CODE.replace('api_client.post', 'other.post'), CODE.replace('isinstance(payload, str)', 'some_predicate(payload)'),
                 CODE.replace('malformed json', 'missing body')):
        assert normalize_transport_examples(code) == (code, [])
