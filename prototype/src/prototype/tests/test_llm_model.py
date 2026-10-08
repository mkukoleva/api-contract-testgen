"""LLM connection settings; constructor doubles, never a paid model call."""

import pytest

from prototype.llm import model


@pytest.fixture
def constructor(monkeypatch):
    for name, value in {"DEEPCODE_API_KEY": "test-key", "DEEPCODE_BASE_URL": "https://example.invalid/v1",
                        "DEEPCODE_MODEL": "test-model"}.items():
        monkeypatch.setenv(name, value)
    for name in ("DEEPCODE_TIMEOUT_SECONDS", "DEEPCODE_MAX_RETRIES"):
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
