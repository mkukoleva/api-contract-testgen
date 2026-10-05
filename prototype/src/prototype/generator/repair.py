"""Limited self-repair: classify failures and repair only generated code.

Boundaries (ADR 0004):
- Infrastructure failures (no Docker, unavailable API), timeouts and runs
  interrupted never produce an LLM repair call.
- A test that fails because the observed response contradicts the documented
  contract is a suspected service defect: it is reported and NOT repaired, so
  an assertion is never weakened to make the test pass.
- Collection/syntax/import errors repair the whole file; failed or errored
  test bodies repair only the failing test function.
"""

import ast
from dataclasses import dataclass
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from ..service_tools.runner.contracts import RunResult, RunStatus, TestOutcome
from .context import (
    documented_status_codes,
    find_operation,
    operation_fragment,
    render_contract_for_generation,
)
from .generate import parse_model_content, request_usage
from .prompts import (
    REPAIR_SYSTEM_PROMPT,
    RepairOutput,
    build_repair_prompt,
    clip_code,
    diag_tail,
)

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_ENV_MARKERS = (
    "connectionerror", "connection refused", "max retries exceeded",
    "connecttimeout", "readtimeout", "timed out", "econnrefused",
    "nodename nor servname", "new connection", "host is unreachable",
)


@dataclass(frozen=True)
class RepairTarget:
    """One repairable unit: a whole file or a single test function."""

    kind: Literal["file", "function"]
    file: str
    function_path: tuple[str, ...] = ()
    signature: str = ""
    reason: str = "unknown"
    nodeid: str | None = None
    message: str | None = None

    def identifier(self) -> str:
        if self.kind == "function" and self.function_path:
            return f"{self.file}::{ '::'.join(self.function_path) }"
        return self.file


@dataclass(frozen=True)
class RepairPlan:
    targets: tuple[RepairTarget, ...] = ()
    suspected_defects: tuple[dict[str, Any], ...] = ()
    environmental: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RepairCallResult:
    ok: bool
    code: str | None = None
    tokens: tuple[int, int, int] = (0, 0, 0)
    payload_chars: int = 0
    error: str | None = None


def error_signature(message: str | None) -> str:
    """Normalise a failure message so equal errors compare equal.

    Line numbers, file paths and surrounding whitespace are removed; the
    result is capped so it stays a compact comparable key.
    """
    if not message:
        return ""
    text = " ".join(message.split())
    text = re.sub(r"\.py:\d+:", ".py:", text)
    text = re.sub(r"\.py:\d+: in <[^>]+>", ".py: in <fn>", text)
    text = re.sub(r'File "[^"]*"', 'File "<...>"', text)
    text = re.sub(r"line \d+", "line N", text)
    return text[:200]


def split_nodeid(nodeid: str) -> tuple[str, tuple[str, ...]]:
    """('test_a.py::TestTags::test_tags[1]',) -> ('test_a.py', ('TestTags', 'test_tags'))."""
    parts = nodeid.split("::")
    file_name = parts[0]
    path: list[str] = []
    for part in parts[1:]:
        name = re.sub(r"\[.*?\]$", "", part)
        name = re.sub(r"\(.*?\)$", "", name)
        if name:
            path.append(name)
    return file_name, tuple(path)


def find_test_node(tree: ast.Module, function_path: tuple[str, ...]):
    """Follow the nodeid path down a module AST to a test function node."""
    node: Any = tree
    for segment in function_path:
        if isinstance(node, ast.Module):
            matches = [
                child
                for child in node.body
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                and child.name == segment
            ]
        elif isinstance(node, ast.ClassDef):
            matches = [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == segment
            ]
        else:
            return None
        if not matches:
            return None
        node = matches[0]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node
    return None


def _decorator_start_line(node) -> int:
    """1-based first line of a function, including its decorators."""
    if getattr(node, "decorator_list", None):
        return min(item.lineno for item in node.decorator_list)
    return node.lineno


def _source_slice(code: str, start_line: int, end_line: int) -> str:
    lines = code.splitlines(keepends=True)
    return "".join(lines[start_line - 1 : end_line])


def extract_function_source(code: str, function_path: tuple[str, ...]) -> str | None:
    """Return the source of one test function (with decorators), or None."""
    if not function_path:
        return None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    node = find_test_node(tree, function_path)
    if node is None:
        return None
    return _source_slice(code, _decorator_start_line(node), node.end_lineno) or None


def replace_function(code: str, function_path: tuple[str, ...], fixed: str) -> str:
    """Replace one test function with the repaired source.

    Raises ValueError when the function cannot be located or the repaired
    source does not parse / does not define the same test function.
    """
    if not function_path:
        raise ValueError("function path is empty")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse module: {exc}") from exc
    node = find_test_node(tree, function_path)
    if node is None:
        raise ValueError(
            f"test function {'::'.join(function_path)} not found in module"
        )

    replacement = fixed.strip("\n") + "\n"
    new_code = _source_slice(code, 1, _decorator_start_line(node) - 1) + replacement
    new_code += _source_slice(code, node.end_lineno + 1, len(code.splitlines()))

    try:
        new_tree = ast.parse(new_code)
    except SyntaxError as exc:
        raise ValueError(f"repaired code is not valid python: {exc}") from exc
    if find_test_node(new_tree, function_path) is None:
        raise ValueError(
            f"repaired code lost the test function {'::'.join(function_path)}"
        )
    return new_code


def _joined_pattern(node: ast.JoinedStr) -> str | None:
    """Rebuild a path pattern like /catalogue/{id} from an f-string URL."""
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
            continue
        if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
            name = part.value.id
            if name in {"base_url", "baseurl", "host"}:
                continue  # host prefix is not part of the observed path
            parts.append("{" + name + "}")
            continue
        parts.append("{}")
    text = "".join(parts)
    return text if "/" in text else None


def _first_path_arg(node: Any) -> str | None:
    """First constant (or f-string pattern) path fragment of a request URL."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if "/" in node.value else None
    if isinstance(node, ast.JoinedStr):
        return _joined_pattern(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _first_path_arg(node.left) or _first_path_arg(node.right)
    return None


def extract_request_site(code: str) -> tuple[str, str] | None:
    """Return (METHOD, path fragment) of the first HTTP call in the code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        method = None
        if isinstance(func, ast.Attribute) and isinstance(func.attr, str) and func.attr.lower() in _HTTP_VERBS:
            method = func.attr.lower()
        elif isinstance(func, ast.Name) and isinstance(func.id, str) and func.id.lower() in _HTTP_VERBS:
            method = func.id.lower()
        if not method:
            continue
        path = _first_path_arg(node.args[0])
        if path:
            return method.upper(), path
    return None


def parse_actual_status(message: str | None) -> int | None:
    """Extract the actual HTTP status from an assertion failure."""
    if not message:
        return None
    for pattern in (r"assert\s+(\d{3})\s*==\s*\d{3}", r"assert\s+(\d{3})(?:\s|$)"):
        match = re.search(pattern, message)
        if match and 100 <= int(match.group(1)) <= 599:
            return int(match.group(1))
    return None


def is_environmental_failure(message: str | None, base_url: str | None) -> bool:
    """A connection/timeout error to the API host is not a code bug."""
    if not message or not base_url:
        return False
    lowered = message.lower()
    if not any(marker in lowered for marker in _ENV_MARKERS):
        return False
    host = urlsplit(base_url).hostname
    return bool(host and host in lowered)


def _as_repair_target(
    *,
    kind: Literal["file", "function"],
    file: str,
    function_path: tuple[str, ...],
    message: str | None,
    reason: str,
    nodeid: str | None = None,
) -> RepairTarget:
    return RepairTarget(
        kind=kind,
        file=file,
        function_path=function_path,
        signature=error_signature(message) if message else "",
        reason=reason,
        nodeid=nodeid,
        message=message,
    )


def classify_result(
    contract: dict[str, Any],
    files: dict[str, str],
    result: RunResult,
    settings: Any,
) -> RepairPlan:
    """Turn one run result into a repair plan (targets + reported problems)."""
    if result.status == RunStatus.COLLECTION_ERROR:
        return _classify_collection(result)
    if result.status != RunStatus.COMPLETED:
        return RepairPlan()

    base_url = settings.base_url
    targets: list[RepairTarget] = []
    defects: list[dict[str, Any]] = []
    environmental: list[dict[str, Any]] = []

    for test in result.tests:
        if test.outcome in (TestOutcome.PASSED, TestOutcome.SKIPPED):
            continue
        file_name, function_path = split_nodeid(test.nodeid)
        if file_name not in files:
            continue
        code = files[file_name]

        if test.outcome == TestOutcome.ERROR:
            if is_environmental_failure(test.message, base_url):
                environmental.append({"nodeid": test.nodeid, "message": test.message})
            elif function_path:
                targets.append(
                    _as_repair_target(
                        kind="function",
                        file=file_name,
                        function_path=function_path,
                        message=test.message,
                        reason="test_error",
                        nodeid=test.nodeid,
                    )
                )
            else:
                targets.append(
                    _as_repair_target(
                        kind="file",
                        file=file_name,
                        function_path=(),
                        message=test.message,
                        reason="test_error",
                        nodeid=test.nodeid,
                    )
                )
            continue

        # outcome == failed: an assertion did not hold.
        target_code = code
        if function_path:
            function_source = extract_function_source(code, function_path)
            if function_source:
                target_code = function_source
        request_site = extract_request_site(target_code) or extract_request_site(code)
        if request_site is None:
            defects.append(
                {
                    "nodeid": test.nodeid,
                    "message": test.message,
                    "reason": "cannot attribute the request to a contract operation",
                }
            )
            continue
        method, observed_path = request_site
        documented = documented_status_codes(contract, method, observed_path)
        actual = parse_actual_status(test.message)
        if documented and actual is not None and actual not in documented:
            defects.append(
                {
                    "nodeid": test.nodeid,
                    "message": test.message,
                    "reason": (
                        f"service returned {actual} for {method} {observed_path}, "
                        f"which is not documented in the contract"
                    ),
                }
            )
            continue
        if not function_path:
            targets.append(
                _as_repair_target(
                    kind="file",
                    file=file_name,
                    function_path=(),
                    message=test.message,
                    reason="assert_failed",
                    nodeid=test.nodeid,
                )
            )
        else:
            targets.append(
                _as_repair_target(
                    kind="function",
                    file=file_name,
                    function_path=function_path,
                    message=test.message,
                    reason="assert_failed",
                    nodeid=test.nodeid,
                )
            )

    return RepairPlan(
        targets=tuple(targets),
        suspected_defects=tuple(defects),
        environmental=tuple(environmental),
    )


def _classify_collection(result: RunResult) -> RepairPlan:
    targets: list[RepairTarget] = []
    seen: set[str] = set()
    def add(file: str, message: str, reason: str) -> None:
        if not file.endswith(".py") or file in seen:
            return
        seen.add(file)
        targets.append(_as_repair_target(kind="file", file=file, function_path=(),
                                         message=message, reason=reason))
    for message in result.collection_errors:
        add(message.split(":", 1)[0].strip(), message, "collection")
    for issue in result.compatibility_issues:
        add(issue.path.rsplit("/", 1)[-1], issue.message, issue.category.value)
    return RepairPlan(targets=tuple(targets))


def _contract_fragment_for(contract: dict[str, Any], settings: Any, target: RepairTarget, code: str) -> str:
    limit = settings.contract_fragment_chars
    if target.kind == "function":
        function_source = extract_function_source(code, target.function_path)
        base = function_source or code
        request_site = extract_request_site(base) or extract_request_site(code)
        if request_site:
            return operation_fragment(
                contract, request_site[0], request_site[1], max_chars=limit
            )
    fragment = render_contract_for_generation(contract, max_chars=limit)
    return fragment[:limit]


def _looks_like_test_code(candidate: str) -> bool:
    """Guard against junk replies: must parse and contain test-like units."""
    try:
        tree = ast.parse(candidate)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                          ast.Import, ast.ImportFrom))
        for node in tree.body
    )


def _extract_repair_code(content: str) -> str:
    """Repair replies are code (the repair prompt asks for it), not JSON.

    Order: a JSON RepairOutput (providers that comply), then markdown fenced
    code blocks, then the text from the first code-looking line, then the raw
    text. The winning candidate must parse and look like a test module, so a
    prose-only reply such as "not json" is rejected.
    """
    try:
        parsed = parse_model_content(content, RepairOutput)
    except ValueError:
        pass
    else:
        return parsed.code

    candidates: list[str] = []
    candidates.extend(re.findall(r"```[^\n]*\n(.*?)```", content, flags=re.S))
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(
            ("def ", "async def ", "class ", "import ", "from ", "@")
        ):
            candidates.append("\n".join(lines[index:]))
            break
    candidates.append(content)

    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and _looks_like_test_code(candidate):
            return candidate
    raise ValueError(
        "no JSON or parseable Python test code was found in the repair output"
    )


def repair_call(
    model: Any,
    settings: Any,
    contract: dict[str, Any],
    target: RepairTarget,
    code: str,
) -> RepairCallResult:
    """One self-repair LLM call with a compact payload (test + contract + tail)."""
    kind = target.kind
    base_code = code
    if kind == "function":
        function_source = extract_function_source(code, target.function_path)
        if function_source is None:
            kind = "file"
        else:
            base_code = function_source

    test_fragment = clip_code(base_code, settings.test_fragment_lines)
    contract_fragment = _contract_fragment_for(contract, settings, target, code)
    diagnostics = diag_tail(target.message, settings.diagnostic_chars)
    prompt = build_repair_prompt(
        kind=kind,
        test_fragment=test_fragment,
        contract_fragment=contract_fragment,
        diagnostics=diagnostics,
    )

    try:
        message = model.invoke(
            [
                {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
    except Exception as exc:
        return RepairCallResult(
            ok=False, payload_chars=len(prompt), error=f"LLM repair call failed: {exc}"
        )

    tokens = request_usage(message)
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    try:
        fixed = _extract_repair_code(content)
    except Exception as exc:
        return RepairCallResult(
            ok=False,
            tokens=tokens,
            payload_chars=len(prompt),
            error=f"repair output is not valid: {exc}",
        )

    if kind == "function":
        normalized = _normalise_function_fix(fixed, target)
        if normalized is None:
            return RepairCallResult(
                ok=False,
                tokens=tokens,
                payload_chars=len(prompt),
                error=(
                    f"repair output lost the test function "
                    f"{'::'.join(target.function_path)}"
                ),
            )
        fixed = normalized
    else:
        fixed = fixed.rstrip() + "\n"

    return RepairCallResult(ok=True, code=fixed, tokens=tokens, payload_chars=len(prompt))


def _normalise_function_fix(fixed: str, target: RepairTarget) -> str | None:
    """Extract the repaired function from the model output, or None."""
    try:
        tree = ast.parse(fixed)
    except SyntaxError:
        return None
    node = find_test_node(tree, target.function_path)
    if node is None and len(target.function_path) == 1:
        # The model may have returned the function without its decorators or
        # wrapped it in a module; search by (unique) name instead.
        for candidate in ast.walk(tree):
            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)) and candidate.name == target.function_path[-1]:
                node = candidate
                break
    if node is None:
        return None
    return _source_slice(fixed, _decorator_start_line(node), node.end_lineno) or None


def apply_repair(files: dict[str, str], target: RepairTarget, fixed: str) -> dict[str, str]:
    """Return a new file map with the repair applied (never mutates input)."""
    updated = dict(files)
    if target.kind == "function" and target.function_path:
        updated[target.file] = replace_function(updated[target.file], target.function_path, fixed)
    else:
        updated[target.file] = fixed.rstrip("\n") + "\n"
    return updated
