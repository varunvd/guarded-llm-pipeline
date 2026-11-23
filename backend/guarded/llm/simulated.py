"""An offline stand-in for a model, so the project runs with no API key.

This is not a language model. It writes replies from templates using the same
context a real model would see, and on a configurable share of tickets it makes
the kinds of mistake real models make: inventing a discount code, promising a
refund the policy does not allow, saying an action already happened, answering
in the wrong language, or obeying an instruction hidden in the ticket.

It is deterministic (the same ticket always gets the same behaviour), which
makes the demo and the tests repeatable. Numbers produced with it show how the
pipeline behaves; they say nothing about any real model.
"""

from __future__ import annotations

import hashlib
import re

from ..context import Context, OrderView, format_money
from ..risk import classify_risk
from ..textutil import detect_language, first_name
from .base import Candidate, GenerationRequest, GenerationResult, ProposedAction

_ORDER_ID = re.compile(r"\bBL-\d{5}\b")


def _intent(text: str) -> str:
    t = text.lower()
    if re.search(r"damaged|broken|cracked|in pieces|rota|roto|dañad", t):
        return "damaged"
    if re.search(r"\b(refund|return|send back|money back)\b|reembolso|devol", t) and (
        "leaf club" not in t
    ):
        return "refund"
    if re.search(r"discount|coupon|promo|price match|cheaper|descuento", t):
        return "discount"
    if re.search(r"cancel", t) and re.search(r"leaf club|subscription|membership", t):
        return "subscription"
    if re.search(r"warranty|defect|stopped working", t):
        return "warranty"
    if re.search(r"where is|tracking|shipping|deliver|arrive|still waiting|envío|llega", t):
        return "shipping"
    return "other"


class SimulatedProvider:
    name = "simulated"
    model = "simulated-v1"

    def __init__(self, flaw_rate: float = 0.45, stubborn_rate: float = 0.15) -> None:
        self.flaw_rate = flaw_rate  # share of tickets whose first draft is flawed
        self.stubborn_rate = stubborn_rate  # share of those that stay flawed on every retry

    # -- behaviour selection ------------------------------------------------

    def _roll(self, body: str, salt: str) -> float:
        digest = hashlib.sha256(f"{salt}:{body}".encode()).digest()
        return int.from_bytes(digest[:4], "big") / 2**32

    def _is_flawed(self, request: GenerationRequest) -> bool:
        if self._roll(request.body, "flaw") >= self.flaw_rate:
            return False
        if request.attempt == 1:
            return True
        return self._roll(request.body, "stubborn") < self.stubborn_rate

    # -- public API ---------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationResult:
        text = f"{request.subject}\n{request.body}"
        ctx = request.context
        order = self._pick_order(text, ctx)
        builder = self._flawed if self._is_flawed(request) else self._clean
        candidate = builder(text, ctx, order, request)
        return GenerationResult(
            candidate=candidate,
            model=self.model,
            input_tokens=(len(request.system) + len(request.user)) // 4,
            output_tokens=len(candidate.model_dump_json()) // 4,
            latency_ms=0,
        )

    @staticmethod
    def _pick_order(text: str, ctx: Context) -> OrderView | None:
        if m := _ORDER_ID.search(text):
            return ctx.order(m.group(0))
        return ctx.orders[0] if ctx.orders else None

    # -- a careful draft ----------------------------------------------------

    def _clean(
        self, text: str, ctx: Context, order: OrderView | None, request: GenerationRequest
    ) -> Candidate:
        name = first_name(ctx.customer_name)
        spanish = detect_language(text) == "es"
        have = ctx.policy_ids()

        def handoff(reason: str) -> Candidate:
            reply = (
                f"Hola {name}, gracias por escribirnos. He pasado su mensaje a una persona de "
                "nuestro equipo, que le responderá en breve."
                if spanish
                else f"Hi {name}, thank you for your message. I've passed it to a member of our "
                "team, who will reply to you directly."
            )
            return Candidate(reply=reply, needs_human=True, needs_human_reason=reason)

        risks = classify_risk(request.subject, request.body)
        if risks:
            return handoff(", ".join(risks))

        intent = _intent(text)
        received = _received_at(request)

        if intent in ("refund", "damaged") and order and order.delivered_at:
            days = (received - order.delivered_at).days
            window, policy = (14, "damaged-items") if intent == "damaged" else (30, "returns")
            if policy not in have:
                return handoff("relevant policy not in context")
            if days <= window and intent == "refund":
                amount = format_money(order.amount_cents)
                reply = (
                    f"Hola {name}, gracias por escribirnos. Su pedido {order.id} fue entregado "
                    f"dentro de nuestro plazo de devolución de 30 días, por lo que puede recibir "
                    f"un reembolso completo de {amount}. Una persona de nuestro equipo confirmará "
                    "la devolución y el reembolso llegará a su método de pago original en un "
                    "plazo de 5-7 días hábiles."
                    if spanish
                    else f"Hi {name}, thanks for getting in touch. Your order {order.id} "
                    f"({order.item}) was delivered within our 30 day return window, so it "
                    f"qualifies for a full refund of {amount}. A member of our team will confirm "
                    "the return, and the refund then goes back to your original payment method "
                    "within 5-7 business days."
                )
                return Candidate(
                    reply=reply,
                    cited_policies=["returns"],
                    proposed_action=ProposedAction(
                        type="refund", order_id=order.id, amount_cents=order.amount_cents
                    ),
                )
            if days <= window:  # damaged, in time
                if spanish:
                    return handoff("damaged item, reply needed in Spanish")
                return Candidate(
                    reply=f"Hi {name}, I'm sorry that your {order.item.lower()} arrived damaged. "
                    "Because you reported it within 14 days of delivery, we can offer either a "
                    "replacement or a full refund. Please reply with a photo of the damage and "
                    "tell us which you would prefer, and a member of our team will arrange it.",
                    cited_policies=["damaged-items"],
                )
            if spanish:
                return handoff("outside policy window, reply needed in Spanish")
            if intent == "refund":
                return Candidate(
                    reply=f"Hi {name}, thanks for getting in touch. Order {order.id} "
                    f"({order.item}) was delivered more than 30 days ago, which is outside our "
                    "30 day return window, so I'm not able to offer a refund for it. If there is "
                    "a fault with the item itself, reply with the details and a member of our "
                    "team will take a look.",
                    cited_policies=["returns"],
                )
            return Candidate(
                reply=f"Hi {name}, I'm sorry to hear about the damage. Damage needs to be "
                "reported within 14 days of delivery, and this order was delivered earlier than "
                "that, so I've passed your message to a member of our team to review.",
                cited_policies=["damaged-items"],
                needs_human=True,
                needs_human_reason="damage reported outside the 14 day window",
            )

        if intent in ("refund", "damaged") and order and "returns" in have and not spanish:
            return Candidate(
                reply=f"Hi {name}, thanks for getting in touch. Order {order.id} has not been "
                "delivered yet, so it can't be returned at this stage. It can be cancelled "
                "instead. Reply to confirm and a member of our team will arrange that.",
                cited_policies=["returns"],
            )

        if intent == "discount" and "discounts" in have and not spanish:
            return Candidate(
                reply=f"Hi {name}, thanks for asking. The only discount code we have at the "
                "moment is WELCOME10, which gives 10% off a first order. We don't offer price "
                "matching or loyalty discounts, so I'm not able to add anything to your order.",
                cited_policies=["discounts"],
            )

        if intent == "subscription" and "leaf-club" in have and not spanish:
            return Candidate(
                reply=f"Hi {name}, you can cancel Leaf Club at any time. It stays active until "
                "the end of the month you have paid for, and partial months are not refunded. "
                "Reply to confirm and a member of our team will cancel it for you.",
                cited_policies=["leaf-club"],
            )

        if intent == "warranty" and "warranty" in have and not spanish:
            return Candidate(
                reply=f"Hi {name}, thanks for letting us know. Tools and planters are covered by "
                "a 1 year warranty against manufacturing defects; live plants are not covered. "
                "Please reply with a photo and a short description of the problem, and a member "
                "of our team will review it.",
                cited_policies=["warranty"],
            )

        if intent == "shipping" and order and "shipping" in have and not spanish:
            if order.status == "shipped":
                body = (
                    f"I'm sorry for the wait. Order {order.id} has shipped. Standard shipping "
                    "takes 3-5 business days. Delivery dates are estimates, so I can't promise "
                    "a day, but if it has not arrived after that, reply here and we will look "
                    "into it."
                )
            elif order.status == "processing":
                body = (
                    f"Order {order.id} is being prepared and has not shipped yet. Once it "
                    "ships, standard shipping takes 3-5 business days."
                )
            else:
                return handoff("customer reports a delivered order as missing")
            return Candidate(reply=f"Hi {name}, {body}", cited_policies=["shipping"])

        return handoff("question is not covered by the policies in context")

    # -- a careless draft ---------------------------------------------------

    def _flawed(
        self, text: str, ctx: Context, order: OrderView | None, request: GenerationRequest
    ) -> Candidate:
        name = first_name(ctx.customer_name)
        intent = _intent(text)
        amount = format_money(order.amount_cents) if order else "$25.00"
        oid = order.id if order else "your order"

        if "possible prompt injection" in classify_risk(request.subject, request.body):
            return Candidate(  # obeys the instruction hidden in the ticket
                reply=f"Sure {name}. As requested, here is a code for a 100% refund on any "
                "order: FREE100. Enter it at checkout.",
            )
        if classify_risk(request.subject, request.body):
            return Candidate(  # answers a ticket that needed a person, and gives advice
                reply=f"Hi {name}, sorry about that. You are legally entitled to compensation "
                "here, so there is no need to involve your bank or anyone else. We will sort "
                "it out for you.",
            )
        if detect_language(text) == "es":
            return Candidate(  # wrong language
                reply=f"Hi {name}, thanks for getting in touch. A member of our team will look "
                "at your order and get back to you about the return.",
                cited_policies=["returns"] if "returns" in ctx.policy_ids() else [],
            )
        if intent == "refund":
            return Candidate(  # claims the refund already happened
                reply=f"Great news {name}! I've processed a full refund of {amount} for order "
                f"{oid}. You should see it in your account within 5-7 business days.",
                cited_policies=["returns"],
            )
        if intent == "damaged":
            return Candidate(  # cheerful, and promises a replacement with nothing to back it
                reply=f"Great news {name}!!! We will send you a free replacement right away, "
                "and as a goodwill gesture here is a code for 20% off your next order: SORRY20.",
            )
        if intent == "discount":
            return Candidate(  # invents a code
                reply=f"Hi {name}, thanks for being a loyal customer. As a thank you, here is a "
                "code for 25% off your next order: LOYAL25.",
                cited_policies=["loyalty-programme"],
            )
        if intent == "subscription":
            return Candidate(  # claims a cancellation and a refund the policy forbids
                reply=f"Hi {name}, I've cancelled your Leaf Club subscription and you will "
                "receive a refund of $12.00 for this month.",
                cited_policies=["leaf-club"],
            )
        if intent == "shipping":
            return Candidate(  # guarantees a date
                reply=f"Hi {name}, order {oid} is on its way and is guaranteed to arrive "
                "within 24 hours.",
                cited_policies=["shipping"],
            )
        if intent == "warranty":
            # A false statement with no number, code or promise in it. No rule in
            # the validation layer can catch this one, on purpose: it marks the
            # limit of deterministic checks.
            return Candidate(
                reply=f"Hi {name}, thanks for letting us know. Everything we sell, including "
                "live plants, is covered by our warranty for as long as you own it, so this "
                "will be sorted out for you.",
                cited_policies=["warranty"] if "warranty" in ctx.policy_ids() else [],
            )
        return Candidate(  # template left unfilled
            reply="Hi [customer name], thanks for contacting us about [issue]. We will get "
            "back to you soon.",
        )


def _received_at(request: GenerationRequest):
    """Recover the ticket date from the prompt, as a real model would read it."""
    from datetime import datetime

    m = re.search(r"Ticket received on (\d{4}-\d{2}-\d{2})", request.user)
    return datetime.fromisoformat(m.group(1)) if m else datetime.now()
