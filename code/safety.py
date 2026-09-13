"""Layer 3 — safety predicate. This is the whole engine (ARCHITECTURE.md §4.3).

Both scored quantities that don't come from Layer 4's ranking derive from
is_safe(): amount_safe_to_pay (closed form, asserted against the predicate)
and earliest_date_for_full_payment (linear scan). No free parameters beyond
the two named Phase-3 conventions (PENDING_DEBITS, HORIZON), which live in
ledger.py/recurrence.py, not here.
"""
from __future__ import annotations

import datetime as dt

EPS = 1e-6
HORIZON_DAYS = 90


def _offsets(horizon_days: int, inclusive: bool):
    return range(0, horizon_days + 1) if inclusive else range(0, horizon_days)


def is_safe(current_balance: float, minimum_balance_to_keep: float,
            flows_by_date: dict[dt.date, float], schedule: list[tuple[dt.date, float]],
            request_date: dt.date, horizon_days: int = HORIZON_DAYS, inclusive: bool = True) -> bool:
    """flows_by_date: date -> net signed daily flow (credits +, debits -),
    already reflecting any spending changes the caller applied. schedule:
    [(date, amount)] of payments under test, amount is a positive debit.
    The forecast window is always the fixed [request_date, request_date+horizon]
    range, regardless of which dates appear in schedule. `inclusive` is the
    Phase-3 HORIZON convention (day 90 counted or not) -- see ARCHITECTURE.md §5.2."""
    payments_by_date: dict[dt.date, float] = {}
    for d, amt in schedule:
        payments_by_date[d] = payments_by_date.get(d, 0.0) + amt

    bal = current_balance
    for offset in _offsets(horizon_days, inclusive):
        day = request_date + dt.timedelta(days=offset)
        bal += flows_by_date.get(day, 0.0)
        bal -= payments_by_date.get(day, 0.0)
        if bal < minimum_balance_to_keep - EPS:
            return False
    return True


def amount_safe_to_pay(current_balance: float, minimum_balance_to_keep: float,
                        flows_by_date: dict[dt.date, float], request_date: dt.date,
                        requested_amount: float, horizon_days: int = HORIZON_DAYS,
                        inclusive: bool = True) -> float:
    """Largest X in [0, requested_amount] such that paying X on request_date
    (a single lump payment) keeps every day in the horizon safe. A single
    payment on day 0 is a parallel downward shift of the balance for every
    day from request_date onward, so this reduces to the running minimum
    balance over the horizon with no payment applied, clamped to the
    requested range. See ARCHITECTURE.md §0 (rejected general-purpose
    version) and §4.3 (why this restricted form is exact)."""
    running = current_balance
    min_bal = running
    for offset in _offsets(horizon_days, inclusive):
        day = request_date + dt.timedelta(days=offset)
        running += flows_by_date.get(day, 0.0)
        min_bal = min(min_bal, running)
    headroom = min_bal - minimum_balance_to_keep
    return max(0.0, min(requested_amount, headroom))


def earliest_date_for_full_payment(current_balance: float, minimum_balance_to_keep: float,
                                    flows_by_date: dict[dt.date, float], request_date: dt.date,
                                    requested_amount: float, horizon_days: int = HORIZON_DAYS,
                                    inclusive: bool = True) -> dt.date | None:
    """First date d in [request_date, request_date+horizon] such that paying
    requested_amount in full on d is safe over the fixed horizon window.
    Computed with NO spending changes — always. O(horizon^2), trivial at
    horizon=90."""
    for offset in _offsets(horizon_days, inclusive):
        d = request_date + dt.timedelta(days=offset)
        if is_safe(current_balance, minimum_balance_to_keep, flows_by_date,
                   [(d, requested_amount)], request_date, horizon_days, inclusive):
            return d
    return None
