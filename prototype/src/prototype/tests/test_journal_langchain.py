"""LangChain callbacks -> run journal events; fake models only, no provider calls."""

import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool


def answer(content="ok"):
    return AIMessage(content=content,
                     usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13})


class FailingModel(GenericFakeChatModel):
    error: type = RuntimeError

    def _generate(self, *args, **kwargs):
        raise self.error("down")


class APITimeoutError(Exception):
    pass


@tool
def ping(x: str) -> str:
    """Return pong."""
    return "pong"


@tool
def boom(x: str) -> str:
    """Always fails."""
    raise ValueError("tool exploded")


@tool
def generate(x: str) -> dict:
    """Return a large generator-like result."""
    return {"tool": "generate_tests_tool", "status": "success", "pipeline_status": "success",
            "tests": [{"nodeid": f"test_a.py::test_{index}", "code": "x" * 50} for index in range(500)]}


def journal_events(journal, name):
    return [event for event in journal.events if event["event"] == name]


@pytest.fixture
def journal():
    from prototype.journal import Journal

    return Journal()


def test_llm_call_has_model_tokens_and_duration(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    with journal_context(JournalCallbackHandler(journal)):
        GenericFakeChatModel(messages=iter([answer()])).invoke("q")
    calls = journal_events(journal, "llm.call")
    assert len(calls) == 1
    assert calls[0]["status"] == "ok"
    data = calls[0]["data"]
    assert (data["input_tokens"], data["output_tokens"], data["total_tokens"]) == (10, 3, 13)
    assert data["duration_s"] >= 0
    assert "model" in data


def test_llm_error_is_error_event(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    with journal_context(JournalCallbackHandler(journal)), pytest.raises(RuntimeError):
        FailingModel(messages=iter([])).invoke("q")
    calls = journal_events(journal, "llm.call")
    assert [call["status"] for call in calls] == ["error"]
    assert "down" in calls[0]["data"]["error"]


def test_timeout_like_exception_is_timeout(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    with journal_context(JournalCallbackHandler(journal)), pytest.raises(APITimeoutError):
        FailingModel(messages=iter([]), error=APITimeoutError).invoke("q")
    assert [call["status"] for call in journal_events(journal, "llm.call")] == ["timeout"]


def test_explicit_config_callbacks_do_not_duplicate(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    handler = JournalCallbackHandler(journal)
    with journal_context(handler):
        GenericFakeChatModel(messages=iter([answer()])).invoke("q", config={"callbacks": [handler]})
    assert len(journal_events(journal, "llm.call")) == 1


def test_tool_ok_and_error_events(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    with journal_context(JournalCallbackHandler(journal)):
        ping.invoke({"x": "1"})
        with pytest.raises(ValueError):
            boom.invoke({"x": "2"})
    calls = journal_events(journal, "tool.call")
    assert [(call["data"]["tool"], call["status"]) for call in calls] == [("ping", "ok"), ("boom", "error")]
    assert "tool exploded" in calls[1]["data"]["error"]
    assert calls[0]["data"]["duration_s"] >= 0


def test_tools_run_by_langgraph_are_journaled(journal):
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode
    from prototype.journal import JournalCallbackHandler, journal_context

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([ping, boom], handle_tool_errors=True))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    request = AIMessage(content="", tool_calls=[{"name": "ping", "args": {"x": "1"}, "id": "1"},
                                                {"name": "boom", "args": {"x": "2"}, "id": "2"}])
    with journal_context(JournalCallbackHandler(journal)):
        list(graph.compile().stream({"messages": [request]}, stream_mode="values"))
    calls = journal_events(journal, "tool.call")
    assert sorted((call["data"]["tool"], call["status"]) for call in calls) == [("boom", "error"), ("ping", "ok")]


def test_huge_tool_output_is_summarized(tmp_path, journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    journal.attach(tmp_path)
    with journal_context(JournalCallbackHandler(journal)):
        generate.invoke({"x": "1"})
    journal.close()
    line = next(line for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
                if '"tool.call"' in line)
    assert len(line) < 4000
    summary = json.loads(line)["data"]["summary"]
    assert summary["status"] == "success"
    assert summary["tests_count"] == 500


def test_nothing_is_recorded_outside_context(journal):
    from prototype.journal import JournalCallbackHandler, journal_context

    with journal_context(JournalCallbackHandler(journal)):
        pass
    GenericFakeChatModel(messages=iter([answer()])).invoke("q")
    ping.invoke({"x": "1"})
    assert journal.events == []
