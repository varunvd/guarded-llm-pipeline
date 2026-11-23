from ..config import Settings
from .base import Candidate, GenerationRequest, GenerationResult, LLMProvider, ProposedAction

__all__ = [
    "Candidate",
    "GenerationRequest",
    "GenerationResult",
    "LLMProvider",
    "ProposedAction",
    "make_provider",
]


def make_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.llm_model)
    if settings.llm_provider == "simulated":
        from .simulated import SimulatedProvider

        return SimulatedProvider()
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
