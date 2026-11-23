"""HTTP API: ticket intake, the review queue, and pipeline statistics."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .db import Attempt, Customer, Event, Ticket, make_session_factory
from .llm import LLMProvider, make_provider
from .queue import InMemoryQueue, TicketQueue, make_queue
from .seed import DEMO_TICKETS, seed_reference_data
from .worker import drain


class TicketIn(BaseModel):
    customer_id: str
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=5000)


class ReviewIn(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    reply: str | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=2000)


def ticket_summary(t: Ticket) -> dict:
    return {
        "id": t.id,
        "customer_id": t.customer_id,
        "customer_name": t.customer.name if t.customer else None,
        "subject": t.subject,
        "status": t.status,
        "resolution": t.resolution,
        "review_reasons": t.review_reasons or [],
        "used_fallback": t.used_fallback,
        "attempts": len(t.attempts),
        "created_at": t.created_at.isoformat(),
    }


def ticket_detail(t: Ticket) -> dict:
    return {
        **ticket_summary(t),
        "body": t.body,
        "customer": {"name": t.customer.name, "email": t.customer.email, "plan": t.customer.plan},
        "orders": [
            {
                "id": o.id,
                "item": o.item,
                "amount_cents": o.amount_cents,
                "status": o.status,
                "placed_at": o.placed_at.isoformat(),
                "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
            }
            for o in t.customer.orders
        ],
        "draft_reply": t.draft_reply,
        "proposed_action": t.proposed_action,
        "final_reply": t.final_reply,
        "reviewer_note": t.reviewer_note,
        "attempt_log": [
            {
                "number": a.number,
                "provider": a.provider,
                "model": a.model,
                "passed": a.passed,
                "error": a.error,
                "candidate": a.candidate,
                "checks": a.checks,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "cost_usd": a.cost_usd,
                "latency_ms": a.latency_ms,
            }
            for a in t.attempts
        ],
        "events": [
            {"type": e.type, "data": e.data, "at": e.created_at.isoformat()} for e in t.events
        ],
    }


def create_app(
    settings: Settings | None = None,
    sessions: sessionmaker[Session] | None = None,
    queue: TicketQueue | None = None,
    provider: LLMProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    sessions = sessions or make_session_factory(settings.database_url)
    queue = queue or make_queue(settings.redis_url)
    # With no Redis there is no separate worker, so the API process runs the
    # pipeline itself, after the response has been sent.
    inline = isinstance(queue, InMemoryQueue)
    if inline and provider is None:
        provider = make_provider(settings)

    with sessions() as session:
        seed_reference_data(session)

    app = FastAPI(title="Guarded LLM pipeline", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def db():
        with sessions() as session:
            yield session

    def submit(session: Session, background: BackgroundTasks, data: TicketIn) -> Ticket:
        if session.get(Customer, data.customer_id) is None:
            raise HTTPException(404, f"unknown customer {data.customer_id}")
        ticket = Ticket(customer_id=data.customer_id, subject=data.subject, body=data.body)
        session.add(ticket)
        session.flush()
        session.add(Event(ticket_id=ticket.id, type="received", data={}))
        session.commit()
        queue.enqueue(ticket.id)
        if inline:
            background.add_task(drain, queue, sessions, provider, settings)
        return ticket

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "provider": settings.llm_provider,
            "auto_send": settings.auto_send,
            "queue_depth": queue.depth(),
        }

    @app.get("/api/customers")
    def customers(session: Session = Depends(db)):
        rows = session.scalars(select(Customer).order_by(Customer.id)).all()
        return [{"id": c.id, "name": c.name} for c in rows]

    @app.post("/api/tickets", status_code=202)
    def create_ticket(data: TicketIn, background: BackgroundTasks, session: Session = Depends(db)):
        return ticket_summary(submit(session, background, data))

    @app.post("/api/demo", status_code=202)
    def load_demo(background: BackgroundTasks, session: Session = Depends(db)):
        """Queue the sample tickets, so there is something to look at."""
        created = [
            submit(session, background, TicketIn(customer_id=c, subject=s, body=b)).id
            for c, s, b in DEMO_TICKETS
        ]
        return {"queued": created}

    @app.get("/api/tickets")
    def list_tickets(status: str | None = None, session: Session = Depends(db)):
        query = select(Ticket).order_by(Ticket.id.desc()).limit(200)
        if status:
            query = query.where(Ticket.status == status)
        return [ticket_summary(t) for t in session.scalars(query).all()]

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: int, session: Session = Depends(db)):
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "no such ticket")
        return ticket_detail(ticket)

    @app.post("/api/tickets/{ticket_id}/review")
    def review(ticket_id: int, data: ReviewIn, session: Session = Depends(db)):
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "no such ticket")
        if ticket.status != "awaiting_review":
            raise HTTPException(409, f"ticket is {ticket.status}, not awaiting review")

        if data.decision == "reject":
            ticket.status, ticket.resolution, ticket.final_reply = "rejected", "rejected", None
        elif data.decision == "edit":
            if not data.reply or not data.reply.strip():
                raise HTTPException(422, "an edited reply cannot be empty")
            ticket.status, ticket.resolution = "sent", "edited"
            ticket.final_reply = data.reply.strip()
        else:
            ticket.status, ticket.resolution = "sent", "approved"
            ticket.final_reply = ticket.draft_reply
        ticket.reviewer_note = data.note
        session.add(
            Event(ticket_id=ticket.id, type=f"review_{data.decision}", data={"note": data.note})
        )
        session.commit()
        return ticket_detail(ticket)

    @app.get("/api/stats")
    def stats(session: Session = Depends(db)):
        tickets = session.scalars(select(Ticket)).all()
        attempts = session.scalars(select(Attempt)).all()
        by_status = Counter(t.status for t in tickets)
        by_resolution = Counter(t.resolution for t in tickets if t.resolution)
        processed = [t for t in tickets if t.status not in ("queued", "processing")]
        first = [a for a in attempts if a.number == 1]
        failed_checks = Counter(c["name"] for a in attempts for c in a.checks if not c["passed"])
        totals = session.execute(
            select(
                func.coalesce(func.sum(Attempt.cost_usd), 0.0),
                func.coalesce(func.sum(Attempt.input_tokens), 0),
                func.coalesce(func.sum(Attempt.output_tokens), 0),
            )
        ).one()

        def share(part: int, whole: int) -> float | None:
            return round(part / whole, 3) if whole else None

        return {
            "tickets": len(tickets),
            "by_status": dict(by_status),
            "by_resolution": dict(by_resolution),
            "processed": len(processed),
            "auto_sent_share": share(by_resolution["auto_sent"], len(processed)),
            "first_attempt_pass_share": share(sum(a.passed for a in first), len(first)),
            "fallback_share": share(sum(t.used_fallback for t in processed), len(processed)),
            "attempts": len(attempts),
            "avg_attempts": round(len(attempts) / len(processed), 2) if processed else None,
            "failed_checks": dict(failed_checks.most_common()),
            "cost_usd": round(float(totals[0]), 4),
            "input_tokens": int(totals[1]),
            "output_tokens": int(totals[2]),
            "queue_depth": queue.depth(),
        }

    return app


def app() -> FastAPI:
    """Entry point for `uvicorn guarded.api:app --factory`."""
    return create_app()
