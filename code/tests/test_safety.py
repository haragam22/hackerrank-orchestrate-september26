"""Phase 1 gate — unit tests for code/safety.py, hand-built fixtures only.
Run: python code/tests/test_safety.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safety

REQ_DATE = dt.date(2024, 1, 1)


def test_is_safe_flat_balance_no_flows():
    assert safety.is_safe(1000, 100, {}, [(REQ_DATE, 800)], REQ_DATE) is True
    assert safety.is_safe(1000, 100, {}, [(REQ_DATE, 901)], REQ_DATE) is False


def test_is_safe_checks_every_day_not_just_the_end():
    # balance ends healthy at day 90 but dips below minimum on day 40 —
    # must be caught, not masked by a good final balance.
    flows = {REQ_DATE + dt.timedelta(days=40): -2000.0,
             REQ_DATE + dt.timedelta(days=89): 3000.0}
    assert safety.is_safe(1000, 100, flows, [], REQ_DATE) is False


def test_amount_safe_to_pay_matches_manual_min_balance():
    # balance starts 1000, min_balance 100. A debit of 300 on day 10, a
    # credit of 200 on day 50. Running balances: day0..9=1000, day10..49=700,
    # day50..90=900. Min running balance = 700. Safe amount = 700-100=600.
    flows = {REQ_DATE + dt.timedelta(days=10): -300.0,
             REQ_DATE + dt.timedelta(days=50): 200.0}
    result = safety.amount_safe_to_pay(1000, 100, flows, REQ_DATE, requested_amount=10000)
    assert result == 600.0, f"expected 600.0, got {result}"


def test_amount_safe_to_pay_clamped_to_requested_amount():
    result = safety.amount_safe_to_pay(100000, 0, {}, REQ_DATE, requested_amount=50)
    assert result == 50.0, "must never exceed requested_amount even with huge headroom"


def test_amount_safe_to_pay_floors_at_zero():
    result = safety.amount_safe_to_pay(50, 100, {}, REQ_DATE, requested_amount=500)
    assert result == 0.0, "already below minimum balance -> nothing is safe to pay"


def test_earliest_date_none_when_never_safe():
    result = safety.earliest_date_for_full_payment(50, 100, {}, REQ_DATE, requested_amount=500)
    assert result is None


def test_earliest_date_lands_on_payday_after_credit():
    payday = REQ_DATE + dt.timedelta(days=14)
    flows = {payday: 1000.0}
    result = safety.earliest_date_for_full_payment(100, 50, flows, REQ_DATE, requested_amount=900)
    assert result == payday, f"expected {payday}, got {result}"


def test_earliest_date_ignores_spending_changes_by_construction():
    # earliest_date_for_full_payment never takes spending_changes as an
    # argument at all -- this test just documents/locks that contract.
    import inspect
    sig = inspect.signature(safety.earliest_date_for_full_payment)
    assert "spending_changes" not in sig.parameters


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
