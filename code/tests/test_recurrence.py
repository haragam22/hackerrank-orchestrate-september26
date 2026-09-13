"""Unit tests for code/recurrence.py's IRREGULAR_MODE mechanisms and the
IRREGULAR_MIN_OCCURRENCES knob. Hand-built fixtures, no dataset dependency
(test_ledger.py's test_user_01_real_chain_dedup_from_dataset covers the one
real-data regression this suite needed).

Guards the window_days fix directly: a user-requested test construction
built on the ORIGINAL (incorrect) theory that a single-occurrence series
gets extrapolated to a perpetual daily charge doesn't actually exercise the
fix, because such series are excluded before either the buggy or the fixed
code ever computes a rate (see the len(events)<2 / IRREGULAR_MIN_OCCURRENCES
guard in _project_irregular). The regression that actually matters is a
*multi*-occurrence series whose own span is much shorter than the ledger's
whole observation window -- that's what the old code got wrong.

Run: python code/tests/test_recurrence.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from io_loader import Event
from ledger import Ledger
import recurrence


def _event(**kw) -> Event:
    base = dict(
        event_id="e", user_id="u1", event_type="expense", description="d",
        category="groceries", direction="debit", amount=100.0, currency="ZAR",
        event_date=dt.date(2024, 1, 1), settlement_date=None, status="settled",
        linked_event_id=None, flexibility="fixed", minimum_allowed_amount=None,
    )
    base.update(kw)
    return Event(**base)


REQUEST_DATE = dt.date(2024, 6, 1)  # 152 days after the anchor event below


def _ledger(history: list[Event]) -> Ledger:
    return Ledger(history=history, known_future=[])


def test_flat_daily_burn_uses_shared_window_not_own_series_span():
    """The window_days regression this investigation actually produced.
    Series A: 2 occurrences 7 days apart, both recent (its own span is
    short). Series B: a single long-lived series that pushes the ledger's
    earliest history event back 150 days, so the *shared* observation
    window is ~150 days even though series A's own span is ~7 days. Under
    the pre-fix bug (denominator = series' own span), series A's daily
    rate would be ~1000/7 ~= 142.86 -- wildly larger than its true share of
    a 150-day history. Under the fix it must be ~1000/150 ~= 6.67."""
    anchor = _event(event_id="anchor", description="Old anchor expense",
                     event_date=REQUEST_DATE - dt.timedelta(days=150), amount=50.0)
    a1 = _event(event_id="a1", description="Short-span series", category="shopping",
                event_date=REQUEST_DATE - dt.timedelta(days=17), amount=500.0)
    a2 = _event(event_id="a2", description="Short-span series", category="shopping",
                event_date=REQUEST_DATE - dt.timedelta(days=10), amount=500.0)
    lg = _ledger([anchor, a1, a2])
    params = {"irregular_mode": "flat_daily_burn", "amount_estimator": "last",
              "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 2}
    items = recurrence.project_flow_items(lg, REQUEST_DATE, 90, params)
    from_series_a = [i for i in items if i.source_event_id == "a2"]
    assert from_series_a, "short-span series must still be projected (n=2 >= IRREGULAR_MIN_OCCURRENCES)"
    daily_rate = abs(from_series_a[0].signed_amount)

    true_window_rate = 1000.0 / 150   # correct: shared observation window
    buggy_own_span_rate = 1000.0 / 7  # wrong: what the pre-fix code produced

    assert abs(daily_rate - true_window_rate) < 0.5, (
        f"expected ~{true_window_rate:.2f}/day (shared window), got {daily_rate:.2f}/day")
    assert daily_rate < buggy_own_span_rate / 2, (
        f"daily rate {daily_rate:.2f} is suspiciously close to the pre-fix "
        f"own-span rate {buggy_own_span_rate:.2f} -- window_days fix may have regressed")


def test_single_occurrence_series_excluded_by_default():
    """IRREGULAR_MIN_OCCURRENCES defaults to 2 -- a lone event contributes
    nothing to flat_daily_burn, matching the pre-existing (undocumented
    until now) behavior this investigation initially misattributed."""
    anchor = _event(event_id="anchor", description="Old anchor expense",
                     event_date=REQUEST_DATE - dt.timedelta(days=150), amount=50.0)
    lone = _event(event_id="lone", description="One-off dinner", category="dining",
                  event_date=REQUEST_DATE - dt.timedelta(days=7), amount=1216.0)
    lg = _ledger([anchor, lone])
    params = {"irregular_mode": "flat_daily_burn", "amount_estimator": "last",
              "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 2}
    items = recurrence.project_flow_items(lg, REQUEST_DATE, 90, params)
    assert not any(i.source_event_id == "lone" for i in items), \
        "single-occurrence series must be excluded when irregular_min_occurrences=2 (the default)"


def test_irregular_min_occurrences_1_includes_single_occurrence_series():
    """Setting the knob to 1 is a real behavior change (not a no-op): it
    newly includes single-occurrence series, dividing by the shared window
    like everything else -- 1216/150, not 1216/7."""
    anchor = _event(event_id="anchor", description="Old anchor expense",
                     event_date=REQUEST_DATE - dt.timedelta(days=150), amount=50.0)
    lone = _event(event_id="lone", description="One-off dinner", category="dining",
                  event_date=REQUEST_DATE - dt.timedelta(days=7), amount=1216.0)
    lg = _ledger([anchor, lone])
    params = {"irregular_mode": "flat_daily_burn", "amount_estimator": "last",
              "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 1}
    items = recurrence.project_flow_items(lg, REQUEST_DATE, 90, params)
    matches = [i for i in items if i.source_event_id == "lone"]
    assert matches, "irregular_min_occurrences=1 must include single-occurrence series"
    daily_rate = abs(matches[0].signed_amount)
    assert abs(daily_rate - 1216.0 / 150) < 0.5, f"expected ~8.11/day (1216/150), got {daily_rate:.2f}"
    assert daily_rate < 1216.0 / 7 / 2, "must use the shared window, not the series' own 7-day span"


def test_median_gap_repeat_single_occurrence_is_well_defined_empty():
    """median_gap_repeat has no gap to take a median of from one event --
    must return nothing (not crash), even when irregular_min_occurrences=1
    would otherwise let the series through."""
    anchor = _event(event_id="anchor", description="Old anchor expense",
                     event_date=REQUEST_DATE - dt.timedelta(days=150), amount=50.0)
    lone = _event(event_id="lone", description="One-off dinner", category="dining",
                  event_date=REQUEST_DATE - dt.timedelta(days=7), amount=1216.0)
    lg = _ledger([anchor, lone])
    params = {"irregular_mode": "median_gap_repeat", "amount_estimator": "last",
              "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 1}
    items = recurrence.project_flow_items(lg, REQUEST_DATE, 90, params)  # must not raise
    assert not any(i.source_event_id == "lone" for i in items)


def test_per_category_monthly_frequency_respects_irregular_min_occurrences():
    """A one-off event must not be counted as a monthly recurrence when
    irregular_min_occurrences excludes it from the category pool -- but
    should be included (and pooled with same-category series) once the
    knob is lowered to 1. Two distinct one-off dining series are used so
    the category-pool-size guard (>=2 pooled events, a separate concern
    from the per-series threshold) is satisfied either way and doesn't
    confound what's being tested here."""
    anchor = _event(event_id="anchor", description="Old anchor expense", category="shopping",
                     event_date=REQUEST_DATE - dt.timedelta(days=150), amount=50.0)
    lone = _event(event_id="lone", description="One-off dinner", category="dining",
                  event_date=REQUEST_DATE - dt.timedelta(days=7), amount=1216.0)
    lone2 = _event(event_id="lone2", description="Another one-off dinner", category="dining",
                    event_date=REQUEST_DATE - dt.timedelta(days=9), amount=900.0)
    lg = _ledger([anchor, lone, lone2])
    excluded = {"irregular_mode": "per_category_monthly_frequency", "amount_estimator": "last",
                "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 2}
    included = {**excluded, "irregular_min_occurrences": 1}

    items_excluded = recurrence.project_flow_items(lg, REQUEST_DATE, 90, excluded)
    assert not any(i.source_event_id in ("lone", "lone2") for i in items_excluded), \
        "dining category pool must not include below-threshold (n=1) series"

    items_included = recurrence.project_flow_items(lg, REQUEST_DATE, 90, included)
    assert any(i.source_event_id in ("lone", "lone2") for i in items_included), \
        "lowering irregular_min_occurrences to 1 must let the category pool see them"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
