"""Model providers: Gemini, OpenAI-compatible (NVIDIA NIM, OpenAI, Ollama...), mock."""
from ..config import Settings
from .base import (
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    RateLimitError,
    ToolCall,
    ToolSpec,
)
from .mock import MockProvider


def create_provider(settings: Settings) -> ModelProvider:
    """Instantiate the provider selected by ``settings.provider``."""
    kind = (settings.provider or "gemini").lower()
    if kind == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(
            api_key=settings.gemini_api_key or "",
            model=settings.gemini_model,
            temperature=settings.temperature,
            timeout=settings.request_timeout,
        )
    if kind in {"openai", "nvidia", "openai-compatible", "openai_compatible"}:
        from .openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key or "",
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            temperature=settings.temperature,
            timeout=settings.request_timeout,
        )
    if kind == "mock":
        return MockProvider()
    raise ProviderError(f"unknown MODEL_PROVIDER '{settings.provider}' (use gemini | openai | mock)")


__all__ = [
    "Message",
    "ModelProvider",
    "ModelResponse",
    "ProviderError",
    "RateLimitError",
    "ToolCall",
    "ToolSpec",
    "MockProvider",
    "create_provider",
]
