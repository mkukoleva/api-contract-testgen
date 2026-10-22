"""Initial LLM generation of pytest test files from an API contract."""

import re
import ast
from typing import Any

from ..postprocess.fixes import PreparedFile, prepare_file


def _duplicate_tests(code: str) -> tuple[str, ...]:
    """Reject Python name shadowing that silently removes generated tests."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return ()  # Syntax errors belong to the existing repair path.
    duplicates = []

    def check_scope(body, prefix=""):
        seen = set()
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            is_test = node.name.startswith("Test" if isinstance(node, ast.ClassDef) else "test_")
            if node.name in seen and is_test:
                duplicates.append(prefix + node.name)
            seen.add(node.name)
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                check_scope(node.body, prefix + node.name + "::")

    check_scope(tree.body)
    return tuple(duplicates)


def _custom_fixtures(code: str) -> tuple[str, ...]:
    """Find explicit pytest fixture declarations without importing LLM code.

    This checks the generator's fixture contract; Docker remains the execution
    security boundary. Syntax errors stay available to the repair pipeline.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return ()
    pytest_names, fixture_names = {"pytest"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pytest_names.update(alias.asname or alias.name for alias in node.names if alias.name == "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            fixture_names.update(alias.asname or alias.name for alias in node.names
                                 if alias.name in {"fixture", "yield_fixture"})
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            direct = isinstance(target, ast.Name) and target.id in fixture_names
            qualified = (isinstance(target, ast.Attribute) and
                         isinstance(target.value, ast.Name) and target.value.id in pytest_names and
                         target.attr in {"fixture", "yield_fixture"})
            if direct or qualified:
                found.append(node.name)
                break
    return tuple(found)


def _empty_tests(code: str) -> tuple[str, ...]:
    """Reject obvious placeholders; this is not semantic assertion validation."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return ()
    scopes = [("", tree.body)]
    found = []
    while scopes:
        prefix, body = scopes.pop()
        for node in body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                scopes.append((prefix + node.name + "::", node.body))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if all(isinstance(item, ast.Pass) or (isinstance(item, ast.Expr)
                       and isinstance(item.value, ast.Constant)) for item in node.body):
                    found.append(prefix + node.name)
    return tuple(found)


def request_usage(message: Any) -> tuple[int, int, int]:
    """Input/output/total tokens from a LangChain message usage_metadata.

    Returns zeros when the provider did not report usage. The budget of the
    repair loop is driven by output tokens (ADR 0004).
    """
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return 0, 0, 0
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, int(total_tokens or 0)


def parse_model_content(content: str, schema: type) -> Any:
    """Parse structured model output that a real LLM may wrap in prose.

    Candidates, in order: the raw text, each markdown fenced block. As a last
    resort every '{' position is tried with json.JSONDecoder.raw_decode, which
    skips brace-like prose such as f\"{base_url}/catalogue\". Raises ValueError
    when no JSON object validates against the schema.
    """
    import json as _json
    from json import JSONDecoder

    text = content.strip()
    candidates = [text]
    candidates.extend(re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S))
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return schema.model_validate_json(candidate)
        except Exception:
            continue
    decoder = JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except Exception:
            continue
        try:
            return schema.model_validate(obj)
        except Exception:
            continue
    raise ValueError(
        "no JSON object matching the expected schema was found in the model output"
    )


def generate_suite(
    contract: dict[str, Any],
    settings: Any,
    *,
    model: Any = None,
    schemathesis_context: str = "",
    user_story: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate and post-process test files with one structured LLM call.

    ``schemathesis_context`` is an optional deterministic-tool digest of the
    live service (TZ v3 4.1.2.3); it is diagnostic context only, the contract
    stays the source of expected values.

    Returns: {prepared: tuple[PreparedFile, ...], errors: tuple[str, ...],
    tokens: (input, output, total), calls: int}. Files with unsafe names are
    dropped and reported; every kept file is normalised and statically checked.
    """
    from ..llm.model import build_model
    from ..storage import GeneratedFile
    from .context import render_contract_for_generation
    from .scenarios import build_scenario_plan
    from .stories import load_user_story, check_story_code
    from .prompts import (
        GENERATION_SYSTEM_PROMPT,
        GenerationOutput,
        build_generation_prompt,
    )

    try:
        contract_fragment = render_contract_for_generation(contract,
            max_chars=settings.summary_max_chars)
        scenario_plan = build_scenario_plan(contract)
        story_path = getattr(settings, "user_story_path", None)
        if user_story is None and story_path is not None:
            user_story = load_user_story(story_path, contract)
    except (OSError, ValueError, UnicodeError) as exc:
        return {"prepared": (), "errors": (f"invalid generation context: {exc}",),
                "tokens": (0, 0, 0), "calls": 0}
    llm = model if model is not None else build_model()
    user_prompt = build_generation_prompt(contract_fragment, scenario_plan,
        source_context=(settings.source_context or {}).get("text", ""),
        schemathesis_context=schemathesis_context, user_story=user_story)

    try:
        message = llm.invoke(
            [
                {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
    except Exception as exc:  # network/API failure is not a repaired code error
        from ..llm.diagnostics import describe_error
        return {
            "prepared": (),
            "errors": (f"LLM generation failed: {describe_error(exc)}",),
            "tokens": (0, 0, 0),
            "calls": 0,
        }

    input_tokens, output_tokens, total_tokens = request_usage(message)
    if (getattr(message, "response_metadata", None) or {}).get("finish_reason") == "length":
        return {"prepared": (), "errors": ("generation output truncated by model token limit; use a smaller contract or increase the configured output limit",),
                "tokens": (input_tokens, output_tokens, total_tokens), "calls": 1}
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )

    try:
        output = parse_model_content(content, GenerationOutput)
    except Exception as exc:
        return {
            "prepared": (),
            "errors": (f"generation output is not valid: {exc}",),
            "tokens": (input_tokens, output_tokens, total_tokens),
            "calls": 1,
        }

    errors: list[str] = []
    transport_fixes = []
    prepared: list[PreparedFile] = []
    names = [spec.name for spec in output.files]
    if len(names) != len(set(names)):
        return {"prepared": (), "errors": ("duplicate generated file names; refusing to overwrite tests",),
                "tokens": (input_tokens, output_tokens, total_tokens), "calls": 1}
    for spec in output.files:
        try:
            from .transport_examples import normalize_transport_examples
            normalized, fixes = normalize_transport_examples(spec.code)
            transport_fixes.extend({"file": spec.name, **fix} for fix in fixes)
            generated = GeneratedFile(spec.name, normalized)
        except ValueError as exc:
            errors.append(f"{spec.name}: {exc}")
            continue
        fixtures = _custom_fixtures(generated.code)
        if fixtures:
            errors.append(f"{spec.name}: custom pytest fixtures are not allowed ({', '.join(fixtures)}); "
                          "use trusted base_url and api_client")
            continue
        duplicates = _duplicate_tests(generated.code)
        if duplicates:
            errors.append(f"{spec.name}: duplicate test definitions would overwrite tests: {', '.join(duplicates)}")
            continue
        placeholders = _empty_tests(generated.code)
        if placeholders:
            errors.append(f"{spec.name}: empty test placeholders are not executable scenarios: {', '.join(placeholders)}")
            continue
        prepared.append(prepare_file(generated.name, generated.code))

    if user_story is not None:
        problem = check_story_code(prepared, user_story)
        if problem and not any(file.status == "syntax_error" for file in prepared):
            errors.append(problem)
            prepared = []
    return {
        "prepared": tuple(prepared),
        "errors": tuple(errors),
        "tokens": (input_tokens, output_tokens, total_tokens),
        "calls": 1,
        "scenario_plan": scenario_plan,
        "transport_fixes": transport_fixes,
        "user_story": user_story,
    }
