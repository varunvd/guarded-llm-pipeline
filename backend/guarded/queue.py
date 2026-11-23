"""Work queue between ticket intake and the pipeline worker.

The queue is the boundary that keeps slow model calls out of the request path.
Delivery is at-least-once: a ticket id is moved to a "processing" list when a
worker takes it and removed only when the worker acknowledges it, so a worker
that dies mid-ticket does not lose the work.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

QUEUE_KEY = "guarded:tickets"
PROCESSING_KEY = "guarded:tickets:processing"


class TicketQueue(Protocol):
    def enqueue(self, ticket_id: int) -> None: ...

    def dequeue(self, timeout: float = 1.0) -> int | None: ...

    def ack(self, ticket_id: int) -> None: ...

    def requeue_unacked(self) -> int: ...

    def depth(self) -> int: ...


class InMemoryQueue:
    """Same semantics as the Redis queue, for tests and single-process runs."""

    def __init__(self) -> None:
        self._pending: deque[int] = deque()
        self._processing: list[int] = []

    def enqueue(self, ticket_id: int) -> None:
        self._pending.append(ticket_id)

    def dequeue(self, timeout: float = 0.0) -> int | None:
        if not self._pending:
            return None
        ticket_id = self._pending.popleft()
        self._processing.append(ticket_id)
        return ticket_id

    def ack(self, ticket_id: int) -> None:
        if ticket_id in self._processing:
            self._processing.remove(ticket_id)

    def requeue_unacked(self) -> int:
        moved = len(self._processing)
        self._pending.extendleft(reversed(self._processing))
        self._processing.clear()
        return moved

    def depth(self) -> int:
        return len(self._pending)


class RedisQueue:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def enqueue(self, ticket_id: int) -> None:
        self._redis.lpush(QUEUE_KEY, ticket_id)

    def dequeue(self, timeout: float = 1.0) -> int | None:
        # Atomic move: the id is never in neither list.
        value = self._redis.blmove(QUEUE_KEY, PROCESSING_KEY, timeout, "RIGHT", "LEFT")
        return int(value) if value is not None else None

    def ack(self, ticket_id: int) -> None:
        self._redis.lrem(PROCESSING_KEY, 1, str(ticket_id))

    def requeue_unacked(self) -> int:
        """Return work a crashed worker took but never finished.

        Safe with a single worker at start-up. With several workers this needs
        per-worker processing lists or visibility timeouts; see the README.
        """
        moved = 0
        while self._redis.lmove(PROCESSING_KEY, QUEUE_KEY, "RIGHT", "RIGHT") is not None:
            moved += 1
        return moved

    def depth(self) -> int:
        return int(self._redis.llen(QUEUE_KEY))


def make_queue(redis_url: str) -> TicketQueue:
    return RedisQueue(redis_url) if redis_url else InMemoryQueue()
