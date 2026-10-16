"""LangChain callbacks -> journal events (ТЗ v3 4.1.8.2: LLM calls and tool calls).

journal_context() installs the handler through a context variable registered with
langchain_core's configure hook, so every model and tool call inside the block —
including the generator's own model calls — is journaled without changing how
the agent is built or invoked. Prompt and response texts are never recorded.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from ..llm.diagnostics import describe_error
from .core import Journal


_ACTIVE: ContextVar = ContextVar("prototype_journal_handler", default=None)
register_configure_hook(_ACTIVE, inheritable=True)

_SUMMARY_FIELDS = ("tool", "status", "pipeline_status", "backend", "success")


def _is_timeout(error: BaseException) -> bool:
    return any("Timeout" in cls.__name__ for cls in type(error).__mro__)


def _model_name(serialized: dict | None, metadata: dict | None, kwargs: dict) -> str | None:
    params = kwargs.get("invocation_params") or {}
    return ((metadata or {}).get("ls_model_name") or params.get("model") or params.get("model_name")
            or (serialized or {}).get("name"))


def _usage(response: Any) -> dict:
    try:
        usage = response.generations[0][0].message.usage_metadata or {}
    except (AttributeError, IndexError, TypeError):
        usage = {}
    if not usage:
        token_usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        usage = {"input_tokens": token_usage.get("prompt_tokens"),
                 "output_tokens": token_usage.get("completion_tokens"),
                 "total_tokens": token_usage.get("total_tokens")}
    return {key: usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")}


def _summary(output: Any) -> dict:
    """Short, code-free description of a tool result."""
    artifact = getattr(output, "artifact", None)
    value = artifact if isinstance(artifact, dict) else getattr(output, "content", output)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"type": "text", "length": len(value)}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    summary = {key: value[key] for key in _SUMMARY_FIELDS
               if key in value and isinstance(value[key], (str, int, float, bool, type(None)))}
    if isinstance(value.get("tests"), list):
        summary["tests_count"] = len(value["tests"])
    return summary


class JournalCallbackHandler(BaseCallbackHandler):
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._llm: dict[str, tuple[float, str | None]] = {}
        self._tools: dict[str, tuple[float, str | None]] = {}

    # --- LLM -------------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs) -> None:
        self._llm.setdefault(str(run_id), (time.monotonic(), _model_name(serialized, metadata, kwargs)))

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs) -> None:
        self._llm.setdefault(str(run_id), (time.monotonic(), _model_name(serialized, metadata, kwargs)))

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        started = self._llm.pop(str(run_id), None)
        if started is None:
            return
        self.journal.emit("llm.call", "ok", model=started[1],
                          duration_s=round(time.monotonic() - started[0], 3), **_usage(response))

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        started = self._llm.pop(str(run_id), None)
        if started is None:
            return
        self.journal.emit("llm.call", "timeout" if _is_timeout(error) else "error", model=started[1],
                          duration_s=round(time.monotonic() - started[0], 3), error=describe_error(error))

    # --- tools -----------------------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name")
        self._tools.setdefault(str(run_id), (time.monotonic(), name))

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:
        started = self._tools.pop(str(run_id), None)
        if started is None:
            return
        self.journal.emit("tool.call", "ok", tool=started[1],
                          duration_s=round(time.monotonic() - started[0], 3), summary=_summary(output))

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:
        started = self._tools.pop(str(run_id), None)
        if started is None:
            return
        self.journal.emit("tool.call", "error", tool=started[1],
                          duration_s=round(time.monotonic() - started[0], 3), error=describe_error(error))


@contextmanager
def journal_context(handler: JournalCallbackHandler):
    token = _ACTIVE.set(handler)
    try:
        yield handler
    finally:
        _ACTIVE.reset(token)
