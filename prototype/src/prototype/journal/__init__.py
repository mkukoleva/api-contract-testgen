"""Run journal for the CLI `tester` (ТЗ v3 4.1.8). Standard library only on import."""

from .core import Journal

__all__ = ["Journal", "JournalCallbackHandler", "journal_context"]


def __getattr__(name):
    # LangChain is loaded only when the callback adapter is actually requested.
    if name in {"JournalCallbackHandler", "journal_context"}:
        from . import langchain as adapter

        return getattr(adapter, name)
    raise AttributeError(name)
