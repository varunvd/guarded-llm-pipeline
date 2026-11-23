"""The pipeline: context -> generate -> validate -> route.

The rule the whole project is built around: the model proposes a candidate and
this code decides what happens to it. A candidate is sent only when

  1. every validation check passes,
  2. no risk rule says a person must handle the ticket,
  3. the model itself did not ask for a person,
  4. the reply commits the business to no action (refund, replacement), and
  5. automatic sending is switched on.

Anything else goes to the review queue with the reasons attached.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .context import build_context
from .db import Attempt, Event, Ticket
from .llm.base import GenerationRequest, LLMProvider
from .prompts import SYSTEM, render_user
from .risk import classify_risk
from .textutil import detect_language, first_name
from .validation import run_checks


@dataclass
class Outcome:
    ticket_id: int
    status: str  # "sent" | "awaiting_review"
    attempts: int
    passed_validation: bool
    review_reasons: list[str]


def fallback_reply(customer_name: str, ticket_text: str) -> str:
    """A reply that is safe to show a reviewer when no candidate passed."""
    name = first_name(customer_name)
    if detect_language(ticket_text) == "es":
        return (
            f"Hola {name}, gracias por escribirnos. He pasado su mensaje a una persona de "
            "nuestro equipo, que le responderá en breve."
        )
    return (
        f"Hi {name}, thank you for your message. I've passed it to a member of our team, "
        "who will reply to you directly."
    )


def _log(session: Session, ticket: Ticket, type_: str, **data) -> None:
    session.add(Event(ticket_id=ticket.id, type=type_, data=data))


def process_ticket(
    session: Session, ticket_id: int, provider: LLMProvider, settings: Settings
) -> Outcome:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id}")
    if ticket.status not in ("queued", "processing"):
        # Delivery is at-least-once, so a ticket can arrive twice. Processing it
        # again would overwrite a decision that has already been made.
        return Outcome(ticket.id, ticket.status, len(ticket.attempts), False, [])

    ticket.status = "processing"
    session.commit()

    ctx = build_context(session, ticket)
    _log(
        session,
        ticket,
        "context_built",
        policies=sorted(ctx.policy_ids()),
        orders=[o.id for o in ctx.orders],
    )

    risk_reasons = classify_risk(ticket.subject, ticket.body)
    feedback: list[str] | None = None
    accepted = None
    number = 0
    # Attempts left behind by a run that crashed part-way keep their numbers.
    already = len(ticket.attempts)

    for number in range(1, settings.max_attempts + 1):
        request = GenerationRequest(
            subject=ticket.subject,
            body=ticket.body,
            context=ctx,
            system=SYSTEM,
            user=render_user(ticket.subject, ticket.body, ctx, ticket.created_at, feedback),
            feedback=feedback,
            attempt=number,
        )
        result = provider.generate(request)

        if result.candidate is None:
            checks = []
            feedback = [f"generation failed: {result.error}"]
        else:
            checks = run_checks(
                result.candidate, ctx, ticket.subject, ticket.body, ticket.created_at, settings
            )
            feedback = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
        passed = result.candidate is not None and not feedback

        session.add(
            Attempt(
                ticket_id=ticket.id,
                number=already + number,
                provider=provider.name,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=settings.cost_usd(result.input_tokens, result.output_tokens),
                latency_ms=result.latency_ms,
                candidate=result.candidate.model_dump() if result.candidate else None,
                error=result.error,
                checks=[c.as_dict() for c in checks],
                passed=passed,
            )
        )
        session.commit()

        if passed:
            accepted = result.candidate
            break

    # ---- routing ----------------------------------------------------------
    reasons = [f"risk rule: {r}" for r in risk_reasons]

    if accepted is None:
        ticket.used_fallback = True
        ticket.draft_reply = fallback_reply(ctx.customer_name, f"{ticket.subject}\n{ticket.body}")
        ticket.proposed_action = None
        reasons.append(f"no draft passed validation in {number} attempts")
    else:
        ticket.draft_reply = accepted.reply
        action = accepted.proposed_action
        ticket.proposed_action = action.model_dump() if action.type != "none" else None
        if accepted.needs_human:
            reasons.append(
                "model asked for a person"
                + (f": {accepted.needs_human_reason}" if accepted.needs_human_reason else "")
            )
        if action.type != "none":
            reasons.append(f"reply commits to a {action.type}, which a person must approve")
            if (
                action.type == "refund"
                and (action.amount_cents or 0) > settings.refund_review_limit_cents
            ):
                reasons.append("refund is above the support-lead approval limit")
        if not settings.auto_send:
            reasons.append("automatic sending is switched off")

    if reasons:
        ticket.status = "awaiting_review"
        ticket.review_reasons = reasons
        _log(session, ticket, "routed_to_review", reasons=reasons, attempts=number)
    else:
        ticket.status = "sent"
        ticket.resolution = "auto_sent"
        ticket.final_reply = ticket.draft_reply
        ticket.review_reasons = []
        _log(session, ticket, "auto_sent", attempts=number)

    session.commit()
    return Outcome(ticket.id, ticket.status, number, accepted is not None, reasons)
