"""The contract between the pipeline and any model provider.

A provider returns a *candidate*: a structured proposal, not an answer. Nothing
in a candidate reaches a customer until the validation layer has passed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..context import Context


class ProposedAction(BaseModel):
    """What the reply commits the business to, stated explicitly so code can check it."""

    type: Literal["none", "refund", "replacement"] = "none"
    order_id: str | None = None
    amount_cents: int | None = None


class Candidate(BaseModel):
    reply: str = Field(description="The message to send to the customer.")
    cited_policies: list[str] = Field(
        default_factory=list,
        description="Ids of the policies the reply relies on, taken from the context.",
    )
    proposed_action: ProposedAction = Field(default_factory=ProposedAction)
    needs_human: bool = Field(
        default=False,
        description="True when a person should handle this ticket instead of an automatic reply.",
    )
    needs_human_reason: str | None = None


@dataclass
class GenerationRequest:
    subject: str
    body: str
    context: Context
    system: str
    user: str
    # Failed checks from the previous attempt, or None on the first attempt.
    feedback: list[str] | None = None
    attempt: int = 1


@dataclass
class GenerationResult:
    candidate: Candidate | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    # Set when no usable candidate came back: API failure, refusal, bad output.
    error: str | None = None


class LLMProvider(Protocol):
    name: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...
