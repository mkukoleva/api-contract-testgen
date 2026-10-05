"""Prompts and payload clipping; no LLM calls."""

import re

from prototype.generator.prompts import (
    REPAIR_SYSTEM_PROMPT,
    build_generation_prompt,
    build_repair_prompt,
    clip_code,
    diag_tail,
)


def test_repair_prompt_explicitly_forbids_weakening_assertions():
    prompt = build_repair_prompt(
        kind="function",
        test_fragment="def test_x():\n    assert r.status_code == 200\n",
        contract_fragment="GET /catalogue -> 200",
        diagnostics="assert 500 == 200",
    )
    lowered = prompt.lower()
    assert "не ослабляй" in REPAIR_SYSTEM_PROMPT.lower()
    assert "assert" in prompt
    # Both the system prompt and the user message carry the prohibition.
    assert "ослаблять" in prompt
    assert "дефект сервиса" in prompt
    assert "def test_x" in prompt
    assert "GET /catalogue -> 200" in prompt
    assert "assert 500 == 200" in prompt


def test_repair_prompt_uses_full_module_for_file_kind():
    prompt = build_repair_prompt(
        kind="file",
        test_fragment="import missing_module\n\ndef test_x():\n    pass\n",
        contract_fragment="GET /catalogue -> 200",
        diagnostics="ModuleNotFoundError",
    )
    assert "исправленный файл модуля целиком" in prompt


def test_generation_prompt_passes_contract_fragment_only():
    prompt = build_generation_prompt("Название API: X\nОперации:\n- GET /catalogue")
    assert "Название API: X" in prompt
    assert "- GET /catalogue" in prompt


def test_clip_code_keeps_short_code_and_clips_long():
    code = "def test_x():\n    assert 1\n    assert 2\n"
    assert clip_code(code, 10) == code
    clipped = clip_code(code, 2)
    assert clipped.startswith("def test_x():")
    assert clipped.endswith("…(обрезано)")
    assert clipped.count("\n") + 1 == 3  # 2 original lines + marker


def test_diag_tail_returns_tail_and_marks_truncation():
    message = "A" * 100 + "\n" + "B" * 50
    tail = diag_tail(message, 60)
    assert tail.startswith("…(обрезано начало)")
    assert tail.endswith("B" * 50)
    assert len(tail) < 80
    assert diag_tail("short", 10) == "short"
    assert diag_tail(None, 10) == "(диагностика отсутствует)"
    re.match(r"…\(обрезано начало\)", tail)
