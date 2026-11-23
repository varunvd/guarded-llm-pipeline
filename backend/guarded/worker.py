"""Queue worker: takes ticket ids off the queue and runs the pipeline on each."""

from __future__ import annotations

import logging
import signal

from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .db import Ticket, make_session_factory
from .llm import LLMProvider, make_provider
from .pipeline import process_ticket
from .queue import TicketQueue, make_queue
from .seed import seed_reference_data

log = logging.getLogger("guarded.worker")


def handle(
    ticket_id: int, sessions: sessionmaker[Session], provider: LLMProvider, settings: Settings
) -> None:
    with sessions() as session:
        try:
            outcome = process_ticket(session, ticket_id, provider, settings)
            log.info(
                "ticket=%s status=%s attempts=%s reasons=%s",
                outcome.ticket_id,
                outcome.status,
                outcome.attempts,
                outcome.review_reasons,
            )
        except Exception:
            # Never drop a ticket silently: park it where a person will see it.
            log.exception("ticket=%s pipeline crashed", ticket_id)
            session.rollback()
            ticket = session.get(Ticket, ticket_id)
            if ticket is not None and ticket.status in ("queued", "processing"):
                ticket.status = "awaiting_review"
                ticket.review_reasons = ["pipeline error; see worker logs"]
                session.commit()


def drain(
    queue: TicketQueue, sessions: sessionmaker[Session], provider: LLMProvider, settings: Settings
) -> int:
    """Process everything currently queued. Returns how many tickets were handled."""
    handled = 0
    while (ticket_id := queue.dequeue(timeout=0.01)) is not None:
        handle(ticket_id, sessions, provider, settings)
        queue.ack(ticket_id)
        handled += 1
    return handled


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    sessions = make_session_factory(settings.database_url)
    with sessions() as session:
        seed_reference_data(session)
    queue = make_queue(settings.redis_url)
    provider = make_provider(settings)

    recovered = queue.requeue_unacked()
    log.info("worker started provider=%s recovered=%s", provider.name, recovered)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        ticket_id = queue.dequeue(timeout=2.0)
        if ticket_id is None:
            continue
        handle(ticket_id, sessions, provider, settings)
        queue.ack(ticket_id)


if __name__ == "__main__":
    main()
