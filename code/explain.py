"""Layer 5 — explanation formatter. Pure string formatting, no model.
Templates and conventions verified against every decision_explanation in
dataset/sample_requests.csv (ARCHITECTURE.md §4.5). See the module-level
NOT_AFFORDABLE_RATIO_THRESHOLD comment for how the template-5-vs-6 open
item (ARCHITECTURE §4.5) was resolved.
"""
from __future__ import annotations

import datetime as dt

from io_loader import Event, Profile, Request
from planner import Plan, SpendingChange

# --- template-5-vs-6 discriminator (ARCHITECTURE.md §4.5 open item) ---
# Checked amount_safe_to_pay / requested_amount for all 7 not_affordable
# samples: template 6 ("Although X is available today...") fires only at
# request_14 (ratio 0.110) and request_24 (ratio 0.122); template 5 ("None
# of the available options...") covers the other five, whose ratios are all
# <= 0.048. The two clusters don't overlap, so any cutoff in (0.048, 0.110)
# reproduces all 7 samples exactly -- 0.08 is the midpoint.
NOT_AFFORDABLE_RATIO_THRESHOLD = 0.08


def _money(currency: str, amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"{currency} {int(round(amount)):,}"
    return f"{currency} {amount:,.2f}"


def _date(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _describe_change(change: SpendingChange, events_by_id: dict[str, Event], currency: str) -> str:
    event = events_by_id.get(change.event_id)
    desc = (event.description if event else change.event_id).lower()
    if change.kind == "stop":
        return f"Stop the {desc}"
    return f"Reduce the {desc} to {_money(currency, change.new_amount)}"


def _join_changes(phrases: list[str]) -> str:
    if len(phrases) == 1:
        return phrases[0]
    out = phrases[0]
    for p in phrases[1:]:
        out += " and " + p[0].lower() + p[1:]
    return out


def generate_explanation(req: Request, profile: Profile, plan: Plan, amount_safe_to_pay: float,
                          events_by_id: dict[str, Event]) -> str:
    currency = profile.home_currency
    min_balance = profile.minimum_balance_to_keep

    if plan.status == "not_affordable":
        ratio = (amount_safe_to_pay / req.requested_amount) if req.requested_amount else 0.0
        if ratio > NOT_AFFORDABLE_RATIO_THRESHOLD:
            return (f"Do not proceed with the {_money(currency, req.requested_amount)} request. "
                    f"Although {_money(currency, amount_safe_to_pay)} is available today, the full amount "
                    f"cannot be completed safely within 90 days.")
        return (f"Do not make this payment by {_date(req.desired_completion_date)}. "
                f"None of the available options keeps the {_money(currency, min_balance)} minimum protected.")

    if plan.method == "partial_payment":
        (_, first_amt), (second_date, second_amt) = plan.schedule
        return (f"Pay {_money(currency, first_amt)} today and the remaining {_money(currency, second_amt)} "
                f"on {_date(second_date)}. This completes the full request and keeps the "
                f"{_money(currency, min_balance)} minimum protected.")

    changes_text = None
    if plan.spending_changes:
        ordered = sorted(plan.spending_changes, key=lambda c: 0 if c.kind == "stop" else 1)
        phrases = [_describe_change(c, events_by_id, currency) for c in ordered]
        changes_text = _join_changes(phrases)

    if plan.method == "installments":
        n = len(plan.schedule)
        amt = plan.schedule[0][1]
        start = plan.schedule[0][0]
        if changes_text:
            # method is a multi-payment schedule -- must not use the
            # single-payment "pay X today" phrasing (see log.txt: request_150
            # caught this producing a misleading explanation for an
            # installments+spending-change plan).
            return (f"{changes_text}, then use {n} installments of {_money(currency, amt)}, "
                    f"starting {_date(start)}. This leaves at least {_money(currency, min_balance)} available.")
        return (f"Use {n} installments of {_money(currency, amt)}, starting {_date(start)}. "
                f"This leaves at least {_money(currency, min_balance)} available.")

    if plan.method == "full_payment":
        amt = plan.schedule[0][1]
        if changes_text:
            return (f"{changes_text}, then pay {_money(currency, amt)} today. "
                    f"This leaves at least {_money(currency, min_balance)} available.")
        return (f"Pay {_money(currency, amt)} today. This leaves at least {_money(currency, min_balance)} "
                f"available over the next 90 days.")

    if plan.method == "wait":
        d, amt = plan.schedule[0]
        return (f"Pay {_money(currency, amt)} in full on {_date(d)}. Paying earlier would take the "
                f"balance below the {_money(currency, min_balance)} minimum.")

    return "No recommendation available."
