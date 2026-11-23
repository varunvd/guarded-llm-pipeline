"""Context retrieval: gather what the model needs to answer one ticket.

Most invented answers come from a gap in the context, so this step runs before
any generation. It collects the customer's own record and orders, and the
knowledge-base policies most relevant to the ticket.

Retrieval here is deliberately simple (weighted keyword overlap over a small
knowledge base) so the project runs with no extra services. Swapping in
embeddings or Postgres full-text search means replacing `rank_policies` only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Customer, Order, Policy, Ticket

_WORD = re.compile(r"[a-záéíóúñü]+")
_STOP = frozenset(
    "a an and are as at be but by can do for from have i if in is it me my of on or our so "
    "that the this to was we were what when will with you your not no please would could "
    "el la los las un una y de que en es por para con mi me no se lo le su al del".split()
)

# Policies every reply must respect, whatever the ticket is about.
ALWAYS_INCLUDE = ("tone-and-scope",)


@dataclass
class OrderView:
    id: str
    item: str
    amount_cents: int
    status: str
    placed_at: datetime
    delivered_at: datetime | None


@dataclass
class PolicyView:
    id: str
    title: str
    body: str


@dataclass
class Context:
    customer_id: str
    customer_name: str
    customer_email: str
    plan: str
    orders: list[OrderView] = field(default_factory=list)
    policies: list[PolicyView] = field(default_factory=list)

    def order(self, order_id: str | None) -> OrderView | None:
        return next((o for o in self.orders if o.id == order_id), None)

    def policy_ids(self) -> set[str]:
        return {p.id for p in self.policies}

    def as_text(self) -> str:
        """Everything the model was shown, as one string. Used for grounding checks."""
        lines = [f"Customer: {self.customer_name} <{self.customer_email}>, plan: {self.plan}"]
        for o in self.orders:
            delivered = o.delivered_at.date().isoformat() if o.delivered_at else "not delivered"
            lines.append(
                f"Order {o.id}: {o.item}, {format_money(o.amount_cents)}, status {o.status}, "
                f"placed {o.placed_at.date().isoformat()}, delivered {delivered}"
            )
        for p in self.policies:
            lines.append(f"[{p.id}] {p.title}: {p.body}")
        return "\n".join(lines)


def format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _stem(word: str) -> str:
    """Fold plurals, so "discount" matches "discounts". Crude, but enough here."""
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


def rank_policies(query: str, policies: list[Policy], k: int = 4) -> list[Policy]:
    """Score each policy by TF-IDF-weighted overlap with the ticket text."""
    docs = {
        p.id: Counter(tokenize(f"{p.title} {p.title} {p.keywords} {p.keywords} {p.body}"))
        for p in policies
    }
    n = len(policies) or 1
    df = Counter(term for counts in docs.values() for term in counts)
    q_terms = set(tokenize(query))

    def score(p: Policy) -> float:
        counts = docs[p.id]
        length = sum(counts.values()) or 1
        return sum((counts[t] / length) * math.log(1 + n / df[t]) for t in q_terms if t in counts)

    ranked = sorted(policies, key=lambda p: (-score(p), p.id))
    return [p for p in ranked if score(p) > 0][:k]


def build_context(session: Session, ticket: Ticket) -> Context:
    customer = session.get(Customer, ticket.customer_id)
    if customer is None:
        raise ValueError(f"ticket {ticket.id} references unknown customer {ticket.customer_id}")

    orders = session.scalars(
        select(Order).where(Order.customer_id == customer.id).order_by(Order.placed_at.desc())
    ).all()
    all_policies = session.scalars(select(Policy).order_by(Policy.id)).all()

    chosen = {p.id: p for p in all_policies if p.id in ALWAYS_INCLUDE}
    for p in rank_policies(f"{ticket.subject} {ticket.body}", list(all_policies)):
        chosen.setdefault(p.id, p)

    return Context(
        customer_id=customer.id,
        customer_name=customer.name,
        customer_email=customer.email,
        plan=customer.plan,
        orders=[
            OrderView(o.id, o.item, o.amount_cents, o.status, o.placed_at, o.delivered_at)
            for o in orders
        ],
        policies=[PolicyView(p.id, p.title, p.body) for p in chosen.values()],
    )
