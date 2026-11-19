"""Ollama model discovery and local generation; no third-party dependencies."""
from __future__ import annotations
import json
import os
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


class OllamaUnavailableError(RuntimeError):
    pass


class OllamaModelProvider:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        configured_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self.base_url = configured_url.rstrip("/")
        self.timeout = timeout

    def list_models(self) -> list[str]:
        request = Request(self.base_url + "/api/tags", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise OllamaUnavailableError(
                f"Could not query Ollama at {self.base_url}. Is Ollama running?"
            ) from exc
        models = payload.get("models", [])
        return sorted({
            str(item.get("name", "")).strip()
            for item in models
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        })

    def model_version(self, model_name: str) -> str | None:
        """Return Ollama's model digest when the local tags endpoint exposes it."""
        request = Request(self.base_url + "/api/tags", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            return None
        for item in payload.get("models", []):
            if isinstance(item, dict) and item.get("name") == model_name:
                digest = item.get("digest")
                return str(digest) if digest else None
        return None

    def generate(self, model: str, prompt: str, *, system: str | None = None) -> str:
        if not model or not model.strip():
            raise ValueError("A model name is required for Ollama generation")
        body = {"model": model, "prompt": prompt, "stream": False}
        if system:
            body["system"] = system
        request = Request(
            self.base_url + "/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise OllamaUnavailableError(f"Ollama generation failed for model '{model}'") from exc
        result = payload.get("response")
        if not isinstance(result, str) or not result.strip():
            raise OllamaUnavailableError("Ollama returned an empty response")
        return result.strip()
