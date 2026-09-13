"""Layer 2 — recurrence model (calibrated in Phase 3).

Phase 1 baseline: IRREGULAR_MODE = none — only recurring_monthly series
project forward; irregular/one-off series contribute nothing beyond
history. Real known_future rows (already scheduled/pending in the data)
always count regardless of series classification. See ARCHITECTURE.md
§4.2 / §5.2 for the full parameter grid Phase 3 sweeps.
"""
from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from io_loader import Event
from ledger import Ledger

# Provisional defaults; Phase 3's calibrate.py sweeps these into params.json.
# Kept as module-level defaults for backward compatibility with earlier
# phases' callers; every function also accepts an explicit `params` dict
# (see DEFAULT_PARAMS) so calibrate.py can sweep without mutating globals.
MIN_OCCURRENCES = 3
GAP_TOLERANCE = 3
HORIZON_DAYS = 90
IRREGULAR_MODE = "none"      # none | median_gap_repeat | per_category_monthly_frequency | flat_daily_burn
AMOUNT_ESTIMATOR = "last"    # last | mean | median | trimmed_mean
# Minimum occurrences a series needs to be ELIGIBLE for irregular-mode
# projection at all (separate from MIN_OCCURRENCES above, which is the
# recurring_monthly *classification* threshold). 2 matches what the code
# already did implicitly before this parameter existed (_project_irregular's
# old unconditional `len(events) < 2` guard) -- so 2, not 1, is the value
# that reproduces today's frozen behavior unchanged. 1 is a real behavior
# change (includes single-occurrence series), not a no-op.
IRREGULAR_MIN_OCCURRENCES = 2

DEFAULT_PARAMS = {
    "min_occurrences": MIN_OCCURRENCES,
    "gap_tolerance": GAP_TOLERANCE,
    "irregular_mode": IRREGULAR_MODE,
    "amount_estimator": AMOUNT_ESTIMATOR,
    "irregular_min_occurrences": IRREGULAR_MIN_OCCURRENCES,
}


def _signed(amount: float, direction: str) -> float:
    return amount if direction == "credit" else -amount


def _series_key(e: Event) -> tuple[str, str]:
    return (e.user_id, e.description)


def _estimate_amount(events: list[Event], estimator: str) -> float:
    amounts = [e.amount for e in events]
    if estimator == "last":
        return amounts[-1]
    if estimator == "mean":
        return sum(amounts) / len(amounts)
    if estimator == "median":
        s = sorted(amounts)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    if estimator == "trimmed_mean" and len(amounts) >= 3:
        s = sorted(amounts)
        return sum(s[1:-1]) / len(s[1:-1])
    return amounts[-1]


def _add_month(d: dt.date, day_of_month: int) -> dt.date:
    month = d.month + 1
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day_of_month, last_day))


def classify_series(history: list[Event], min_occurrences: int = MIN_OCCURRENCES,
                     gap_tolerance: int = GAP_TOLERANCE) -> dict[tuple[str, str], str]:
    """{(user_id, description): 'recurring_monthly' | 'irregular'}."""
    by_series: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in history:
        by_series[_series_key(e)].append(e)

    classification = {}
    for key, events in by_series.items():
        events = sorted(events, key=lambda e: e.event_date)
        if len(events) < min_occurrences:
            classification[key] = "irregular"
            continue
        gaps = [(events[i + 1].event_date - events[i].event_date).days for i in range(len(events) - 1)]
        classification[key] = (
            "recurring_monthly" if all(30 - gap_tolerance <= g <= 30 + gap_tolerance for g in gaps)
            else "irregular"
        )
    return classification


@dataclass
class FlowItem:
    date: dt.date
    signed_amount: float          # credits +, debits -
    source_event_id: str          # known_future: that row's own id. recurring
                                   # projection: the series' last history event id
                                   # (the id spending_changes_needed references).
    is_recurring: bool            # True only for recurring_monthly projections
                                   # -- "only recurring expenses... may be changed"


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _project_irregular(events: list[Event], request_date: dt.date, horizon_end: dt.date,
                        mode: str, estimator: str, window_days: int) -> list[FlowItem]:
    """IRREGULAR_MODE variants (ARCHITECTURE.md §5.2/§5.4). `events` is one
    (user_id, description) series' history, sorted or not. `window_days` is
    the user's whole observation window (earliest history event ->
    request_date), shared across every series -- NOT each series' own span.
    A single-occurrence series' own span is the days since its one event,
    which for a recent one-off (e.g. 7 days ago) inflates its daily rate to
    ~total/7 and extrapolates it as a perpetual daily charge for 90 days.
    Dividing by the shared window instead spreads it over the full
    observation period, like every other series.

    Callers pre-filter `events` by IRREGULAR_MIN_OCCURRENCES before calling
    this, so a caller may legitimately pass a single-event series (that
    knob defaults to 2, matching the old hardcoded behavior, but can be set
    to 1). `median_gap_repeat` still needs its own >=2 guard below because a
    single event has no gap to take a median of, regardless of the knob --
    that's a mode-specific mathematical requirement, not a general
    min-occurrences policy."""
    if mode == "none":
        return []
    events = sorted(events, key=lambda e: e.event_date)
    last = events[-1]
    signed_amount = _signed(_estimate_amount(events, estimator), last.direction)
    items: list[FlowItem] = []

    if mode == "median_gap_repeat":
        if len(events) < 2:
            return []  # no gap to repeat from a single occurrence
        gaps = [(events[i + 1].event_date - events[i].event_date).days for i in range(len(events) - 1)]
        gap = max(1, round(_median([float(g) for g in gaps])))
        cursor = last.event_date
        while True:
            cursor = cursor + dt.timedelta(days=gap)
            if cursor > horizon_end:
                break
            if cursor > request_date:
                items.append(FlowItem(cursor, signed_amount, last.event_id, is_recurring=False))
        return items

    if mode == "flat_daily_burn":
        total = sum(e.amount for e in events)
        daily_rate = _signed(total, last.direction) / max(1, window_days)
        cursor = request_date
        while cursor < horizon_end:
            cursor = cursor + dt.timedelta(days=1)
            items.append(FlowItem(cursor, daily_rate, last.event_id, is_recurring=False))
        return items

    return items  # per_category_monthly_frequency is handled at the category level, see below


def _project_irregular_by_category(history: list[Event], request_date: dt.date, horizon_end: dt.date,
                                    estimator: str, exclude_keys: set, window_days: int) -> list[FlowItem]:
    """per_category_monthly_frequency: pool all irregular series by category
    (not by description), spread the average monthly historical spend as
    one lump per ~30 days forward. Only used when IRREGULAR_MODE ==
    'per_category_monthly_frequency'. Uses the shared `window_days` (see
    _project_irregular) rather than each category's own first-event span,
    for the same over-projection reason."""
    by_category: dict[str, list[Event]] = defaultdict(list)
    for e in history:
        if _series_key(e) in exclude_keys:
            continue
        by_category[e.category].append(e)

    items: list[FlowItem] = []
    for category, events in by_category.items():
        if len(events) < 2:
            continue
        events = sorted(events, key=lambda e: e.event_date)
        total = sum(e.amount for e in events)
        monthly_amount = total / max(1, window_days) * 30
        last = events[-1]
        signed_amount = _signed(monthly_amount, last.direction)
        cursor = request_date
        while True:
            cursor = cursor + dt.timedelta(days=30)
            if cursor > horizon_end:
                break
            items.append(FlowItem(cursor, signed_amount, last.event_id, is_recurring=False))
    return items


def project_flow_items(ledger: Ledger, request_date: dt.date,
                        horizon_days: int = HORIZON_DAYS,
                        params: dict | None = None) -> list[FlowItem]:
    """Flow list over (request_date, request_date+horizon], merged with
    known_future, with source_event_id retained per ARCHITECTURE.md §4.2
    ("a wrong forecast can be traced to the event or series that caused
    it") and so Layer 4 can apply stop:/reduce_to: spending changes."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    classification = classify_series(ledger.history, p["min_occurrences"], p["gap_tolerance"])
    by_series: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in ledger.history:
        by_series[_series_key(e)].append(e)

    horizon_end = request_date + dt.timedelta(days=horizon_days)
    items: list[FlowItem] = []

    known_future_dates: set[tuple[str, dt.date]] = set()
    for e in ledger.known_future:
        if request_date < e.event_date <= horizon_end:
            items.append(FlowItem(e.event_date, _signed(e.amount, e.direction), e.event_id, is_recurring=False))
            known_future_dates.add((e.description, e.event_date))

    irregular_keys = set()
    for key, events in by_series.items():
        if classification[key] != "recurring_monthly":
            irregular_keys.add(key)
            continue
        events = sorted(events, key=lambda e: e.event_date)
        last = events[-1]
        day_of_month = last.event_date.day
        signed_amount = _signed(_estimate_amount(events, p["amount_estimator"]), last.direction)
        cursor = last.event_date
        while True:
            cursor = _add_month(cursor, day_of_month)
            if cursor > horizon_end:
                break
            if cursor <= request_date:
                continue
            if (last.description, cursor) in known_future_dates:
                continue  # a real scheduled/pending row already covers this occurrence
            items.append(FlowItem(cursor, signed_amount, last.event_id, is_recurring=True))

    mode = p["irregular_mode"]
    window_days = max(1, (request_date - min(e.event_date for e in ledger.history)).days) if ledger.history else 1
    # IRREGULAR_MIN_OCCURRENCES applies to all three IRREGULAR_MODE values
    # alike: a series with fewer occurrences than this is dropped from the
    # irregular pool entirely -- not just from flat_daily_burn's numerator,
    # but from per_category_monthly_frequency's category pool too (a
    # category otherwise silently counts a one-off as a monthly recurrence).
    # The shared `window_days` denominator above is computed from the
    # user's WHOLE ledger.history regardless of this filter, so dropping a
    # series here can only lower the projected rate, never shrink the
    # window it's divided by.
    irregular_keys_for_projection = {k for k in irregular_keys
                                      if len(by_series[k]) >= p["irregular_min_occurrences"]}
    if mode == "per_category_monthly_frequency":
        items.extend(_project_irregular_by_category(ledger.history, request_date, horizon_end,
                                                      p["amount_estimator"],
                                                      set(by_series) - irregular_keys_for_projection,
                                                      window_days))
    elif mode != "none":
        for key in irregular_keys_for_projection:
            items.extend(_project_irregular(by_series[key], request_date, horizon_end, mode,
                                             p["amount_estimator"], window_days))

    return items


def recurring_series_representatives(ledger: Ledger, min_occurrences: int = MIN_OCCURRENCES,
                                      gap_tolerance: int = GAP_TOLERANCE) -> dict[str, Event]:
    """{last_history_event_id: last_history_event} for every series classified
    recurring_monthly -- the event_id spending_changes_needed refers to, and
    the source of its flexibility/category/minimum_allowed_amount."""
    classification = classify_series(ledger.history, min_occurrences, gap_tolerance)
    by_series: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for e in ledger.history:
        by_series[_series_key(e)].append(e)

    reps = {}
    for key, events in by_series.items():
        if classification[key] != "recurring_monthly":
            continue
        last = max(events, key=lambda e: e.event_date)
        reps[last.event_id] = last
    return reps


def items_to_dict(items: list[FlowItem]) -> dict[dt.date, float]:
    flows: dict[dt.date, float] = defaultdict(float)
    for item in items:
        flows[item.date] += item.signed_amount
    return dict(flows)


def project_flows(ledger: Ledger, request_date: dt.date, horizon_days: int = HORIZON_DAYS,
                   params: dict | None = None) -> dict[dt.date, float]:
    """Returns {date: net signed daily flow}. Thin wrapper over
    project_flow_items for callers (Phase 1 tests) that don't need
    per-source traceability or spending-change application."""
    return items_to_dict(project_flow_items(ledger, request_date, horizon_days, params))
