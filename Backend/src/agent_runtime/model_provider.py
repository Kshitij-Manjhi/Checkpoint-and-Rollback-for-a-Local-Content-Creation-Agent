"""Ollama model discovery and local generation; no third-party dependencies."""
from __future__ import annotations
import json
import os
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


class OllamaUnavailableError(RuntimeError):
    pass


class OllamaModelProvider:
    def __init__(self, base_url: str | None = None, timeout: float = 120.0):
        configured_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self.base_url = configured_url.rstrip("/")
        self.timeout = timeout

    def list_models(self) -> list[str]:
        request = Request(self.base_url + "/api/tags", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise OllamaUnavailableError(
                f"Ollama model discovery at {self.base_url}/api/tags returned HTTP {exc.code} {exc.reason}"
                + (f": {detail}" if detail else "")
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise OllamaUnavailableError(
                f"Could not query Ollama at {self.base_url}/api/tags: {exc}. "
                "Check that Ollama is running and OLLAMA_BASE_URL is correct."
            ) from exc
        if not isinstance(payload, dict):
            raise OllamaUnavailableError(f"Ollama returned an invalid model list from {self.base_url}/api/tags")
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
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1500]
            raise OllamaUnavailableError(
                f"Ollama generation at {self.base_url}/api/generate returned HTTP {exc.code} {exc.reason} "
                f"for model '{model}'" + (f": {detail}" if detail else "")
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise OllamaUnavailableError(
                f"Ollama generation at {self.base_url}/api/generate failed for model '{model}': {exc}. "
                f"Timeout is {self.timeout:g}s; check Ollama status, model availability, and OLLAMA_BASE_URL."
            ) from exc
        if not isinstance(payload, dict):
            raise OllamaUnavailableError(f"Ollama returned an invalid generation response for model '{model}'")
        if payload.get("error"):
            raise OllamaUnavailableError(f"Ollama could not generate with model '{model}': {payload['error']}")
        result = payload.get("response")
        if not isinstance(result, str) or not result.strip():
            raise OllamaUnavailableError("Ollama returned an empty response")
        return result.strip()
