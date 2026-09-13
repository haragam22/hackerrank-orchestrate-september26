"""Phase 1 gate — unit tests for code/ledger.py, hand-built fixtures only.
Run: python code/tests/test_ledger.py
"""
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from io_loader import Dataset, Event, ExchangeRate, Profile
import ledger


def _event(**kw) -> Event:
    base = dict(
        event_id="e", user_id="u1", event_type="expense", description="d",
        category="rent", direction="debit", amount=100.0, currency="ZAR",
        event_date=dt.date(2024, 1, 1), settlement_date=None, status="settled",
        linked_event_id=None, flexibility="fixed", minimum_allowed_amount=None,
    )
    base.update(kw)
    return Event(**base)


def _profile(**kw) -> Profile:
    base = dict(
        user_id="u1", home_currency="ZAR", current_available_balance=1000.0,
        minimum_balance_to_keep=100.0, financial_priorities=[], expense_categories_to_protect=[],
        expense_categories_user_is_willing_to_reduce=[], expense_categories_user_is_willing_to_stop=[],
        payment_methods_user_will_consider=["full_payment"], max_installment_months=None,
    )
    base.update(kw)
    return Profile(**base)


def _ds(events: list[Event], profile: Profile, rates: list[ExchangeRate] | None = None) -> Dataset:
    ds = Dataset()
    ds.profiles[profile.user_id] = profile
    ds.events_by_user[profile.user_id] = events
    for e in events:
        ds.events_by_id[e.event_id] = e
    ds.exchange_rates = rates or []
    return ds


def test_linked_chain_collapses_to_settlement():
    auth = _event(event_id="e1", status="cancelled", amount=500.0)
    settle = _event(event_id="e2", status="settled", linked_event_id="e1", amount=500.0)
    ds = _ds([auth, settle], _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={})
    ids = {e.event_id for e in cleaned}
    assert ids == {"e2"}, f"expected only settlement to remain, got {ids}"


def test_refund_kept_as_credit():
    settle = _event(event_id="e2", status="settled", amount=500.0, direction="debit")
    refund = _event(event_id="e3", status="settled", linked_event_id="e2", amount=500.0, direction="credit")
    ds = _ds([settle, refund], _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={})
    ids = {e.event_id for e in cleaned}
    assert ids == {"e2", "e3"}, f"expected settlement + refund both kept, got {ids}"


def test_cancelled_failed_unrealized_dropped():
    events = [
        _event(event_id="c1", status="cancelled"),
        _event(event_id="f1", status="failed"),
        _event(event_id="u1e", status="unrealized"),
        _event(event_id="ok1", status="settled"),
        _event(event_id="ncash", status="settled", direction="non_cash"),
    ]
    ds = _ds(events, _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={})
    ids = {e.event_id for e in cleaned}
    assert ids == {"ok1"}, f"expected only ok1, got {ids}"


def test_pending_debit_kept_pending_credit_excluded_from_known_future_by_status_split():
    # ledger.py doesn't drop pending credits itself (that's recurrence's job
    # not to project them as future income beyond what's explicit) but the
    # split correctly buckets a pending debit as known_future.
    debit = _event(event_id="pd", status="pending", direction="debit",
                    event_date=dt.date(2024, 2, 1))
    ds = _ds([debit], _profile())
    lg = ledger.build_ledger(ds, "u1", request_date=dt.date(2024, 1, 15))
    future_ids = {e.event_id for e in lg.known_future}
    assert "pd" in future_ids, "pending debit after request_date must appear in known_future"


def test_blank_amount_dropped_without_cache():
    blank = _event(event_id="b1", amount=None)
    ds = _ds([blank], _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={})
    assert cleaned == [], "blank amount with no cache entry must be dropped, not zeroed"


def test_blank_amount_filled_from_cache():
    blank = _event(event_id="b1", amount=None, currency="ZAR")
    ds = _ds([blank], _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={"b1": {"amount": 777.0, "currency": "ZAR"}}, amendments={})
    assert len(cleaned) == 1 and cleaned[0].amount == 777.0


def test_fx_exact_date_match():
    rate = ExchangeRate(rate_date=dt.date(2024, 1, 1), from_currency="EUR", to_currency="ZAR", rate=20.0)
    result = ledger.fx_to_home(10.0, "EUR", "ZAR", dt.date(2024, 1, 1), [rate])
    assert result == 200.0


def test_fx_falls_back_to_latest_rate_on_or_before():
    older = ExchangeRate(rate_date=dt.date(2024, 1, 1), from_currency="EUR", to_currency="ZAR", rate=20.0)
    newer = ExchangeRate(rate_date=dt.date(2024, 1, 10), from_currency="EUR", to_currency="ZAR", rate=21.0)
    result = ledger.fx_to_home(10.0, "EUR", "ZAR", dt.date(2024, 1, 20), [older, newer])
    assert result == 210.0, "should use the latest rate on/before the event date (21.0), not the first one found"


def test_amendment_cancel_event_drops_row():
    e = _event(event_id="e9")
    ds = _ds([e], _profile())
    cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={"e9": {"action": "cancel_event"}})
    assert cleaned == []


def test_amendment_ignore_and_confirm_are_noops():
    e = _event(event_id="e9", amount=100.0)
    ds = _ds([e], _profile())
    for action in ("ignore", "confirm_event"):
        cleaned = ledger.clean_events(ds, "u1", image_amounts={}, amendments={"e9": {"action": action}})
        assert len(cleaned) == 1 and cleaned[0].amount == 100.0


def test_user_01_real_chain_dedup_from_dataset():
    """Real-data regression, not a synthetic fixture -- catches CSV parsing
    issues the hand-built fixtures above can't. user_01 has two linked
    chains: event_98 (settled) -> event_99 (refund, settled, linked to 98)
    and event_100 (cancelled auth) -> event_101 (settled purchase, linked to
    100), plus event_102 (still-pending fuel authorization, unlinked).
    Investigated per user request after request_01's amount_safe_to_pay
    came in short under flat_daily_burn -- confirms the shortfall is a
    forecasting issue (see recurrence.py), not a ledger dedup leak, before
    trusting any calibration sweep result built on top of it."""
    import io_loader
    ds = io_loader.load()
    cleaned = ledger.clean_events(ds, "user_01")
    by_id = {e.event_id: e for e in cleaned}

    assert "event_100" not in by_id, "cancelled Card authorization must not survive"
    assert by_id["event_101"].amount == 816.2 and by_id["event_101"].direction == "debit", \
        "Settled card purchase must appear exactly once, as a debit"
    assert by_id["event_99"].direction == "credit" and by_id["event_99"].amount == 583.0, \
        "refund must be kept as its own credit"
    assert by_id["event_102"].status == "pending" and by_id["event_102"].direction == "debit", \
        "still-pending fuel authorization must be kept as a pending debit (not dropped, not double-counted)"
    ids = [e.event_id for e in cleaned]
    assert len(ids) == len(set(ids)), "no event_id should appear twice after dedup"


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
