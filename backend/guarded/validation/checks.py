"""The validation layer: what must be true before a candidate may be used.

Every check is deterministic code. That is a deliberate choice: the checks are
fast, free, and give the same verdict every time, which makes them testable and
makes the evaluation reproducible. Their limit is that they only catch what can
be expressed as a rule. See the README for where a model-based reviewer fits.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

from ..config import Settings
from ..context import Context
from ..llm.base import Candidate
from ..textutil import detect_language, negated


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Fact extraction
# --------------------------------------------------------------------------

_NUMBER = r"\d[\d,]*(?:\.\d{1,2})?"
_MONEY = re.compile(rf"[$€£]\s?({_NUMBER})|\b({_NUMBER})\s?(?:USD|dollars|dólares|euros)\b", re.I)
_PERCENT = re.compile(r"\b(\d+(?:\.\d+)?)\s?(?:%|percent\b|por ciento\b)", re.I)
_UNITS = {
    "day": "day",
    "days": "day",
    "día": "day",
    "días": "day",
    "dia": "day",
    "dias": "day",
    "hour": "hour",
    "hours": "hour",
    "hora": "hour",
    "horas": "hour",
    "week": "week",
    "weeks": "week",
    "semana": "week",
    "semanas": "week",
    "month": "month",
    "months": "month",
    "mes": "month",
    "meses": "month",
    "year": "year",
    "years": "year",
    "año": "year",
    "años": "year",
}
_DURATION = re.compile(
    r"\b(\d+)(?:\s?(?:-|–|to|a)\s?(\d+))?[\s-]+(?:business\s+|working\s+)?("
    + "|".join(sorted(_UNITS, key=len, reverse=True))
    + r")\b",
    re.I,
)
_CODE = re.compile(r"\b[A-Z]{4,}\d{1,3}\b")
_ORDER = re.compile(r"\bBL-\d{5}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def extract_facts(text: str) -> set[str]:
    """Checkable facts in a text: amounts, percentages, durations, codes, order ids."""
    facts: set[str] = set()
    for m in _MONEY.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        facts.add(f"amount {float(raw):.2f}")
    for m in _PERCENT.finditer(text):
        facts.add(f"{float(m.group(1)):g}%")
    for m in _DURATION.finditer(text):
        unit = _UNITS[m.group(3).lower()]
        for n in (m.group(1), m.group(2)):
            if n:
                facts.add(f"{int(n)} {unit}")
    facts.update(f"code {m.group(0)}" for m in _CODE.finditer(text))
    facts.update(f"order {m.group(0)}" for m in _ORDER.finditer(text))
    return facts


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{\{|\}\}|</?[a-z_]+>|\bTODO\b|lorem ipsum", re.I)
_META = re.compile(
    r"\bas an ai\b|language model|system prompt|my instructions|previous instructions"
    r"|proposed_action|needs_human|cited_policies",
    re.I,
)


def check_format(c: Candidate) -> CheckResult:
    reply = c.reply.strip()
    problems = []
    if not 40 <= len(reply) <= 1800:
        problems.append(f"reply is {len(reply)} characters (allowed 40-1800)")
    if m := _PLACEHOLDER.search(reply):
        problems.append(f"contains a placeholder or markup: {m.group(0)!r}")
    if m := _META.search(reply):
        problems.append(f"mentions internals: {m.group(0)!r}")
    return CheckResult("format", not problems, "; ".join(problems))


def check_citations(c: Candidate, ctx: Context) -> CheckResult:
    unknown = sorted(set(c.cited_policies) - ctx.policy_ids())
    detail = f"cites policies that were not in the context: {', '.join(unknown)}" if unknown else ""
    return CheckResult("citations", not unknown, detail)


def check_grounding(c: Candidate, ctx: Context, ticket_text: str) -> CheckResult:
    """Every checkable fact in the reply must come from the context or the customer."""
    allowed = extract_facts(ctx.as_text()) | extract_facts(ticket_text)
    unsupported = sorted(extract_facts(c.reply) - allowed)
    detail = (
        "states facts that are not in the context: " + ", ".join(unsupported) if unsupported else ""
    )
    return CheckResult("grounding", not unsupported, detail)


def check_action(c: Candidate, ctx: Context, received_at: datetime) -> CheckResult:
    """A proposed refund or replacement must be one the policies actually allow."""
    action = c.proposed_action
    if action.type == "none":
        return CheckResult("action", True)

    order = ctx.order(action.order_id)
    if order is None:
        return CheckResult(
            "action",
            False,
            f"{action.type} names order {action.order_id!r}, which is not "
            "one of this customer's orders",
        )
    if order.status != "delivered" or order.delivered_at is None:
        return CheckResult(
            "action",
            False,
            f"order {order.id} has not been delivered, so it cannot be "
            f"{'refunded as a return' if action.type == 'refund' else 'replaced'}",
        )

    days = (received_at - order.delivered_at).days
    window = 30 if action.type == "refund" else 14
    if days > window:
        return CheckResult(
            "action",
            False,
            f"order {order.id} was delivered {days} days ago, outside the "
            f"{window} day window for a {action.type}",
        )
    if action.type == "refund":
        if not action.amount_cents or action.amount_cents <= 0:
            return CheckResult("action", False, "refund has no amount")
        if action.amount_cents > order.amount_cents:
            return CheckResult(
                "action",
                False,
                f"refund of {action.amount_cents / 100:.2f} is more than the "
                f"order total of {order.amount_cents / 100:.2f}",
            )
    return CheckResult("action", True)


_ALREADY_DONE = re.compile(
    r"\b(?:i|we)(?:'ve| have)?(?: already| just| now)? "
    r"(issued|processed|refunded|credited|cancell?ed|sent (?:you |out )?a replacement)\b"
    r"|\b(?:has|have) been (refunded|processed|credited|cancell?ed)\b"
    r"|\b(?:hemos|he) (reembolsado|cancelado|procesado)\b",
    re.I,
)
_REFUND_PROMISE = re.compile(
    r"\b(?:will|'ll) (?:be )?(?:refund\w*|issue a refund|credit\w*)\b"
    r"|\byou(?:'ll| will) (?:receive|get) (?:a |your )?(?:full )?refund\b"
    r"|\bqualif(?:y|ies) for a (?:full )?refund\b"
    r"|\b(?:recibirá|puede recibir) un reembolso\b",
    re.I,
)
_REPLACEMENT_PROMISE = re.compile(
    r"\b(?:will|'ll) (?:send|ship|arrange) (?:you |out )?a (?:free )?replacement\b"
    r"|\byou(?:'ll| will) (?:receive|get) a replacement\b",
    re.I,
)
_GUARANTEE = re.compile(r"\bguarante\w+|\bgarantiz\w+", re.I)
_DISCOUNT_OFFER = re.compile(
    r"\b(?:here(?:'s| is)|i(?:'ve| have) (?:added|applied)|i can (?:offer|give)|we can (?:offer|give)"
    r"|i(?:'ll| will) (?:add|apply|give)|as a (?:thank you|goodwill|courtesy))\b"
    r"[^.!?\n]{0,60}\b(discount|credit|coupon|voucher|code|% off)\b",
    re.I,
)


def check_commitments(c: Candidate, ctx: Context) -> CheckResult:
    """The reply must not commit the business to anything code cannot verify."""
    reply = c.reply
    problems = []

    if m := _ALREADY_DONE.search(reply):
        if not negated(reply, m.start()):
            problems.append(
                f"says an action has already happened ({m.group(0)!r}); the system cannot "
                "carry out actions"
            )
    if (m := _REFUND_PROMISE.search(reply)) and not negated(reply, m.start()):
        if c.proposed_action.type != "refund":
            problems.append("promises a refund without a refund in proposed_action")
    if (m := _REPLACEMENT_PROMISE.search(reply)) and not negated(reply, m.start()):
        if c.proposed_action.type != "replacement":
            problems.append("promises a replacement without one in proposed_action")
    for m in _GUARANTEE.finditer(reply):
        if not negated(reply, m.start()):
            problems.append("makes a guarantee that no policy supports")
            break
    for m in _DISCOUNT_OFFER.finditer(reply):
        if negated(reply, m.start()):
            continue
        sentence_codes = extract_facts(reply[m.start() : m.end() + 80]) & {
            f for f in extract_facts(ctx.as_text()) if f.startswith("code ")
        }
        if not sentence_codes:
            problems.append("offers a discount or credit that no policy supports")
            break

    return CheckResult("commitments", not problems, "; ".join(problems))


_RUDE = re.compile(
    r"\bcalm down\b|\bas i (?:already )?said\b|\bobviously\b|\byou should have\b"
    r"|\bper my last\b|\bthere is nothing (?:i|we) can do\b",
    re.I,
)
_UNHAPPY = re.compile(
    r"\b(angry|furious|unacceptable|terrible|awful|worst|disappointed|ridiculous|disgusted"
    r"|damaged|broken|cracked|never arrived|still waiting|fed up|complain\w*"
    r"|enojad\w+|inaceptable|rot[oa]|dañad\w+|decepcionad\w+|harto)\b",
    re.I,
)
_CHEERFUL = re.compile(
    r"\b(great news|good news|awesome|exciting|happy to announce|yay|genial|buenas noticias)\b",
    re.I,
)
_SHOUTING = re.compile(r"!!|\b(?:[A-Z]{3,}\s+){2,}[A-Z]{3,}\b")


def check_tone(c: Candidate, ticket_text: str) -> CheckResult:
    reply = c.reply
    problems = []
    if m := _RUDE.search(reply):
        problems.append(f"dismissive phrase: {m.group(0)!r}")
    if _SHOUTING.search(reply):
        problems.append("shouting (repeated exclamation marks or capitals)")
    if _UNHAPPY.search(ticket_text):
        if "!" in reply:
            problems.append("exclamation mark in a reply to an unhappy customer")
        if m := _CHEERFUL.search(reply):
            problems.append(f"cheerful wording for an unhappy customer: {m.group(0)!r}")
    return CheckResult("tone", not problems, "; ".join(problems))


def check_language(c: Candidate, ticket_text: str) -> CheckResult:
    wanted, got = detect_language(ticket_text), detect_language(c.reply)
    if wanted and got and wanted != got:
        return CheckResult(
            "language", False, f"customer wrote in '{wanted}' but the reply is in '{got}'"
        )
    return CheckResult("language", True)


def check_privacy(c: Candidate, ctx: Context) -> CheckResult:
    """The reply may only contain addresses the customer is meant to see."""
    allowed = {e.lower() for e in _EMAIL.findall(ctx.as_text())}
    leaked = sorted({e.lower() for e in _EMAIL.findall(c.reply)} - allowed)
    detail = (
        f"contains an address that is not in the context: {', '.join(leaked)}" if leaked else ""
    )
    return CheckResult("privacy", not leaked, detail)


def run_checks(
    candidate: Candidate,
    ctx: Context,
    subject: str,
    body: str,
    received_at: datetime,
    settings: Settings | None = None,
) -> list[CheckResult]:
    ticket_text = f"{subject}\n{body}"
    return [
        check_format(candidate),
        check_citations(candidate, ctx),
        check_grounding(candidate, ctx, ticket_text),
        check_action(candidate, ctx, received_at),
        check_commitments(candidate, ctx),
        check_tone(candidate, ticket_text),
        check_language(candidate, ticket_text),
        check_privacy(candidate, ctx),
    ]
