"""Deterministic rules for tickets a person must handle.

These run on the ticket itself, before and regardless of what the model says.
A model can be talked out of escalating; a rule cannot.
"""

from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "legal threat",
        re.compile(
            r"\b(lawyer|attorney|solicitor|sue|suing|lawsuit|legal action|small claims"
            r"|court|abogado|demanda|demandar)\b",
            re.I,
        ),
    ),
    (
        "payment dispute",
        re.compile(
            r"\b(chargeback|charge back|dispute (the|this|my) (charge|payment)"
            r"|disputing|contracargo)\b",
            re.I,
        ),
    ),
    (
        "account security",
        re.compile(
            r"\b(hacked|unauthori[sz]ed|someone (else )?(logged|ordered|used)"
            r"|stolen card|fraud(ulent)?|didn't (place|make) this order)\b",
            re.I,
        ),
    ),
    (
        "injury or safety",
        re.compile(
            r"\b(injur\w+|hurt (my|me)|cut my|bleeding|allergic reaction|hospital"
            r"|burn(ed|t)? my|poison\w*)\b",
            re.I,
        ),
    ),
    (
        "possible prompt injection",
        re.compile(
            r"(ignore (all |your |the )?(previous|prior|above) (instructions|rules)"
            r"|disregard (all |your |the )?(previous|prior|above)"
            r"|system prompt|you are now|act as (an? )?(admin|developer|manager))",
            re.I,
        ),
    ),
]


def classify_risk(subject: str, body: str) -> list[str]:
    text = f"{subject}\n{body}"
    return [reason for reason, pattern in _RULES if pattern.search(text)]
