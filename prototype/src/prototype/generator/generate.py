"""Initial LLM generation of pytest test files from an API contract."""

import re
from typing import Any

from ..postprocess.fixes import PreparedFile, prepare_file


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
) -> dict[str, Any]:
    """Generate and post-process test files with one structured LLM call.

    Returns: {prepared: tuple[PreparedFile, ...], errors: tuple[str, ...],
    tokens: (input, output, total), calls: int}. Files with unsafe names are
    dropped and reported; every kept file is normalised and statically checked.
    """
    from ..llm.model import build_model
    from ..storage import GeneratedFile
    from .context import render_contract_for_generation
    from .prompts import (
        GENERATION_SYSTEM_PROMPT,
        GenerationOutput,
        build_generation_prompt,
    )

    llm = model if model is not None else build_model()
    contract_fragment = render_contract_for_generation(contract)
    user_prompt = build_generation_prompt(contract_fragment)

    try:
        message = llm.invoke(
            [
                {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
    except Exception as exc:  # network/API failure is not a repaired code error
        return {
            "prepared": (),
            "errors": (f"LLM generation failed: {exc}",),
            "tokens": (0, 0, 0),
            "calls": 0,
        }

    input_tokens, output_tokens, total_tokens = request_usage(message)
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
    prepared: list[PreparedFile] = []
    for spec in output.files:
        try:
            generated = GeneratedFile(spec.name, spec.code)
        except ValueError as exc:
            errors.append(f"{spec.name}: {exc}")
            continue
        prepared.append(prepare_file(generated.name, generated.code))

    return {
        "prepared": tuple(prepared),
        "errors": tuple(errors),
        "tokens": (input_tokens, output_tokens, total_tokens),
        "calls": 1,
    }
