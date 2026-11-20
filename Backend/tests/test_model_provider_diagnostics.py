import json
from urllib.error import HTTPError, URLError

import pytest

from agent_runtime.model_provider import OllamaModelProvider, OllamaUnavailableError


class FakeResponse:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


def test_generation_connection_error_has_url_and_cause(monkeypatch):
    def fail(*args, **kwargs):
        raise URLError("connection refused")
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", fail)
    with pytest.raises(OllamaUnavailableError, match="connection refused") as caught:
        OllamaModelProvider("http://ollama:11434", timeout=17).generate("qwen3:4b", "hello")
    assert caught.value.__cause__ is not None
    assert "http://ollama:11434/api/generate" in str(caught.value)
    assert "17s" in str(caught.value)


def test_generation_http_error_includes_status_body(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("http://localhost:11434/api/generate", 404, "Not Found", {}, None)
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", fail)
    with pytest.raises(OllamaUnavailableError, match="HTTP 404"):
        OllamaModelProvider().generate("missing-model", "hello")


def test_generation_ollama_error_body_is_not_treated_as_response(monkeypatch):
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", lambda *a, **k: FakeResponse(
        json.dumps({"error": "model requires more memory"}).encode()))
    with pytest.raises(OllamaUnavailableError, match="requires more memory"):
        OllamaModelProvider().generate("qwen3:4b", "hello")


def test_model_discovery_http_error_includes_status(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("http://localhost:11434/api/tags", 503, "Unavailable", {}, None)
    monkeypatch.setattr("agent_runtime.model_provider.urlopen", fail)
    with pytest.raises(OllamaUnavailableError, match="HTTP 503"):
        OllamaModelProvider().list_models()
