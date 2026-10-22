"""Normalize provably empty raw examples explicitly labelled malformed JSON.

This changes request data, never expected responses. It does not infer service
behaviour from a failing response. Missing-body cases and json= values stay intact.
"""
import ast
import re


def _body_nodes(node):
    """Walk the test body, excluding nested scopes and their dead calls."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _body_nodes(child)


def normalize_transport_examples(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    replacements = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or not function.name.startswith('test_'):
            continue
        raw_names = set()
        json_names = set()
        for call in _body_nodes(function):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == 'api_client'
                    and call.func.attr in {'post', 'put', 'patch', 'request', 'delete'}):
                continue
            for keyword in call.keywords:
                if isinstance(keyword.value, ast.Name):
                    if keyword.arg == 'data':
                        raw_names.add(keyword.value.id)
                    elif keyword.arg == 'json':
                        json_names.add(keyword.value.id)
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Attribute)
                    and isinstance(decorator.func.value.value, ast.Name)
                    and decorator.func.value.value.id == 'pytest'
                    and decorator.func.value.attr == 'mark' and decorator.func.attr == 'parametrize'
                    and len(decorator.args) >= 2):
                continue
            names, rows = decorator.args[:2]
            if not (isinstance(names, ast.Constant) and isinstance(names.value, str)
                    and isinstance(rows, (ast.List, ast.Tuple))):
                continue
            names = [name.strip() for name in names.value.split(',')]
            for row in rows.elts:
                if not isinstance(row, (ast.List, ast.Tuple)) or len(row.elts) != len(names):
                    continue
                labels = [v.value.lower() for n, v in zip(names, row.elts)
                          if n in {'desc', 'description', 'case', 'case_name'}
                          and isinstance(v, ast.Constant) and isinstance(v.value, str)]
                if (not any('malformed json' in label for label in labels)
                        or any(re.search(r'\b(?:not|no|without)\b|missing\s+body', label) for label in labels)):
                    continue
                for name, value in zip(names, row.elts):
                    # A common dispatch pattern uses data= for strings and
                    # json= for objects under the same parameter. Only an
                    # explicit string type branch establishes raw transport.
                    if name not in raw_names or not isinstance(value, ast.Constant) or value.value != '':
                        continue
                    if name in json_names:
                        raw_branch = any(isinstance(node, ast.If)
                            and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name)
                            and node.test.func.id == 'isinstance' and len(node.test.args) == 2
                            and isinstance(node.test.args[0], ast.Name) and node.test.args[0].id == name
                            and isinstance(node.test.args[1], ast.Name) and node.test.args[1].id == 'str'
                            and any(isinstance(k, ast.keyword) and k.arg == 'data'
                                    and isinstance(k.value, ast.Name) and k.value.id == name
                                    for stmt in node.body for k in ast.walk(stmt)) for node in _body_nodes(function))
                        if not raw_branch:
                            continue
                    replacements.append((value, function.name))
    lines = code.encode('utf-8').splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    raw = code.encode('utf-8')
    edits = [(offsets[node.lineno-1] + node.col_offset, offsets[node.end_lineno-1] + node.end_col_offset, name)
             for node, name in replacements]
    for start, end, _ in sorted(edits, reverse=True):
        raw = raw[:start] + b'"{"' + raw[end:]
    return raw.decode('utf-8'), [{'test': name, 'change': 'empty raw malformed-JSON example replaced with nonempty invalid JSON',
                                 'expected_responses_unchanged': True} for _, _, name in edits]
