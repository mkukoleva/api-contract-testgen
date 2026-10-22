"""LLM connection settings; constructor doubles, never a paid model call."""

import pytest

from prototype.llm import model


@pytest.fixture
def constructor(monkeypatch):
    for name, value in {"DEEPCODE_API_KEY": "test-key", "DEEPCODE_BASE_URL": "https://example.invalid/v1",
                        "DEEPCODE_MODEL": "test-model"}.items():
        monkeypatch.setenv(name, value)
    for name in ("DEEPCODE_TIMEOUT_SECONDS", "DEEPCODE_MAX_RETRIES",
                 "DEEPCODE_ENABLE_THINKING", "DEEPCODE_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(model, "ChatOpenAI", lambda **kwargs: calls.append(kwargs) or kwargs)
    return calls


def test_model_defaults_are_preserved(constructor):
    result = model.build_model()
    assert result["timeout"] == 60
    assert result["max_retries"] == 1
    assert result["temperature"] == 0
    assert result["api_key"] == "test-key"
    assert result["base_url"] == "https://example.invalid/v1"
    assert result["model"] == "test-model"
    assert "extra_body" not in result and "max_tokens" not in result


@pytest.mark.parametrize("timeout,retries", [("2.5", "0"), ("120", "3")])
def test_connection_limits_can_be_configured(constructor, monkeypatch, timeout, retries):
    monkeypatch.setenv("DEEPCODE_TIMEOUT_SECONDS", timeout)
    monkeypatch.setenv("DEEPCODE_MAX_RETRIES", retries)
    result = model.build_model()
    assert result["timeout"] == float(timeout)
    assert result["max_retries"] == int(retries)


@pytest.mark.parametrize("name,values", [
    ("DEEPCODE_TIMEOUT_SECONDS", ["", "0", "-1", "nan", "inf", "oops"]),
    ("DEEPCODE_MAX_RETRIES", ["", "-1", "1.5", "nan", "inf", "oops"]),
    ("DEEPCODE_ENABLE_THINKING", ["", "yes", "0", "oops"]),
    ("DEEPCODE_MAX_OUTPUT_TOKENS", ["", "0", "-1", "1.5", "nan", "inf", "oops"]),
])
def test_invalid_limits_do_not_construct_model(constructor, monkeypatch, name, values):
    for value in values:
        monkeypatch.setenv(name, value)
        with pytest.raises(RuntimeError, match=name):
            model.build_model()
        assert constructor == []


@pytest.mark.parametrize("name", ["DEEPCODE_API_KEY", "DEEPCODE_BASE_URL", "DEEPCODE_MODEL"])
def test_missing_credentials_still_fail_before_constructor(constructor, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(RuntimeError, match=name):
        model.build_model()
    assert constructor == []


def test_configuration_never_logs_api_key(constructor, capsys):
    model.build_model()
    assert "test-key" not in capsys.readouterr().out


def test_metadata_reads_actual_sdk_parameters_without_serializing_credentials():
    import json
    from langchain_openai import ChatOpenAI
    client = ChatOpenAI(model="configured-qwen", api_key="dummy-private-sdk-key",
                        base_url="https://example.invalid/v1", temperature=0, timeout=17, max_retries=0)
    data = model.describe_model(client)
    assert data == {"model": "configured-qwen", "temperature": 0, "timeout_seconds": 17, "max_retries": 0}
    assert "dummy-private-sdk-key" not in json.dumps(data)


@pytest.mark.parametrize("setting,expected", [("false", False), ("true", True)])
def test_qwen_options_reach_actual_sdk_request_and_safe_metadata(constructor, monkeypatch, setting, expected):
    import json
    from langchain_openai import ChatOpenAI
    monkeypatch.setenv("DEEPCODE_ENABLE_THINKING", setting)
    monkeypatch.setenv("DEEPCODE_MAX_OUTPUT_TOKENS", "8000")
    kwargs = model.build_model()
    client = ChatOpenAI(**kwargs)
    payload = client._get_request_payload([{"role": "user", "content": "offline control"}])
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is expected
    assert payload["max_completion_tokens"] == 8000
    assert client.request_timeout == 60
    metadata = model.describe_model(client)
    assert metadata["enable_thinking_requested"] is expected
    assert metadata["max_output_tokens"] == 8000
    assert "test-key" not in json.dumps(metadata)
