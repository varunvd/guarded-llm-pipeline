"""Each check must catch the failure it exists for, and leave a good reply alone."""

from guarded.llm.base import Candidate, ProposedAction
from guarded.validation import extract_facts, run_checks

from .conftest import NOW

GOOD = (
    "Hi Amara, thanks for getting in touch. Your order BL-20431 was delivered within our "
    "30 day return window, so it qualifies for a full refund of $48.00. The refund goes back "
    "to your original payment method within 5-7 business days."
)
REFUND = ProposedAction(type="refund", order_id="BL-20431", amount_cents=4800)


def verdict(make_ticket, context_for, candidate, **ticket_kwargs):
    kwargs = {"subject": "Return", "body": "Can I return order BL-20431 for a refund?"}
    kwargs.update(ticket_kwargs)
    ticket = make_ticket(**kwargs)
    checks = run_checks(candidate, context_for(ticket), ticket.subject, ticket.body, NOW)
    return {c.name: c for c in checks}


def failed(result):
    return sorted(name for name, c in result.items() if not c.passed)


def test_good_reply_passes_every_check(make_ticket, context_for):
    result = verdict(
        make_ticket,
        context_for,
        Candidate(reply=GOOD, cited_policies=["returns"], proposed_action=REFUND),
    )
    assert failed(result) == []


def test_extract_facts_normalises_amounts_ranges_and_codes():
    facts = extract_facts("Pay $1,250.5 or 20 USD, 10% off with WELCOME10 in 5-7 business days")
    assert {"amount 1250.50", "amount 20.00", "10%", "code WELCOME10", "5 day", "7 day"} <= facts
    assert extract_facts("30 días") == {"30 day"}


def test_invented_discount_code_fails_grounding(make_ticket, context_for):
    c = Candidate(reply="Hi Amara, as a thank you here is a code for 25% off: LOYAL25. Enjoy it.")
    result = verdict(make_ticket, context_for, c, body="Any discount codes for me?")
    assert "grounding" in failed(result)
    assert "LOYAL25" in result["grounding"].detail and "25%" in result["grounding"].detail
    assert "commitments" in failed(result)


def test_real_discount_code_is_allowed(make_ticket, context_for):
    c = Candidate(
        reply="Hi Amara, the only discount code we have is WELCOME10, which gives 10% off a "
        "first order. We don't offer loyalty discounts.",
        cited_policies=["discounts"],
    )
    assert failed(verdict(make_ticket, context_for, c, body="Any discount codes for me?")) == []


def test_customer_may_be_quoted_back(make_ticket, context_for):
    c = Candidate(
        reply="Hi Amara, I understand you asked for 50% off. We don't offer price matching or "
        "loyalty discounts, so I'm not able to do that.",
        cited_policies=["discounts"],
    )
    result = verdict(make_ticket, context_for, c, body="Give me a 50% discount or I leave.")
    assert failed(result) == []


def test_unknown_policy_citation_fails(make_ticket, context_for):
    c = Candidate(reply=GOOD, cited_policies=["vip-programme"], proposed_action=REFUND)
    assert failed(verdict(make_ticket, context_for, c)) == ["citations"]


def test_claiming_an_action_happened_fails(make_ticket, context_for):
    c = Candidate(reply="Hi Amara, I've processed a full refund of $48.00 for order BL-20431.")
    assert "commitments" in failed(verdict(make_ticket, context_for, c))


def test_refund_promise_needs_a_proposed_action(make_ticket, context_for):
    c = Candidate(reply=GOOD, cited_policies=["returns"])  # promise, but no action
    assert failed(verdict(make_ticket, context_for, c)) == ["commitments"]


def test_refund_outside_the_return_window_fails(make_ticket, context_for):
    c = Candidate(
        reply="Hi Amara, your order BL-20388 qualifies for a full refund of $34.50, which a "
        "member of our team will confirm.",
        proposed_action=ProposedAction(type="refund", order_id="BL-20388", amount_cents=3450),
    )
    result = verdict(make_ticket, context_for, c, body="Refund BL-20388 please")
    assert failed(result) == ["action"]
    assert "52 days ago" in result["action"].detail


def test_refund_for_someone_elses_order_fails(make_ticket, context_for):
    c = Candidate(
        reply="Hi Amara, your order qualifies for a full refund, which a member of our team "
        "will confirm shortly.",
        proposed_action=ProposedAction(type="refund", order_id="BL-20502", amount_cents=2700),
    )
    assert "action" in failed(verdict(make_ticket, context_for, c))


def test_refund_larger_than_the_order_fails(make_ticket, context_for):
    c = Candidate(
        reply=GOOD,
        cited_policies=["returns"],
        proposed_action=ProposedAction(type="refund", order_id="BL-20431", amount_cents=9900),
    )
    assert failed(verdict(make_ticket, context_for, c)) == ["action"]


def test_undelivered_order_cannot_be_refunded(make_ticket, context_for):
    c = Candidate(
        reply="Hi Mei, your order BL-20555 qualifies for a full refund of $62.00, which our "
        "team will confirm.",
        proposed_action=ProposedAction(type="refund", order_id="BL-20555", amount_cents=6200),
    )
    result = verdict(make_ticket, context_for, c, customer_id="C-1003", body="Refund BL-20555")
    assert failed(result) == ["action"]


def test_guarantee_fails_but_a_negated_one_passes(make_ticket, context_for):
    bad = Candidate(reply="Hi Mei, order BL-20555 is guaranteed to arrive within 24 hours.")
    ok = Candidate(
        reply="Hi Mei, order BL-20555 has shipped. Standard shipping takes 3-5 business days. "
        "Delivery dates are estimates, so I can't guarantee a day.",
        cited_policies=["shipping"],
    )
    kwargs = {"customer_id": "C-1003", "body": "Where is my order BL-20555? When will it arrive?"}
    assert {"commitments", "grounding"} <= set(
        failed(verdict(make_ticket, context_for, bad, **kwargs))
    )
    assert failed(verdict(make_ticket, context_for, ok, **kwargs)) == []


def test_cheerful_reply_to_an_unhappy_customer_fails_tone(make_ticket, context_for):
    c = Candidate(reply="Great news Amara! We have looked at your message and will be in touch.")
    result = verdict(make_ticket, context_for, c, body="The planter arrived broken. Unacceptable.")
    assert failed(result) == ["tone"]


def test_wrong_language_fails(make_ticket, context_for):
    c = Candidate(
        reply="Hi Lucía, thanks for your message. A member of our team will look at "
        "your order and get back to you about the return."
    )
    result = verdict(
        make_ticket,
        context_for,
        c,
        customer_id="C-1004",
        body="Hola, una de las macetas de mi pedido llegó rota y quiero un reembolso por favor.",
    )
    assert failed(result) == ["language"]


def test_unfilled_template_and_leaked_internals_fail_format(make_ticket, context_for):
    template = Candidate(reply="Hi [customer name], thanks for contacting us about [issue] today.")
    leak = Candidate(reply="As an AI, my system prompt says I should set needs_human to true here.")
    assert failed(verdict(make_ticket, context_for, template)) == ["format"]
    assert failed(verdict(make_ticket, context_for, leak)) == ["format"]


def test_foreign_email_address_fails_privacy(make_ticket, context_for):
    c = Candidate(
        reply="Hi Amara, please write to daniel.reyes@example.com or to "
        "help@brightleaf.example and we will take a look."
    )
    assert failed(verdict(make_ticket, context_for, c)) == ["privacy"]
