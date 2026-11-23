"""Evaluation: how many unacceptable replies reach a customer?

Two arms are run over the same cases with the same provider:

    raw      one model call. The reply is sent unless the model itself asks
             for a person. This is what a prototype usually does.
    guarded  the full pipeline: validation, retries with feedback, risk rules,
             fallback and review routing, with automatic sending switched on.

A reply that is held for a person is never counted as unacceptable, because
nothing was sent. Holding too much has its own cost, so the report also shows
how many tickets each arm held that did not need a person.

Usage:
    uv run python -m evals.run                      # offline, simulated model
    uv run python -m evals.run --provider anthropic --yes
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from guarded.config import Settings
from guarded.context import build_context
from guarded.db import Ticket, make_session_factory
from guarded.llm import make_provider
from guarded.llm.base import GenerationRequest, LLMProvider
from guarded.pipeline import process_ticket
from guarded.prompts import SYSTEM, render_user
from guarded.seed import seed_reference_data
from guarded.textutil import detect_language

from .cases import CASES, Case

EVAL_NOW = datetime(2026, 6, 15, 12, 0, 0)
RESULTS = Path(__file__).parent / "results"


@dataclass
class ArmResult:
    sent: bool
    reply: str | None
    violations: list[str]
    attempts: int = 1
    cost_usd: float = 0.0
    used_fallback: bool = False
    no_reply: bool = False


def judge(case: Case, reply: str) -> list[str]:
    """Why sending `reply` for this case would be wrong. Empty means acceptable."""
    violations = []
    if case.must_route_human:
        violations.append("sent automatically, but this ticket needed a person")
    for pattern in case.forbidden:
        if m := re.search(pattern, reply, re.I if pattern.islower() else 0):
            violations.append(f"contains {m.group(0)!r}")
    if case.language and detect_language(reply) not in (case.language, None):
        violations.append(f"not written in '{case.language}'")
    return violations


def run_raw(session, ticket: Ticket, case: Case, provider: LLMProvider, settings) -> ArmResult:
    ctx = build_context(session, ticket)
    request = GenerationRequest(
        subject=ticket.subject,
        body=ticket.body,
        context=ctx,
        system=SYSTEM,
        user=render_user(ticket.subject, ticket.body, ctx, ticket.created_at),
        attempt=1,
    )
    result = provider.generate(request)
    cost = settings.cost_usd(result.input_tokens, result.output_tokens)
    if result.candidate is None:
        return ArmResult(False, None, [], cost_usd=cost, no_reply=True)
    if result.candidate.needs_human:
        return ArmResult(False, result.candidate.reply, [], cost_usd=cost)
    reply = result.candidate.reply
    return ArmResult(True, reply, judge(case, reply), cost_usd=cost)


def run_guarded(session, ticket: Ticket, case: Case, provider: LLMProvider, settings) -> ArmResult:
    outcome = process_ticket(session, ticket.id, provider, settings)
    session.refresh(ticket)
    cost = sum(a.cost_usd for a in ticket.attempts)
    if outcome.status != "sent":
        return ArmResult(
            False, ticket.draft_reply, [], outcome.attempts, cost, ticket.used_fallback
        )
    return ArmResult(
        True, ticket.final_reply, judge(case, ticket.final_reply), outcome.attempts, cost
    )


def evaluate(provider: LLMProvider, settings: Settings, cases: list[Case]) -> dict:
    rows = []
    for arm, runner in (("raw", run_raw), ("guarded", run_guarded)):
        # A fresh database per arm, so the arms cannot affect each other.
        sessions = make_session_factory("sqlite://")
        with sessions() as session:
            seed_reference_data(session, now=EVAL_NOW)
            for case in cases:
                ticket = Ticket(
                    customer_id=case.customer_id,
                    subject=case.subject,
                    body=case.body,
                    created_at=EVAL_NOW,
                )
                session.add(ticket)
                session.commit()
                r = runner(session, ticket, case, provider, settings)
                rows.append(
                    {
                        "arm": arm,
                        "case": case.id,
                        "trap": case.trap,
                        "must_route_human": case.must_route_human,
                        **r.__dict__,
                    }
                )
    return {"rows": rows, "summary": summarise(rows, cases)}


def summarise(rows: list[dict], cases: list[Case]) -> dict:
    out = {}
    for arm in ("raw", "guarded"):
        mine = [r for r in rows if r["arm"] == arm]
        bad = [r for r in mine if r["violations"]]
        held = [r for r in mine if not r["sent"] and not r["no_reply"]]
        by_trap: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in mine:
            by_trap[r["trap"]][1] += 1
            by_trap[r["trap"]][0] += bool(r["violations"])
        out[arm] = {
            "cases": len(mine),
            "sent": sum(r["sent"] for r in mine),
            "held_for_person": len(held),
            "held_unnecessarily": sum(not r["must_route_human"] for r in held),
            "no_reply": sum(r["no_reply"] for r in mine),
            "unacceptable_sent": len(bad),
            "unacceptable_share": round(len(bad) / len(mine), 3) if mine else 0.0,
            "model_calls": sum(r["attempts"] for r in mine),
            "fallbacks": sum(r["used_fallback"] for r in mine),
            "cost_usd": round(sum(r["cost_usd"] for r in mine), 4),
            "by_trap": {k: {"unacceptable": v[0], "cases": v[1]} for k, v in by_trap.items()},
            "violations": dict(Counter(v for r in bad for v in r["violations"]).most_common()),
        }
    return out


def to_markdown(result: dict, provider_name: str, model: str) -> str:
    s = result["summary"]
    raw, g = s["raw"], s["guarded"]
    lines = [
        "# Evaluation report",
        "",
        f"- Provider: `{provider_name}`, model: `{model}`",
        f"- Cases: {raw['cases']}",
        f"- Run at: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    if provider_name == "simulated":
        lines += [
            "> **These numbers come from the offline simulated model.** It makes scripted",
            "> mistakes on a fixed share of tickets, so the figures show how the pipeline",
            "> behaves, not how any real model performs. Run with `--provider anthropic`",
            "> for real figures.",
            "",
        ]
    lines += [
        "| | Raw model | Guarded pipeline |",
        "|---|---:|---:|",
        f"| Replies sent automatically | {raw['sent']} | {g['sent']} |",
        f"| **Unacceptable replies sent** | **{raw['unacceptable_sent']}** | **{g['unacceptable_sent']}** |",
        f"| Share of all cases | {raw['unacceptable_share']:.1%} | {g['unacceptable_share']:.1%} |",
        f"| Held for a person | {raw['held_for_person']} | {g['held_for_person']} |",
        f"| Held without needing to be | {raw['held_unnecessarily']} | {g['held_unnecessarily']} |",
        f"| No usable reply from the model | {raw['no_reply']} | {g['no_reply']} |",
        f"| Model calls | {raw['model_calls']} | {g['model_calls']} |",
        f"| Fell back to the safe template | n/a | {g['fallbacks']} |",
        f"| Cost (USD) | {raw['cost_usd']:.4f} | {g['cost_usd']:.4f} |",
        "",
        "## Unacceptable replies by kind of ticket",
        "",
        "| Kind of ticket | Cases | Raw model | Guarded pipeline |",
        "|---|---:|---:|---:|",
    ]
    for trap, v in raw["by_trap"].items():
        lines.append(
            f"| {trap} | {v['cases']} | {v['unacceptable']} | "
            f"{g['by_trap'][trap]['unacceptable']} |"
        )
    for arm, title in (("raw", "raw model"), ("guarded", "guarded pipeline")):
        bad = [r for r in result["rows"] if r["arm"] == arm and r["violations"]]
        lines += ["", f"## Unacceptable replies sent by the {title}", ""]
        if not bad:
            lines.append("None.")
        for r in bad:
            lines += [f"- **{r['case']}**: {'; '.join(r['violations'])}", f"  > {r['reply']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--provider", default="simulated", choices=["simulated", "anthropic"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--yes", action="store_true", help="confirm spending on a real provider")
    args = parser.parse_args(argv)

    overrides = {"llm_provider": args.provider, "auto_send": True}
    if args.model:
        overrides["llm_model"] = args.model
    settings = Settings(**overrides)
    cases = CASES[: args.limit] if args.limit else CASES

    if args.provider != "simulated" and not args.yes:
        calls = len(cases) * (1 + settings.max_attempts)
        print(
            f"This will make up to {calls} calls to {settings.llm_model} and cost real money.\n"
            "Re-run with --yes to go ahead.",
            file=sys.stderr,
        )
        return 2

    provider = make_provider(settings)
    model = getattr(provider, "model", settings.llm_model)
    result = evaluate(provider, settings, cases)

    RESULTS.mkdir(exist_ok=True)
    stem = f"{args.provider}"
    report = to_markdown(result, args.provider, model)
    (RESULTS / f"{stem}.md").write_text(report)
    (RESULTS / f"{stem}.json").write_text(json.dumps(result, indent=2, default=str))
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
