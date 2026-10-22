"""Measured JSON-body conditions; never infer coverage from test names.

The first implementation measures flat OAS 3.0 object inputs. Unsupported
shapes are reported explicitly. The independent benchmark audit stays separate.
"""
import json
from jsonschema import Draft4Validator
from ..parser.contract import normalize_contract, HTTP_METHODS

BOUNDS = ('minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems')


def _supported(schema):
    if not isinstance(schema, dict):
        return False
    if any(key in schema for key in ('$ref', 'allOf', 'anyOf', 'oneOf', 'not', 'nullable',
                                      'exclusiveMinimum', 'exclusiveMaximum')):
        return False
    if schema.get('type') == 'array':
        return _supported(schema.get('items', {}))
    return schema.get('type') in ('string', 'integer', 'number', 'boolean')


def input_coverage(contract, run):
    contract = normalize_contract(contract)
    run = run or {}
    passed = {t['nodeid'] for t in run.get('tests', []) if t.get('outcome') == 'passed'}
    available = (run.get('status') == 'completed' and run.get('http_observations_complete') is True
                 and isinstance(run.get('http_observations'), (list, tuple)))
    rows, limitations = [], []
    for path, item in contract.get('paths', {}).items():
        for method, op in item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue
            body = op.get('requestBody', {})
            if not body:
                continue
            label = f'{method.upper()} {path}'
            schema = body.get('content', {}).get('application/json', {}).get('schema', {})
            props = schema.get('properties', {})
            if (not str(contract.get('openapi', '')).startswith('3.0') or '{' in path
                or item.get('parameters') or op.get('parameters') or schema.get('type') != 'object'
                or any(k in schema for k in ('$ref', 'allOf', 'anyOf', 'oneOf', 'not', 'nullable'))
                or not props or not all(_supported(p) for p in props.values())):
                limitations.append(label + ': JSON input shape/parameters not supported by measured coverage')
                continue
            validator = Draft4Validator(schema)
            events = [e for e in run.get('http_observations') or [] if e.get('nodeid') in passed
                      and e.get('method') == method.upper() and e.get('path') == path]
            trace_ok = available and all(e.get('request_body', {}).get('kind') in ('json', 'absent', 'malformed') for e in events)
            valid, invalid = [], []
            for event in events:
                request = event.get('request_body', {})
                if request.get('kind') != 'json':
                    continue
                value = request['value']
                errors = list(validator.iter_errors(value))
                code = event['status_code']
                if str(code) not in op.get('responses', {}):
                    continue
                if code // 100 == 2 and not errors:
                    valid.append(value)
                elif code // 100 == 4 and errors:
                    invalid.append((value, errors))
            checks = {'valid_request': bool(valid)}
            if body.get('required'):
                checks['missing_body'] = any(e.get('request_body', {}).get('kind') == 'absent'
                    and e['status_code'] // 100 == 4 and str(e['status_code']) in op.get('responses', {}) for e in events)
            for field, prop in props.items():
                def isolated(predicate, validators=None):
                    for value, errors in invalid:
                        if not isinstance(value, dict) or not predicate(value):
                            continue
                        # Required errors have the containing object path.
                        if all((list(err.path)[:1] == [field] or
                                (err.validator == 'required' and not err.path and
                                 set(schema.get('required', [])) - value.keys() == {field}))
                               and (validators is None or err.validator in validators) for err in errors):
                            return True
                    return False
                if field in schema.get('required', []):
                    checks[field + ':omit_required'] = isolated(lambda v: field not in v, {'required'})
                else:
                    checks[field + ':omit_optional'] = any(field not in v for v in valid)
                typecheck = Draft4Validator({'type': prop['type']})
                checks[field + ':invalid_type'] = isolated(lambda v: field in v and not typecheck.is_valid(v[field]))
                for bound in BOUNDS:
                    if bound not in prop:
                        continue
                    threshold = prop[bound]
                    def measure(v):
                        value = v.get(field)
                        if not typecheck.is_valid(value):
                            return None
                        return value if bound in ('minimum', 'maximum') else len(value)
                    checks[field + ':' + bound] = any(measure(v) == threshold for v in valid)
                    if bound in ('minLength', 'minItems') and threshold == 0:
                        continue
                    checks[field + ':outside_' + bound] = isolated(
                        lambda v: measure(v) is not None and
                        (measure(v) < threshold if bound.startswith('min') else measure(v) > threshold), {bound})
            missing = [key for key, observed in checks.items() if not observed]
            rows.append({'operation': label, 'checks': checks, 'missing': missing,
                         'status': 'unavailable' if not trace_ok else 'incomplete' if missing else 'observed'})
    states = [r['status'] for r in rows]
    status = ('unavailable' if 'unavailable' in states else 'incomplete' if 'incomplete' in states
              else 'observed' if rows else 'not_applicable')
    return {'status': status, 'operations': rows, 'limitations': limitations,
            'scope': 'Flat OpenAPI 3.0 JSON objects: valid/omitted inputs, field types and numeric/length boundaries; passing tests and isolated violations only. Other conditions and response assertions are not measured.'}


def supplement(model, contract, coverage, existing):
    """One append-only model call. Never allow replacements or custom fixtures."""
    from .generate import parse_model_content, request_usage, _custom_fixtures, _duplicate_tests, _empty_tests
    from .prompts import GenerationOutput, GENERATION_SYSTEM_PROMPT
    from ..storage import GeneratedFile
    from ..postprocess.fixes import prepare_file
    from .context import render_contract_for_generation
    prompt = ('Supplement an existing passing pytest suite. Return ONLY new files in the usual JSON files schema. '
              'Do not reproduce or modify existing files. Use unique test_input_supplement_*.py filenames. '
              'Use trusted api_client/base_url fixtures. Generate tests for EACH missing condition below. '
              'Negative boundary tests MUST violate only that boundary, with all other fields and item enums valid. '
              'For arrays repeat valid enum items unless uniqueItems forbids it. Assert the specific documented '
              'status and response schema/content. Do not use jsonschema (not installed in runner). '
              'Use test_normal_, test_boundary_, test_error_ names as appropriate.\n'
              + 'Existing filenames: ' + json.dumps(sorted(existing)) + '\nMissing conditions: '
              + json.dumps([{'operation': r['operation'], 'missing': r['missing']} for r in coverage['operations'] if r['missing']])
              + '\nContract:\n' + render_contract_for_generation(contract, max_chars=0))
    tokens = (0, 0, 0)
    try:
        message = model.invoke([{'role': 'system', 'content': GENERATION_SYSTEM_PROMPT},
                                {'role': 'user', 'content': prompt}])
        tokens = request_usage(message)
        if (getattr(message, 'response_metadata', None) or {}).get('finish_reason') == 'length':
            raise ValueError('supplement output truncated')
        output = parse_model_content(message.content, GenerationOutput)
        files = {}
        for spec in output.files:
            file = GeneratedFile(spec.name, spec.code)
            if file.name in existing or file.name in files:
                raise ValueError('supplement may not replace existing files or duplicate names')
            if _custom_fixtures(file.code) or _duplicate_tests(file.code) or _empty_tests(file.code):
                raise ValueError('supplement contains forbidden fixtures/duplicate/empty tests')
            prepared = prepare_file(file.name, file.code)
            if prepared.status == 'syntax_error' or not prepared.test_cases:
                raise ValueError('supplement must contain executable test definitions')
            files[file.name] = prepared.code
        if not files:
            raise ValueError('empty supplement')
        return files, tokens, None
    except Exception as exc:
        from ..llm.diagnostics import describe_error
        return {}, tokens, describe_error(exc)
