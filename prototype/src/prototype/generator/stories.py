"""Load and check a user story against the contract without running test code."""

import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .context import find_operation
from ..parser.contract import normalize_contract

MAX_STORY_BYTES = 64 * 1024


def load_user_story(path, contract):
    """Validate local JSON; bindings describe prior response JSON pointers.

    The legacy user-story tool's story/steps/endpoint/expected_result/final_goal
    format is accepted. Optional status and bindings make expectations explicit.
    No URL, network or runner configuration is accepted from a story.
    """
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_STORY_BYTES + 1)
    if len(raw) > MAX_STORY_BYTES:
        raise ValueError("user story exceeds 64 KiB")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("user story must contain valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("user story must be a JSON object")
    for field in ("story", "final_goal"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"user story requires non-empty {field}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not 2 <= len(steps) <= 20:
        raise ValueError("user story requires 2 to 20 ordered steps")
    # Operation lookup and shared parameter bindings must use the same
    # expanded contract, including local references on the path item.
    contract = normalize_contract(contract)
    canonical = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"story step {index + 1} must be an object")
        endpoint = step.get("endpoint", "")
        parts = endpoint.split() if isinstance(endpoint, str) else []
        if len(parts) != 2:
            raise ValueError(f"story step {index + 1} requires endpoint 'METHOD /path'")
        method, route = parts[0].upper(), parts[1]
        parsed = urlsplit(route)
        if not route.startswith("/") or route.startswith("//") or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("story endpoints must be contract paths, without host/query/fragment")
        found = find_operation(contract, method, route)
        if found is None:
            raise ValueError(f"story step {index + 1}: undocumented operation {method} {route}")
        if not isinstance(step.get("expected_result"), str) or not step["expected_result"].strip():
            raise ValueError(f"story step {index + 1} requires expected_result")
        entry = {"endpoint": f"{method} {route}", "expected_result": step["expected_result"]}
        status = step.get("expected_status")
        if status is not None:
            response_map = found[1].get("responses", {})
            responses = {str(key).upper() for key in response_map} if isinstance(response_map, dict) else set()
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("story expected_status must be an HTTP status integer")
            if str(status) not in responses and f"{status // 100}XX" not in responses and "DEFAULT" not in responses:
                raise ValueError(f"story status {status} is not documented for {method} {route}")
            entry["expected_status"] = status
        bindings = step.get("bindings", [])
        if not isinstance(bindings, list):
            raise ValueError("story bindings must be a list")
        entry["bindings"] = []
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValueError("story binding must be an object")
            source, pointer, target = (binding.get(key) for key in ("from_step", "pointer", "target"))
            if type(source) is not int or not 0 <= source < index:
                raise ValueError("story binding must refer to a previous step (zero-based index)")
            if (not isinstance(pointer, str) or (pointer and not pointer.startswith("/"))
                or re.search(r"~(?![01])", pointer)):
                raise ValueError("story binding pointer must be a JSON pointer")
            if not isinstance(target, str) or ":" not in target:
                raise ValueError("story binding target must be path:name, query:name, header:name or body:/pointer")
            location, name = target.split(":", 1)
            if not name or location not in {"path", "query", "header", "body"}:
                raise ValueError("invalid story binding target")
            if location == "path" and "{" + name + "}" not in found[0]:
                raise ValueError(f"story binding refers to unknown path parameter {name}")
            if location in {"query", "header"}:
                shared = contract.get("paths", {}).get(found[0], {}).get("parameters", [])
                local = found[1].get("parameters", [])
                if not isinstance(shared, list) or not isinstance(local, list):
                    raise ValueError("story operation parameters must be a list")
                params = [*shared, *local]
                if not any(isinstance(p, dict) and p.get("in") == location and p.get("name") == name
                           for p in params):
                    raise ValueError(f"story binding refers to undocumented {location} parameter {name}")
            if location == "body" and (not name.startswith("/") or re.search(r"~(?![01])", name)):
                raise ValueError("story body target must contain a JSON pointer")
            if location == "body":
                has_body = "requestBody" in found[1] or any(
                    isinstance(parameter, dict) and parameter.get("in") == "body"
                    for parameter in found[1].get("parameters", []) or [])
                if not has_body:
                    raise ValueError("story binding requires a documented request body")
            entry["bindings"].append({"from_step": source, "pointer": pointer, "target": target})
        canonical.append(entry)
    return {"story": data["story"], "steps": canonical, "final_goal": data["final_goal"],
            "source": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def check_story_code(files, story):
    """Check the minimum executable chain shape, not semantic correctness.

    A single test_story_* must contain the ordered direct HTTP calls and
    assertions for steps and the final goal. Assertions still need execution;
    the static check cannot establish that the story's goal is true.
    """
    expected = [step["endpoint"].split() for step in story["steps"]]
    for file in files:
        try:
            tree = ast.parse(file.code)
        except (SyntaxError, ValueError):
            continue
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef) or not function.name.startswith("test_story_"):
                continue
            calls, assertions, variables = [], [], {}
            statements = list(_story_statements(function.body))
            for statement in statements:
                if isinstance(statement, ast.Assert) and any(isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call))
                                                            for node in ast.walk(statement.test)):
                    assertions.append(statement.lineno)
                for node in _expression_nodes(statement):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "api_client"):
                        method = node.func.attr.lower()
                        kwargs = {kw.arg: kw.value for kw in node.keywords}
                        if method == "request":
                            method_node = node.args[0] if node.args else kwargs.get("method")
                            method = str(method_node.value).lower() if isinstance(method_node, ast.Constant) else ""
                            url = node.args[1] if len(node.args) > 1 else kwargs.get("url")
                        else:
                            url = node.args[0] if node.args else kwargs.get("url")
                        if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                            calls.append((node.lineno, node.col_offset, method, _url_shape(url, variables)))
                if isinstance(statement, ast.Assign):
                    shape = _url_shape(statement.value, variables)
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            variables[target.id] = shape
            calls.sort()
            args = {arg.arg for arg in function.args.args}
            matches = len(calls) >= len(expected)
            for call, (method, route) in zip(calls, expected):
                pattern = re.escape(route)
                pattern = re.sub(r"\\\{[^{}]+\\\}", "[^/]+", pattern)
                matches = matches and call[2] == method.lower() and re.fullmatch("BASE" + pattern, call[3]) is not None
            checked_steps = all(any(line >= call[0] for line in assertions) for call in calls[:len(expected)])
            if ({"base_url", "api_client"} <= args and matches and checked_steps and
                len(assertions) >= len(expected) + 1 and _checks_response_chain(statements, story)):
                return None
    return "user story requires one test_story_* using trusted fixtures, ordered HTTP steps, response assertions and declared data bindings for each step and final goal"


def _checks_response_chain(statements, story):
    """Conservative data-flow gate for direct response/json assignments.

    Track exact JSON-pointer extraction through aliases. Unsupported dynamic
    extraction must be rewritten into direct assignments; never execute code
    or claim to prove the meaning of the story's natural-language goal.
    """
    variables, responses, checks, calls = {}, {}, [], []

    def sources(node):
        if node is None:
            return set()
        if isinstance(node, ast.Name):
            return variables.get(node.id, set())
        if id(node) in responses:
            return {(responses[id(node)], ('@response',))}
        if isinstance(node, ast.Attribute):
            return {(step, ('@status',)) for step, path in sources(node.value)
                    if path == ('@response',) and node.attr == 'status_code'}
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return {(step, (*path, str(node.slice.value))) for step, path in sources(node.value)
                    if not path or not path[0].startswith('@')}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'json' and not node.args and not node.keywords:
                return {(step, ()) for step, path in sources(node.func.value) if path == ('@response',)}
            if node.func.attr == 'get' and node.args and isinstance(node.args[0], ast.Constant):
                return {(step, (*path, str(node.args[0].value))) for step, path in sources(node.func.value)
                        if not path or not path[0].startswith('@')}
        return set().union(*(sources(child) for child in ast.iter_child_nodes(node)))

    def target_value(node, target):
        location, name = target.split(':', 1)
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        if location == 'path':
            # Route shape/order is checked separately. Require the exact prior
            # JSON value in the URL expression, not merely anywhere in the call.
            request = node.func.attr == 'request'
            index = 1 if request else 0
            return node.args[index] if len(node.args) > index else kwargs.get('url')
        value = kwargs.get({'query': 'params', 'header': 'headers', 'body': 'json'}.get(location))
        if location == 'body':
            if name == '':
                return value
            parts = [part.replace('~1', '/').replace('~0', '~') for part in name.lstrip('/').split('/')]
        else:
            parts = [name]
        for part in parts:
            if isinstance(value, ast.Name):
                value = assigned.get(value.id)
            if not isinstance(value, ast.Dict):
                return None
            value = next((child for key, child in zip(value.keys, value.values)
                          if isinstance(key, ast.Constant) and str(key.value) == part), None)
        return value

    assigned = {}
    for statement in statements:
        for node in _expression_nodes(statement):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'api_client'):
                continue
            if node.func.attr not in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace', 'request'}:
                continue
            index = len(calls)
            if index >= len(story['steps']):
                return False
            for binding in story['steps'][index].get('bindings', []):
                pointer = tuple(part.replace('~1', '/').replace('~0', '~')
                                for part in binding['pointer'].split('/')[1:])
                if (binding['from_step'], pointer) not in sources(target_value(node, binding['target'])):
                    return False
            responses[id(node)] = index
            calls.append(statement.lineno)
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    variables[target.id] = sources(statement.value)
                    assigned[target.id] = statement.value
        if isinstance(statement, ast.Assert):
            # Bare truthiness/constant/self-equality cannot validate a response.
            comparisons = [node for node in ast.walk(statement.test) if isinstance(node, ast.Compare)
                           and any(ast.dump(node.left) != ast.dump(other) for other in node.comparators)]
            if comparisons and not any(isinstance(node, (ast.IfExp, ast.Lambda)) or
                    (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)) for node in ast.walk(statement.test)):
                checks.append((statement.lineno, sources(statement.test), [
                    (comparison, sources(comparison.left), sources(comparison.comparators[0]))
                    for comparison in comparisons]))
    if len(calls) != len(story['steps']):
        return False
    for index, line in enumerate(calls):
        end = calls[index + 1] if index + 1 < len(calls) else float('inf')
        expected_status = story['steps'][index].get('expected_status')
        def checks_status(comparisons):
            if expected_status is None:
                return True
            for comparison, left_sources, right_sources in comparisons:
                if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
                    continue
                left, right = comparison.left, comparison.comparators[0]
                for origins, literal in ((left_sources, right), (right_sources, left)):
                    if ((index, ('@status',)) in origins and isinstance(literal, ast.Constant)
                            and literal.value == expected_status):
                        return True
            return False
        if not any(line <= check_line < end and (index, ('@status',)) in origins and checks_status(comparisons)
                   for check_line, origins, comparisons in checks):
            return False
    final = len(calls) - 1
    # The final goal must compare final payload data. A status-only assertion
    # or an unrelated base_url assertion does not establish a business result.
    return any(line >= calls[-1] and any(step == final and (not path or not path[0].startswith('@'))
               for step, path in origins) for line, origins, _ in checks)


def _story_statements(statements):
    """Only the unconditional sequence counts; branch/helper calls do not."""
    for statement in statements:
        if isinstance(statement, ast.Try):
            # A finally block may return, raise or suppress an assertion. Do
            # not infer an unconditional story through such control flow.
            if statement.finalbody:
                break
            # Exceptions may be re-raised, never swallowed to pass a story.
            if any(not handler.body or not isinstance(handler.body[-1], ast.Raise) for handler in statement.handlers):
                continue
            yield from _story_statements(statement.body)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Assert)):
            yield statement
        elif isinstance(statement, (ast.Return, ast.Raise)):
            break


def _expression_nodes(statement):
    """Do not count statements hidden in lambdas, comprehensions or helpers."""
    yield statement
    for node in ast.iter_child_nodes(statement):
        if isinstance(node, (ast.Lambda, ast.IfExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        yield from _expression_nodes(node)


def _url_shape(node, variables):
    """Retain literal route parts; response-derived values stay dynamic."""
    if isinstance(node, ast.Name):
        return "BASE" if node.id == "base_url" else variables.get(node.id, "DYNAMIC")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _url_shape(node.left, variables) + _url_shape(node.right, variables)
    if isinstance(node, ast.JoinedStr):
        return "".join(_url_shape(part.value, variables) if isinstance(part, ast.FormattedValue)
                       else _url_shape(part, variables) for part in node.values)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rstrip" and not node.keywords
            and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "/"):
        shape = _url_shape(node.func.value, variables)
        return "BASE" if shape == "BASE" else "DYNAMIC"
    return "DYNAMIC"
