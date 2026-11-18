"""Content generation providers with a dependable no-LLM fallback."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from .model_provider import OllamaModelProvider


@dataclass(frozen=True)
class GeneratedContent:
    text: str
    mode: str
    model_name: str | None


class Generator(Protocol):
    def generate(self, prompt: str) -> GeneratedContent: ...


class NoLLMContentGenerator:
    """Deterministic template-based generator; never downloads or calls a model."""
    def generate(self, prompt: str) -> GeneratedContent:
        clean = " ".join(prompt.split())
        if not clean:
            raise ValueError("Prompt must not be empty")
        text = (
            f"Topic: {clean}\n\n"
            "Overview\n"
            f"This draft introduces {clean.lower()} and organizes the topic into a clear starting point.\n\n"
            "Key points\n"
            "- Define the main objective and the intended audience.\n"
            "- Explain the core concepts with verifiable details and examples.\n"
            "- Identify limitations, assumptions, and items that need confirmation.\n\n"
            "Next steps\n"
            "Review factual claims, add trustworthy references where needed, and revise this draft "
            "for the intended audience before publication."
        )
        return GeneratedContent(text=text, mode="no_llm", model_name=None)


class OllamaContentGenerator:
    def __init__(self, provider: OllamaModelProvider, model_name: str):
        self.provider = provider
        self.model_name = model_name

    def generate(self, prompt: str) -> GeneratedContent:
        clean = prompt.strip()
        if not clean:
            raise ValueError("Prompt must not be empty")
        text = self.provider.generate(
            self.model_name,
            clean,
            system=(
                "You are a careful content drafting assistant. Do not invent citations. "
                "Flag uncertainty and avoid requesting or exposing sensitive personal information."
            ),
        )
        return GeneratedContent(text=text, mode="ollama", model_name=self.model_name)


def create_generator(mode: str, *, model_name: str | None = None,
                     provider: OllamaModelProvider | None = None) -> Generator:
    if mode == "no_llm":
        return NoLLMContentGenerator()
    if mode == "ollama":
        if provider is None:
            provider = OllamaModelProvider()
        if not model_name:
            raise ValueError("Choose an installed Ollama model before generation")
        return OllamaContentGenerator(provider, model_name)
    raise ValueError("mode must be 'no_llm' or 'ollama'")
