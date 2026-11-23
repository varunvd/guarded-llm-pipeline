"""Database models. Works on PostgreSQL (production) and SQLite (tests)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    """Naive UTC timestamp, so values compare the same on SQLite and Postgres."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(40), default="none")

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    item: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # processing | shipped | delivered
    placed_at: Mapped[datetime] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="orders")


class Policy(Base):
    """One knowledge-base entry. The model may only state what these say."""

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    # Search terms customers actually use, in any supported language. Used for
    # retrieval only; never shown to the model.
    keywords: Mapped[str] = mapped_column(Text, default="")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)

    # queued -> processing -> sent | awaiting_review -> sent | rejected
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # auto_sent | approved | edited | rejected
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Why a person has to look at it. Empty when it was sent automatically.
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    # True when the draft shown to the reviewer is the safe template because
    # no generated candidate passed validation.
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False)

    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    customer: Mapped[Customer] = relationship()
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="ticket", order_by="Attempt.number", cascade="all, delete-orphan"
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="ticket", order_by="Event.id", cascade="all, delete-orphan"
    )


class Attempt(Base):
    """One model call and the verdict on what it produced."""

    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    candidate: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="attempts")


class Event(Base):
    """Append-only audit trail for a ticket."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="events")


def make_engine(url: str):
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            # One shared connection, so every session sees the same in-memory DB.
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(url: str) -> sessionmaker[Session]:
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
