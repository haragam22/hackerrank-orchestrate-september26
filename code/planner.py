"""Layer 4 — candidate generation, eligibility, spending-change subsets,
ranking, status mapping. Implements the published rules exactly (no free
parameters) -- ARCHITECTURE.md §4.4, problem_statement.md "Choosing Between
Safe Plans". Generate every candidate, then filter, then rank -- never
reason directly to an answer.
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from io_loader import PaymentOption, Profile, Request
from ledger import Ledger
import recurrence
import safety


@dataclass
class SpendingChange:
    kind: str          # "stop" | "reduce_to"
    event_id: str
    new_amount: float | None = None  # only for reduce_to


@dataclass
class Candidate:
    method: str                              # full_payment | partial_payment | installments | wait
    schedule: list[tuple[dt.date, float]]    # chronological (date, amount)
    spending_changes: list[SpendingChange]
    payment_option_id: str | None = None     # installments only, for the tie-break rule
    total_paid: float = 0.0

    def __post_init__(self):
        self.total_paid = sum(amt for _, amt in self.schedule)


@dataclass
class Plan:
    status: str
    method: str
    schedule: list[tuple[dt.date, float]]
    spending_changes: list[SpendingChange]
    payment_option_id: str | None


def _apply_spending_changes(items: list[recurrence.FlowItem],
                             changes: list[SpendingChange]) -> list[recurrence.FlowItem]:
    if not changes:
        return items
    stop_ids = {c.event_id for c in changes if c.kind == "stop"}
    reduce_map = {c.event_id: c.new_amount for c in changes if c.kind == "reduce_to"}
    out = []
    for item in items:
        if item.source_event_id in stop_ids:
            continue
        if item.source_event_id in reduce_map:
            # signed_amount is negative for a debit; new_amount (minimum_allowed_amount)
            # is a positive floor -- flip sign to match the item's own sign convention.
            sign = -1 if item.signed_amount < 0 else 1
            item = recurrence.FlowItem(item.date, sign * abs(reduce_map[item.source_event_id]),
                                        item.source_event_id, item.is_recurring)
        out.append(item)
    return out


def eligible_spending_change_events(ledger: Ledger, profile: Profile, params: dict | None = None) -> list:
    """Recurring, flexible events whose category the user permits changing.
    Returns [(event, allowed_kinds)] where allowed_kinds is a subset of
    {"stop", "reduce_to"}."""
    p = {**recurrence.DEFAULT_PARAMS, **(params or {})}
    reps = recurrence.recurring_series_representatives(ledger, p["min_occurrences"], p["gap_tolerance"])
    out = []
    for event in reps.values():
        kinds = set()
        if event.flexibility in ("stoppable", "reducible_or_stoppable") and \
                event.category in profile.expense_categories_user_is_willing_to_stop:
            kinds.add("stop")
        if event.flexibility in ("reducible", "reducible_or_stoppable") and \
                event.category in profile.expense_categories_user_is_willing_to_reduce and \
                event.minimum_allowed_amount is not None:
            kinds.add("reduce_to")
        if kinds:
            out.append((event, kinds))
    return out


def _spending_change_subsets(eligible: list, max_size: int = 3):
    """Yield subsets of size 0..max_size. Stop/reduce are mutually exclusive
    on the same event, so each eligible event contributes at most one
    SpendingChange per subset. Size 0 first (tried first per the "fewest
    spending changes" tie-break)."""
    yield []
    options_per_event = []
    for event, kinds in eligible:
        opts = []
        if "stop" in kinds:
            opts.append(SpendingChange("stop", event.event_id))
        if "reduce_to" in kinds:
            opts.append(SpendingChange("reduce_to", event.event_id, event.minimum_allowed_amount))
        if opts:
            options_per_event.append(opts)

    for size in range(1, max_size + 1):
        for combo_events in itertools.combinations(range(len(options_per_event)), size):
            choices = [options_per_event[i] for i in combo_events]
            for combo in itertools.product(*choices):
                yield list(combo)


def _safe_amount_and_earliest(profile: Profile, items: list[recurrence.FlowItem],
                               request_date: dt.date, requested_amount: float,
                               horizon_days: int = 90, inclusive: bool = True):
    flows = recurrence.items_to_dict(items)
    safe = safety.amount_safe_to_pay(profile.current_available_balance, profile.minimum_balance_to_keep,
                                      flows, request_date, requested_amount, horizon_days, inclusive)
    earliest = safety.earliest_date_for_full_payment(profile.current_available_balance,
                                                       profile.minimum_balance_to_keep,
                                                       flows, request_date, requested_amount,
                                                       horizon_days, inclusive)
    return safe, earliest


def _schedule_is_safe(profile: Profile, items: list[recurrence.FlowItem],
                       request_date: dt.date, schedule: list[tuple[dt.date, float]],
                       horizon_days: int = 90, inclusive: bool = True) -> bool:
    flows = recurrence.items_to_dict(items)
    return safety.is_safe(profile.current_available_balance, profile.minimum_balance_to_keep,
                           flows, schedule, request_date, horizon_days, inclusive)


def generate_candidates(req: Request, profile: Profile, options: list[PaymentOption],
                         amount_safe_to_pay: float, earliest_date: dt.date | None) -> list[Candidate]:
    """Every candidate shape, unfiltered. Eligibility is applied separately
    in build_plan() so callers can see the full universe if needed."""
    candidates: list[Candidate] = []

    candidates.append(Candidate("full_payment", [(req.request_date, req.requested_amount)], []))

    for opt in options:
        if opt.payment_method != "installments" or opt.first_payment_date is None:
            continue
        freq = opt.payment_frequency_days or 0
        schedule = [
            (opt.first_payment_date + dt.timedelta(days=freq * k), opt.payment_amount)
            for k in range(opt.number_of_payments)
        ]
        candidates.append(Candidate("installments", schedule, [], payment_option_id=opt.payment_option_id))

    if 0 < amount_safe_to_pay < req.requested_amount and earliest_date is not None:
        candidates.append(Candidate(
            "partial_payment",
            [(req.request_date, amount_safe_to_pay), (earliest_date, req.requested_amount - amount_safe_to_pay)],
            [],
        ))

    if earliest_date is not None:
        candidates.append(Candidate("wait", [(earliest_date, req.requested_amount)], []))

    return candidates


def _eligible_immediate(candidate: Candidate, req: Request, profile: Profile) -> bool:
    if candidate.method not in profile.payment_methods_user_will_consider:
        return False
    if candidate.method == "installments":
        n_payments = len(candidate.schedule)
        if profile.max_installment_months is None:
            return False
        if n_payments > profile.max_installment_months:
            return False
    if candidate.method == "partial_payment":
        if not req.allows_partial_payment:
            return False
    completion_date = candidate.schedule[-1][0] if candidate.schedule else None
    if completion_date is None or completion_date > req.desired_completion_date:
        return False
    return True


def _eligible_wait(candidate: Candidate, profile: Profile) -> bool:
    return "full_payment" in profile.payment_methods_user_will_consider


RANK_STATUS_FOR_METHOD = {
    "full_payment": "affordable_now",
    "installments": "affordable_with_plan",
    "partial_payment": "affordable_with_plan",
    "wait": "affordable_later",
}


def _rank_key(candidate: Candidate, req: Request):
    completes_by_deadline = candidate.schedule[-1][0] <= req.desired_completion_date
    return (
        0 if completes_by_deadline else 1,
        len(candidate.spending_changes),
        candidate.total_paid,
        candidate.schedule[0][0],
        len(candidate.schedule),
        candidate.payment_option_id or "",
    )


def build_plan(req: Request, profile: Profile, ledger: Ledger, options: list[PaymentOption],
               params: dict | None = None, horizon_days: int = 90, inclusive: bool = True) -> Plan:
    base_items = recurrence.project_flow_items(ledger, req.request_date, horizon_days, params)
    amount_safe, earliest = _safe_amount_and_earliest(profile, base_items, req.request_date, req.requested_amount,
                                                        horizon_days, inclusive)

    eligible_events = eligible_spending_change_events(ledger, profile, params)
    safe_candidates: list[Candidate] = []

    # full_payment / installments / wait: no spending changes needed if
    # already safe on the raw forecast (they don't touch amount_safe_to_pay
    # or earliest_date, which are always computed on base_items).
    base_candidates = generate_candidates(req, profile, options, amount_safe, earliest)
    for cand in base_candidates:
        if cand.method == "wait":
            if not _eligible_wait(cand, profile):
                continue
        else:
            if not _eligible_immediate(cand, req, profile):
                continue
        if _schedule_is_safe(profile, base_items, req.request_date, cand.schedule, horizon_days, inclusive):
            safe_candidates.append(cand)

    # If nothing immediate is safe without changes, escalate through
    # spending-change subsets for full_payment and installments (partial's
    # amount_safe_to_pay is defined without changes, so it doesn't get a
    # spending-change variant -- ARCHITECTURE §4.3/§4.4).
    immediate_methods_present = {c.method for c in safe_candidates if c.method in ("full_payment", "installments")}
    need_escalation = "full_payment" not in immediate_methods_present
    if need_escalation and eligible_events:
        for changes in _spending_change_subsets(eligible_events):
            if not changes:
                continue
            mutated_items = _apply_spending_changes(base_items, changes)
            full = Candidate("full_payment", [(req.request_date, req.requested_amount)], changes)
            if _eligible_immediate(full, req, profile) and \
                    _schedule_is_safe(profile, mutated_items, req.request_date, full.schedule, horizon_days, inclusive):
                safe_candidates.append(full)

            for opt in options:
                if opt.payment_method != "installments" or opt.first_payment_date is None:
                    continue
                freq = opt.payment_frequency_days or 0
                schedule = [
                    (opt.first_payment_date + dt.timedelta(days=freq * k), opt.payment_amount)
                    for k in range(opt.number_of_payments)
                ]
                inst = Candidate("installments", schedule, changes, payment_option_id=opt.payment_option_id)
                if _eligible_immediate(inst, req, profile) and \
                        _schedule_is_safe(profile, mutated_items, req.request_date, inst.schedule,
                                          horizon_days, inclusive):
                    safe_candidates.append(inst)

    if not safe_candidates:
        return Plan("not_affordable", "not_recommended", [], [], None)

    safe_candidates.sort(key=lambda c: _rank_key(c, req))
    best = safe_candidates[0]
    status = RANK_STATUS_FOR_METHOD[best.method]
    if best.spending_changes:
        status = "affordable_with_plan"
    return Plan(status, best.method, best.schedule, best.spending_changes, best.payment_option_id)
