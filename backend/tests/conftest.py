from datetime import datetime, timedelta

import pytest

from guarded.config import Settings
from guarded.context import build_context
from guarded.db import Ticket, make_session_factory
from guarded.seed import seed_reference_data

NOW = datetime(2026, 6, 15, 12, 0, 0)


@pytest.fixture
def settings():
    return Settings(_env_file=None, auto_send=True, max_attempts=3)


@pytest.fixture
def sessions():
    factory = make_session_factory("sqlite://")
    with factory() as session:
        seed_reference_data(session, now=NOW)
    return factory


@pytest.fixture
def live_sessions():
    """Seeded relative to the real clock, for tests that create tickets through the API."""
    factory = make_session_factory("sqlite://")
    with factory() as session:
        seed_reference_data(session)
    return factory


@pytest.fixture
def session(sessions):
    with sessions() as s:
        yield s


@pytest.fixture
def make_ticket(session):
    def _make(customer_id="C-1001", subject="Question", body="Hello", days_ago=0):
        ticket = Ticket(
            customer_id=customer_id,
            subject=subject,
            body=body,
            created_at=NOW - timedelta(days=days_ago),
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


@pytest.fixture
def context_for(session):
    return lambda ticket: build_context(session, ticket)
