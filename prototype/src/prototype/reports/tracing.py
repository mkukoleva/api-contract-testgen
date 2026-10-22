"""
Result explanation and tracing for generation runs.

Implements TZ v3 section 4.1.5:

4.1.5.1 - API operation/response-code/parameter coverage
4.1.5.2 - trace for every generated test
4.1.5.3 - diagnostics and classification of failed tests

The module does not execute generated tests and does not import them.
It only reads the generated source code using AST and combines it with
the API contract and pytest runner results.
"""

from __future__ import annotations

import ast
import textwrap
import re
from pathlib import Path
from typing import Any

from ..parser.contract import load_contract_document


HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
}


def _load_contract(path: str | Path) -> dict[str, Any]:
    """Load the saved OpenAPI/Swagger contract."""
    document = load_contract_document(path)
    return document.data


def _operation_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def _normalize_path(path: str) -> str:
    """Normalize a concrete URL path to an OpenAPI-style path."""
    if not path:
        return "/"

    path = path.split("?", 1)[0]

    # Replace UUID-like values.
    path = re.sub(
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}",
        "/{id}",
        path,
    )

    # Replace numeric path components.
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)

    return path or "/"


def _path_matches(template: str, concrete: str) -> bool:
    """Check whether a concrete path belongs to an OpenAPI path template."""
    if template == concrete:
        return True

    template_parts = template.strip("/").split("/")
    concrete_parts = concrete.strip("/").split("/")

    if len(template_parts) != len(concrete_parts):
        return False

    for expected, actual in zip(template_parts, concrete_parts):
        if expected.startswith("{") and expected.endswith("}"):
            continue
        if expected != actual:
            return False

    return True


def _extract_string(node: ast.AST | None) -> str | None:
    """Extract a best-effort static string from an AST expression."""
    if node is None:
        return None

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []

        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                # Keep a placeholder for dynamic expressions.
                parts.append("{value}")

        return "".join(parts)

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _extract_string(node.left)
        right = _extract_string(node.right)

        if left is not None and right is not None:
            return left + right

    return None


def _extract_call_path(node: ast.Call) -> str | None:
    """Extract URL/path from api_client.get/post/... call."""
    if not node.args:
        return None

    value = _extract_string(node.args[0])

    if value is None:
        return None

    # Remove common base_url prefix.
    for prefix in (
        "{base_url}",
        "{baseurl}",
        "{api_url}",
        "base_url",
    ):
        value = value.replace(prefix, "")

    if "://" in value:
        match = re.search(r"https?://[^/]+(/.*)?$", value)
        if match:
            value = match.group(1) or "/"

    if not value.startswith("/"):
        slash = value.find("/")
        if slash >= 0:
            value = value[slash:]
        else:
            value = "/" + value

    return _normalize_path(value)


def _extract_parameter_names(node: ast.Call) -> list[str]:
    """Extract parameter names from query/path/header/json/data arguments."""
    names: list[str] = []

    for keyword in node.keywords:
        if keyword.arg in {"params", "headers", "json", "data", "files"}:
            value = keyword.value

            if isinstance(value, ast.Dict):
                for key in value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        names.append(key.value)

    return sorted(set(names))


def _extract_http_calls(source: str) -> list[dict[str, Any]]:
    """Extract HTTP calls from generated test source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    calls: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if not isinstance(node.func, ast.Attribute):
            continue

        method = node.func.attr.lower()

        if method not in HTTP_METHODS:
            continue

        path = _extract_call_path(node)

        if path is None:
            continue

        calls.append(
            {
                "method": method.upper(),
                "path": path,
                "parameters": _extract_parameter_names(node),
                "line": getattr(node, "lineno", None),
            }
        )

    return calls


def _test_category(source: str, function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """
    Determine scenario category from function name/docstring/source.

    The generator is asked to describe scenarios, but this function also
    supports existing generated tests that do not yet have that metadata.
    """
    text_parts = [
        function.name.lower(),
        ast.get_docstring(function) or "",
        source.lower(),
    ]

    text = " ".join(text_parts)

    if any(
        marker in text
        for marker in (
            "negative",
            "error",
            "invalid",
            "not_found",
            "notfound",
            "unauthorized",
            "forbidden",
            "bad_request",
            "ошиб",
            "негатив",
            "404",
            "401",
            "403",
            "400",
        )
    ):
        return "негативный сценарий"

    if any(
        marker in text
        for marker in (
            "boundary",
            "edge",
            "limit",
            "empty",
            "null",
            "миним",
            "максим",
            "гранич",
        )
    ):
        return "граничный сценарий"

    return "штатный сценарий"


def _expected_statuses(function: ast.AST) -> list[str]:
    """Find status codes asserted by the generated test."""
    statuses: set[str] = set()

    for node in ast.walk(function):
        if not isinstance(node, ast.Compare):
            continue

        for value in [node.left, *node.comparators]:
            if isinstance(value, ast.Constant):
                if isinstance(value.value, int) and 100 <= value.value <= 599:
                    statuses.add(str(value.value))

    return sorted(statuses)


def _asserted_response_pairs(function: ast.AST, operations) -> set[tuple[str, str]]:
    """Static, direct equality assertions tied to their response operation.

    This describes source assertions, not observed execution or semantic coverage.
    Ambiguous branches, helpers and dynamic expected values remain uncounted.
    """
    responses = {}
    pairs = set()

    def origin(value):
        if isinstance(value, ast.Name):
            return responses.get(value.id)
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name) and value.func.value.id == "api_client"
                and value.func.attr.lower() in HTTP_METHODS):
            path = _extract_call_path(value)
            for operation in operations:
                if (path is not None and operation["method"] == value.func.attr.upper()
                        and _path_matches(operation["path"], path)):
                    return operation
        return None

    for statement in function.body:
        if isinstance(statement, (ast.Return, ast.Raise, ast.Try, ast.TryStar)):
            break
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            operation = origin(statement.value)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        responses.pop(name.id, None)
                        if operation is not None and isinstance(target, ast.Name):
                            responses[name.id] = operation
        elif isinstance(statement, ast.Assert):
            if any(isinstance(n, (ast.Lambda, ast.IfExp)) or
                   isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)
                   for n in ast.walk(statement.test)):
                continue
            for comparison in ast.walk(statement.test):
                if not isinstance(comparison, ast.Compare) or len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
                    continue
                left, right = comparison.left, comparison.comparators[0]
                for status, literal in ((left, right), (right, left)):
                    if not (isinstance(status, ast.Attribute) and status.attr == "status_code"
                            and isinstance(literal, ast.Constant) and type(literal.value) is int):
                        continue
                    operation = origin(status.value)
                    if operation is not None and str(literal.value) in operation["response_codes"]:
                        pairs.add((operation["operation"], str(literal.value)))
    return pairs


def _function_source(source: str, node: ast.AST) -> str:
    lines = source.splitlines()

    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", len(lines))

    return textwrap.dedent("\n".join(lines[start:end]))


def _find_operations(
    calls: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []

    for call in calls:
        for operation in operations:
            if call["method"] != operation["method"]:
                continue

            if _path_matches(operation["path"], call["path"]):
                matched.append(operation)
                break

    return matched


def _build_operations(contract: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    paths = contract.get("paths") or {}

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        path_parameters = path_item.get("parameters") or []

        for method, operation in path_item.items():
            method_lower = str(method).lower()

            if method_lower not in HTTP_METHODS:
                continue

            if not isinstance(operation, dict):
                continue

            operation_parameters = operation.get("parameters") or []

            parameters: list[str] = []

            for parameter in [*path_parameters, *operation_parameters]:
                if not isinstance(parameter, dict):
                    continue

                name = parameter.get("name")
                location = parameter.get("in")

                if name:
                    parameters.append(
                        f"{location}.{name}" if location else str(name)
                    )

            responses = operation.get("responses") or {}

            response_codes = sorted(
                str(code)
                for code in responses.keys()
                if str(code).lower() != "default"
            )

            result.append(
                {
                    "method": method_lower.upper(),
                    "path": str(path),
                    "operation": _operation_key(method_lower, str(path)),
                    "parameters": sorted(set(parameters)),
                    "response_codes": response_codes,
                }
            )

    return result


def _classify_failure(
    outcome: str,
    message: str | None,
    *,
    collection_errors: list[str] | None = None,
    compatibility_issues: list[dict[str, Any]] | None = None,
    http_observation: dict[str, Any] | None = None,
    operation_response_codes: set[str] | None = None,
) -> dict[str, Any]:
    """
    Classify a failed test into service defect, test-code error or environment.
    """
    text = (message or "").lower()

    compatibility_issues = compatibility_issues or []
    collection_errors = collection_errors or []

    if outcome == "passed":
        return {
            "classification": "none",
            "reason": None,
            "diagnostics": None,
        }

    if compatibility_issues:
        issue = compatibility_issues[0]
        return {
            "classification": "test_code_error",
            "reason": issue.get("message") or "ошибка совместимости теста",
            "diagnostics": issue,
        }

    environment_markers = (
        "connection refused",
        "connectionerror",
        "connecttimeout",
        "readtimeout",
        "timeout",
        "dns",
        "name or service not known",
        "network is unreachable",
        "no route to host",
        "connection reset",
        "remote end closed",
    )

    if any(marker in text for marker in environment_markers):
        return {
            "classification": "environment",
            "reason": "стенд или сетевое окружение недоступны",
            "diagnostics": message,
        }

    if collection_errors:
        return {
            "classification": "test_code_error",
            "reason": "ошибка импорта или collection pytest",
            "diagnostics": collection_errors,
        }

    if http_observation and operation_response_codes is not None:
        actual = http_observation.get("status_code")

        if actual is not None and str(actual) not in operation_response_codes:
            return {
                "classification": "service_defect",
                "reason": (
                    f"сервис вернул HTTP {actual}, "
                    "который не задокументирован в контракте"
                ),
                "diagnostics": {
                    "actual_status": actual,
                    "documented_statuses": sorted(operation_response_codes),
                },
            }

    assertion_markers = (
        "assert",
        "assertionerror",
        "expected",
        "actual",
    )

    if any(marker in text for marker in assertion_markers):
        return {
            "classification": "service_defect",
            "reason": "фактический ответ не соответствует проверке теста",
            "diagnostics": message,
        }

    return {
        "classification": "unknown",
        "reason": "причина требует ручной проверки",
        "diagnostics": message,
    }


def build_result_trace(run) -> dict[str, Any]:
    """
    Build the complete result trace for GenerationRun.

    The function is deliberately defensive: tracing is an explanatory layer
    and must not break the generation pipeline.
    """
    try:
        provenance = run.provenance or {}

        contract_meta = provenance.get("contract") or {}
        snapshot = contract_meta.get("snapshot_path")

        if not snapshot:
            snapshot = run.contract_path

        contract = _load_contract(snapshot)

        operations = _build_operations(contract)

        operation_keys = {
            item["operation"]
            for item in operations
        }

        all_response_codes = {
            (operation["operation"], code)
            for operation in operations
            for code in operation["response_codes"]
        }

        all_parameters = {
            parameter
            for operation in operations
            for parameter in operation["parameters"]
        }

        tests_dir: Path | None = None

        if run.saved_versions:
            last = run.saved_versions[-1]
            raw_dir = last.get("tests_dir")

            if raw_dir:
                tests_dir = Path(raw_dir)

        source_tests: dict[str, str] = {}

        if tests_dir and tests_dir.exists():
            for path in sorted(tests_dir.rglob("test_*.py")):
                try:
                    source_tests[str(path)] = path.read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeError):
                    continue

        test_results = {
            item.get("nodeid"): item
            for item in ((run.run_result or {}).get("tests") or [])
            if isinstance(item, dict) and item.get("nodeid")
        }

        http_observations = {
            item.get("nodeid"): item
            for item in ((run.run_result or {}).get("http_observations") or [])
            if isinstance(item, dict) and item.get("nodeid")
        }

        covered_operations: set[str] = set()
        covered_codes: set[tuple[str, str]] = set()
        covered_parameters: set[str] = set()

        traced_tests: list[dict[str, Any]] = []

        for path, source in source_tests.items():
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue

                if not node.name.startswith("test_"):
                    continue

                function_source = _function_source(source, node)
                calls = _extract_http_calls(function_source)

                matched_operations = _find_operations(calls, operations)

                operation_names = [
                    operation["operation"]
                    for operation in matched_operations
                ]

                for operation in matched_operations:
                    covered_operations.add(operation["operation"])

                    for call in calls:
                        if call["method"] != operation["method"]:
                            continue

                        for parameter in call["parameters"]:
                            candidates = {
                                parameter,
                                f"query.{parameter}",
                                f"path.{parameter}",
                                f"header.{parameter}",
                            }

                            for candidate in candidates:
                                if candidate in operation["parameters"]:
                                    covered_parameters.add(candidate)

                # Match the complete pytest source identity, including class
                # and every parameterized invocation, never just a function name.
                parents = {child: parent for parent in ast.walk(tree)
                           for child in ast.iter_child_nodes(parent)}
                ancestors = []
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        break
                    if isinstance(parent, ast.ClassDef):
                        ancestors.append(parent.name)
                    parent = parents.get(parent)
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                identity = "::".join([*reversed(ancestors), node.name])
                relative = Path(path).relative_to(tests_dir).as_posix()
                covered_codes.update(_asserted_response_pairs(node, operations))
                nodeid_candidates = []
                for key in test_results:
                    file_part, separator, test_part = key.replace("\\", "/").partition("::")
                    matches_file = (file_part == relative or
                                    file_part.endswith("/" + relative))
                    if separator and matches_file and (test_part == identity or
                                                       test_part.startswith(identity + "[")):
                        nodeid_candidates.append(key)
                for nodeid in nodeid_candidates or [None]:
                    result = test_results.get(nodeid, {})
                    outcome = result.get("outcome", "not_run")

                    expected_statuses = _expected_statuses(node)

                    expected_result = (
                        "Ожидаемый HTTP-статус: "
                        + ", ".join(expected_statuses)
                        if expected_statuses
                        else "Ожидаемый результат определяется проверками теста"
                    )

                    operation_response_codes = {
                        code
                        for operation in matched_operations
                        for code in operation["response_codes"]
                    }

                    observation = http_observations.get(nodeid)

                    failure = _classify_failure(
                        outcome,
                        result.get("message"),
                        collection_errors=(run.run_result or {}).get(
                            "collection_errors"
                        ),
                        compatibility_issues=(run.run_result or {}).get(
                            "compatibility_issues"
                        ),
                        http_observation=observation,
                        operation_response_codes=operation_response_codes,
                    )

                    traced_tests.append(
                        {
                            "nodeid": nodeid or node.name,
                            "category": _test_category(function_source, node),
                            "expected_result": expected_result,
                            "operations": operation_names,
                            "response_codes": expected_statuses,
                            "parameters": sorted(
                                {
                                    parameter
                                    for call in calls
                                    for parameter in call["parameters"]
                                }
                            ),
                            "outcome": outcome,
                            "failure": failure,
                        }
                    )

        # Add runner results for tests that could not be mapped back to source.
        known_nodeids = {
            item["nodeid"]
            for item in traced_tests
        }

        for nodeid, result in test_results.items():
            if nodeid in known_nodeids:
                continue

            outcome = result.get("outcome", "unknown")

            failure = _classify_failure(
                outcome,
                result.get("message"),
                collection_errors=(run.run_result or {}).get(
                    "collection_errors"
                ),
                compatibility_issues=(run.run_result or {}).get(
                    "compatibility_issues"
                ),
            )

            traced_tests.append(
                {
                    "nodeid": nodeid,
                    "category": "не определена",
                    "expected_result": "Не удалось восстановить из исходного теста",
                    "operations": [],
                    "response_codes": [],
                    "parameters": [],
                    "outcome": outcome,
                    "failure": failure,
                }
            )

        uncovered_operations = sorted(
            operation_keys - covered_operations
        )

        uncovered_codes = sorted(
            all_response_codes - covered_codes
        )

        uncovered_parameters = sorted(
            all_parameters - covered_parameters
        )

        failed_tests = [
            item
            for item in traced_tests
            if item["outcome"] in {"failed", "error"}
        ]

        return {
            "summary": {
                "operations_total": len(operation_keys),
                "operations_covered": len(covered_operations),
                "operations_uncovered": uncovered_operations,
                "response_codes_total": len(all_response_codes),
                "response_codes_covered": len(covered_codes),
                "response_codes_uncovered": [f"{operation} -> {code}" for operation, code in uncovered_codes],
                "response_codes_basis": "static operation/status equality assertions; not runtime coverage",
                "parameters_total": len(all_parameters),
                "parameters_covered": len(covered_parameters),
                "parameters_uncovered": uncovered_parameters,
            },
            "tests": traced_tests,
            "failed_tests": failed_tests,
        }

    except Exception as exc:
        # 4.1.5 is explanatory functionality. If the trace itself fails,
        # preserve a machine-readable diagnostic instead of destroying report.
        return {
            "summary": {
                "operations_total": 0,
                "operations_covered": 0,
                "operations_uncovered": [],
                "response_codes_total": 0,
                "response_codes_covered": 0,
                "response_codes_uncovered": [],
                "parameters_total": 0,
                "parameters_covered": 0,
                "parameters_uncovered": [],
            },
            "tests": [],
            "failed_tests": [],
            "trace_error": str(exc),
        }
