"""Claude provider, using the official Anthropic SDK with structured outputs."""

from __future__ import annotations

import time

import anthropic

from .base import Candidate, GenerationRequest, GenerationResult


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str) -> None:
        # Credentials come from the environment (ANTHROPIC_API_KEY or a stored profile).
        self._client = anthropic.Anthropic()
        self._model = model

    def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.monotonic()

        def result(candidate=None, error=None, usage=None) -> GenerationResult:
            return GenerationResult(
                candidate=candidate,
                model=self._model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=16000,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_format=Candidate,
            )
        except anthropic.RateLimitError as exc:
            return result(error=f"rate limited: {exc.message}")
        except anthropic.APIStatusError as exc:
            return result(error=f"API error {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            return result(error=f"connection error: {exc}")

        # A refusal or a truncated reply is not a candidate. The pipeline treats
        # it like any other failed attempt and, if it persists, hands the ticket
        # to a person.
        if response.stop_reason == "refusal":
            return result(error="model declined to answer", usage=response.usage)
        if response.stop_reason == "max_tokens":
            return result(error="output was cut off at max_tokens", usage=response.usage)
        if response.parsed_output is None:
            return result(error="model returned no structured output", usage=response.usage)

        return result(candidate=response.parsed_output, usage=response.usage)
