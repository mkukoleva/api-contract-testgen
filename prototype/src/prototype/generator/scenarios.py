"""Plan the three test categories from contract facts, without executing code.

A plan describes requested tests, not measured coverage or generated tests.
Undocumented error expectations are explicitly unavailable.
"""

import math
import json

from ..parser.contract import HTTP_METHODS, normalize_contract


def _required_fields(schema):
    """Required fields contributed by every allOf branch on this object."""
    if not isinstance(schema, dict):
        return set()
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
        raise ValueError("schema.required must be a list of field names")
    combined = set(required)
    alternatives = schema.get("allOf", [])
    if not isinstance(alternatives, list):
        raise ValueError("schema.allOf must be a list")
    for branch in alternatives:
        combined.update(_required_fields(branch))
    return combined


def _input_checks(schema, target, boundary, errors, inherited_required=(), parent_constraints=()):
    """Describe input variations; keep values symbolic for complex schemas."""
    if not isinstance(schema, dict) or "$ref" in schema:
        return
    required = _required_fields(schema) | set(inherited_required)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            if key.startswith("exclusive") and isinstance(schema[key], bool):
                continue  # Swagger 2/OAS 3.0: exclusivity modifies min/max.
            value = schema[key]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"schema.{key} must be a finite number")
            if key in {"minLength", "maxLength", "minItems", "maxItems"} and (type(value) is not int or value < 0):
                raise ValueError(f"schema.{key} must be a non-negative integer")
            boundary.append({"target": target, "check": "valid_boundary",
                             "constraint": key, "value": schema[key],
                             "exclusive": key.startswith("exclusive") or
                             schema.get("exclusive" + key.capitalize()) is True})
            errors.append({"target": target, "check": "outside_boundary",
                           "constraint": key, "value": schema[key]})
    kind = schema.get("type")
    empty_forbidden = ((kind == "object" and bool(required)) or
                       (kind == "string" and schema.get("minLength", 0) > 0) or
                       (kind == "array" and schema.get("minItems", 0) > 0))
    if kind in ("string", "array", "object") and not target.startswith("path:") and not empty_forbidden:
        from jsonschema import Draft4Validator
        empty = {"string": "", "array": [], "object": {}}[kind]
        try:
            # enum/pattern/compositions can forbid empty values even when
            # minLength/minItems are absent. Keep enclosing compositions when
            # walking their branches, but not when entering a new property.
            permitted = all(Draft4Validator(rule).is_valid(empty)
                            for rule in (*parent_constraints, schema))
        except Exception:
            # Unresolved recursive refs or unsupported schema constructs are
            # not evidence that an empty value is a valid positive boundary.
            permitted = False
        if permitted:
            boundary.append({"target": target, "check": "empty_if_allowed"})
    if "type" in schema:
        errors.append({"target": target, "check": "invalid_type",
                       "expected_type": schema["type"]})
    if "enum" in schema:
        boundary.append({"target": target, "check": "enum_values", "values": schema["enum"]})
        errors.append({"target": target, "check": "outside_enum"})
    if "pattern" in schema:
        errors.append({"target": target, "check": "invalid_pattern", "pattern": schema["pattern"]})
    properties = schema.get("properties", {})
    for name, child in properties.items() if isinstance(properties, dict) else ():
        if isinstance(child, dict) and child.get("readOnly"):
            continue
        path = f"{target}.{name}"
        if name in required:
            errors.append({"target": path, "check": "omit_required"})
        else:
            boundary.append({"target": path, "check": "omit_optional"})
        _input_checks(child, path, boundary, errors)
    if "items" in schema:
        _input_checks(schema["items"], target + "[]", boundary, errors)
    for key in ("allOf", "anyOf", "oneOf"):
        alternatives = schema.get(key, [])
        if not isinstance(alternatives, list):
            raise ValueError(f"schema.{key} must be a list")
        for index, child in enumerate(alternatives):
            if key == "allOf":
                _input_checks(child, target, boundary, errors, required, (*parent_constraints, schema))
            else:
                _input_checks(child, f"{target}.{key}[{index}]", boundary, errors,
                              parent_constraints=(*parent_constraints, schema))


def _parameters(node):
    parameters = node.get("parameters", [])
    if not isinstance(parameters, list):
        raise ValueError("operation/path parameters must be a list")
    if not all(isinstance(parameter, dict) for parameter in parameters):
        raise ValueError("operation/path parameters must contain objects")
    return parameters


def build_scenario_plan(contract):
    """Return per-operation normal/boundary/error requests with limitations.

    Response schemas are not input constraints. Operations override shared
    parameters. Error status lists are candidates, never a blanket assertion
    that any documented 4xx is acceptable for any malformed request.
    """
    contract = normalize_contract(contract)
    operations = []
    for route, item in contract.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            boundary, errors, limitations = [], [], []
            parameters = {}
            for parameter in [*_parameters(item), *_parameters(operation)]:
                if isinstance(parameter, dict):
                    parameters[(parameter.get("in"), parameter.get("name"))] = parameter
            for (location, name), parameter in parameters.items():
                target = f"{location}:{name}"
                if parameter.get("required"):
                    if location != "path":
                        errors.append({"target": target, "check": "omit_required"})
                else:
                    boundary.append({"target": target, "check": "omit_optional"})
                # Swagger non-body parameters carry type/constraints directly;
                # their boolean 'required' is not JSON Schema's required list.
                schema = parameter.get("schema", {key: value for key, value in parameter.items() if key != "required"})
                _input_checks(schema, target, boundary, errors)
            body = operation.get("requestBody", {})
            if isinstance(body, dict):
                if body.get("required"):
                    errors.append({"target": "body", "check": "omit_required"})
                elif body:
                    boundary.append({"target": "body", "check": "omit_optional"})
                content_map = body.get("content", {})
                if not isinstance(content_map, dict):
                    raise ValueError("requestBody.content must be an object")
                for media, content in content_map.items():
                    if isinstance(content, dict):
                        _input_checks(content.get("schema"), f"body:{media}", boundary, errors)
            responses = operation.get("responses", {})
            statuses = [str(code) for code in responses] if isinstance(responses, dict) else []
            success = sorted(code for code in statuses if code.startswith("2"))
            error_statuses = sorted(code for code in statuses if code.startswith("4"))
            if "404" in error_statuses and "{" in route:
                errors.append({"target": "resource", "check": "nonexistent_resource"})
            if not success:
                limitations.append("Нет документированного успешного ответа; ожидание нужно уточнить.")
            if not boundary:
                limitations.append("В контракте нет входных данных для обоснованного граничного случая.")
            if not error_statuses:
                limitations.append("Нет документированных 4xx; ожидаемый ответ на невалидный запрос неизвестен.")
            elif not errors:
                limitations.append("4xx описаны, но подходящие негативные входные данные не определены.")
            # allOf may repeat the same constraint. Keep distinct conditions,
            # but do not ask the model to generate identical cases twice.
            boundary = list({json.dumps(case, sort_keys=True): case for case in boundary}.values())
            errors = list({json.dumps(case, sort_keys=True): case for case in errors}.values())
            operations.append({"method": method.upper(), "path": route,
                               "normal": {"check": "valid_request_and_response_schema",
                                          "documented_statuses": success},
                               "boundary": boundary,
                               "error": {"input_variations": errors,
                                         "documented_statuses": error_statuses,
                                         "available": bool(errors and error_statuses)},
                               "limitations": limitations})
    return {"kind": "requested_scenarios", "operations": operations}


def category_evidence(plan, run):
    """Report observed categories per operation, not semantic assertion coverage.

    A test name alone is insufficient: require a completed trusted HTTP trace,
    a passing test, and the documented status family for that category.
    Unsupported expectations remain explicit, never fabricated as coverage.
    """
    from .context import match_contract_path, match_response_definition

    operations = plan.get("operations", [])
    run = run or {}
    available = bool(operations) and (run.get("status") == "completed"
        and run.get("http_observations_complete") is True
        and isinstance(run.get("http_observations"), (list, tuple)))
    passed = {test["nodeid"] for test in run.get("tests", [])
              if test.get("outcome") == "passed"}
    seen = {(op["method"], op["path"]): {name: set() for name in ("normal", "boundary", "error")}
            for op in operations}
    for event in run.get("http_observations") or ():
        nodeid = event.get("nodeid", "")
        if not available or nodeid not in passed:
            continue
        name = nodeid.split("[", 1)[0].split("::")[-1]
        category = next((kind for kind in ("normal", "boundary", "error")
                         if name.startswith(f"test_{kind}_")), None)
        if category is None:
            continue
        method, path = event.get("method", "").upper(), event.get("path", "")
        matches = [op for op in operations if op["method"] == method
                   and match_contract_path(op["path"], path)]
        exact = next((op for op in matches if op["path"] == path), None)
        op = exact or (matches[0] if len(matches) == 1 else None)
        status = event.get("status_code")
        if op is None or type(status) is not int:
            continue
        codes = op["error" if category == "error" else "normal"]["documented_statuses"]
        family = 4 if category == "error" else 2
        if status // 100 == family and match_response_definition(codes, status) is not None:
            seen[(method, op["path"])][category].add(nodeid)
    rows = []
    for op in operations:
        required = {"normal": bool(op["normal"]["documented_statuses"]),
                    "boundary": bool(op["boundary"] and op["normal"]["documented_statuses"]),
                    "error": op["error"]["available"]}
        categories = {}
        for kind, applicable in required.items():
            nodeids = sorted(seen[(op["method"], op["path"])][kind])
            state = ("unavailable" if not available else "observed" if nodeids else
                     "missing" if applicable else "needs_contract_clarification")
            categories[kind] = {"status": state, "nodeids": nodeids}
        rows.append({"operation": f"{op['method']} {op['path']}",
                     "categories": categories, "limitations": op["limitations"]})
    states = [category["status"] for row in rows for category in row["categories"].values()]
    return {"requirement": "4.1.4.1", "status": "unavailable" if not available else
            "incomplete" if "missing" in states else
            "needs_contract_clarification" if "needs_contract_clarification" in states else "observed",
            "operations": rows,
            "scope": "Категория из имени теста + успешный pytest + HTTP-наблюдения. Это не доказательство всех границ, правильности assert или выполнения всего ТЗ."}
