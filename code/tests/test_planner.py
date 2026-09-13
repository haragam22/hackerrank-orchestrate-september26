"""Phase 2 gate — unit tests for code/planner.py, hand-built fixtures only.
Run: python code/tests/test_planner.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from io_loader import Event, PaymentOption, Profile, Request
from ledger import Ledger
import planner
from recurrence import FlowItem


def _profile(**kw) -> Profile:
    base = dict(
        user_id="u1", home_currency="ZAR", current_available_balance=100000.0,
        minimum_balance_to_keep=1000.0, financial_priorities=[], expense_categories_to_protect=[],
        expense_categories_user_is_willing_to_reduce=[], expense_categories_user_is_willing_to_stop=[],
        payment_methods_user_will_consider=["full_payment", "installments", "partial_payment"],
        max_installment_months=None,
    )
    base.update(kw)
    return Profile(**base)


def _request(**kw) -> Request:
    base = dict(
        request_id="r1", user_id="u1", request_date=dt.date(2024, 1, 1), request_type="purchase",
        requested_amount=1000.0, desired_completion_date=dt.date(2024, 6, 1),
        allows_partial_payment=True, request_text="",
    )
    base.update(kw)
    return Request(**base)


def _option(**kw) -> PaymentOption:
    base = dict(
        payment_option_id="opt_01", request_id="r1", payment_method="installments",
        payment_amount=100.0, number_of_payments=3, first_payment_date=dt.date(2024, 1, 1),
        payment_frequency_days=30, financing_fee=0.0, total_payable_amount=300.0,
    )
    base.update(kw)
    return PaymentOption(**base)


def _event(**kw) -> Event:
    base = dict(
        event_id="e1", user_id="u1", event_type="expense", description="d", category="dining",
        direction="debit", amount=100.0, currency="ZAR", event_date=dt.date(2024, 1, 1),
        settlement_date=None, status="settled", linked_event_id=None,
        flexibility="reducible_or_stoppable", minimum_allowed_amount=20.0,
    )
    base.update(kw)
    return Event(**base)


# --- confirmed rule: max_installment_months is a payment-COUNT cap ---

def test_max_installment_months_excludes_by_payment_count_not_duration():
    profile = _profile(max_installment_months=7)
    req = _request(requested_amount=300.0)
    big = planner.Candidate("installments", [(dt.date(2024, 1, 1), 50.0)] * 18, [], payment_option_id="opt_18")
    small = planner.Candidate("installments", [(dt.date(2024, 1, 1), 100.0)] * 3, [], payment_option_id="opt_03")
    assert not planner._eligible_immediate(big, req, profile), "18-payment option must be excluded by a 7 cap"
    assert planner._eligible_immediate(small, req, profile), "3-payment option must be allowed under a 7 cap"


def test_max_installment_months_none_means_installments_never_eligible():
    profile = _profile(max_installment_months=None)
    req = _request()
    cand = planner.Candidate("installments", [(dt.date(2024, 1, 1), 100.0)], [], payment_option_id="opt_01")
    assert not planner._eligible_immediate(cand, req, profile)


# --- confirmed rule: installment dates = first_payment_date + k*freq ---

def test_installment_dates_formula():
    req = _request()
    profile = _profile()
    opt = _option(first_payment_date=dt.date(2025, 8, 8), payment_frequency_days=30, number_of_payments=3,
                  payment_amount=100.0)
    cands = planner.generate_candidates(req, profile, [opt], amount_safe_to_pay=0.0, earliest_date=None)
    inst = next(c for c in cands if c.method == "installments")
    dates = [d for d, _ in inst.schedule]
    assert dates == [dt.date(2025, 8, 8), dt.date(2025, 9, 7), dt.date(2025, 10, 7)]


# --- partial-payment eligibility ---

def test_partial_rejected_when_allows_partial_payment_false():
    req = _request(allows_partial_payment=False)
    profile = _profile()
    cand = planner.Candidate("partial_payment", [(req.request_date, 300.0), (dt.date(2024, 2, 1), 700.0)], [])
    assert not planner._eligible_immediate(cand, req, profile)


def test_partial_rejected_when_completion_after_deadline():
    req = _request(desired_completion_date=dt.date(2024, 1, 15), allows_partial_payment=True)
    profile = _profile()
    cand = planner.Candidate("partial_payment", [(req.request_date, 300.0), (dt.date(2024, 2, 1), 700.0)], [])
    assert not planner._eligible_immediate(cand, req, profile)


def test_partial_candidate_not_generated_outside_open_range():
    req = _request(requested_amount=1000.0)
    profile = _profile()
    # amount_safe_to_pay == requested_amount -> not a partial candidate (should be full)
    cands = planner.generate_candidates(req, profile, [], amount_safe_to_pay=1000.0, earliest_date=req.request_date)
    assert not any(c.method == "partial_payment" for c in cands)
    # amount_safe_to_pay == 0 -> not a partial candidate either
    cands = planner.generate_candidates(req, profile, [], amount_safe_to_pay=0.0, earliest_date=req.request_date)
    assert not any(c.method == "partial_payment" for c in cands)


# --- spending-change subsets: mutual exclusion + reduce_to value ---

def test_subset_never_both_stop_and_reduce_same_event():
    ev = _event(event_id="e1", flexibility="reducible_or_stoppable", category="dining",
                minimum_allowed_amount=20.0)
    profile = _profile(expense_categories_user_is_willing_to_reduce=["dining"],
                        expense_categories_user_is_willing_to_stop=["dining"])
    eligible = [(ev, {"stop", "reduce_to"})]
    for subset in planner._spending_change_subsets(eligible):
        ids_seen = [c.event_id for c in subset]
        assert len(ids_seen) == len(set(ids_seen)), "same event_id must not appear twice in one subset"


def test_reduce_to_value_is_always_minimum_allowed_amount():
    ev = _event(event_id="e1", flexibility="reducible", category="dining", minimum_allowed_amount=42.0)
    profile = _profile(expense_categories_user_is_willing_to_reduce=["dining"])
    ledger = Ledger(history=[ev, _event(event_id="e2", event_date=dt.date(2023, 12, 1)),
                              _event(event_id="e3", event_date=dt.date(2023, 11, 1))], known_future=[])
    eligible = planner.eligible_spending_change_events(ledger, profile)
    reduce_changes = [c for _, subset in
                       [(None, s) for s in planner._spending_change_subsets(eligible)]
                       for c in subset if c.kind == "reduce_to"]
    assert reduce_changes, "expected at least one reduce_to change to be enumerated"
    assert all(c.new_amount == 42.0 for c in reduce_changes)


def test_eligibility_requires_both_flexibility_and_category_permission():
    # flexibility permits reduce, but category not in the user's willing-to-reduce list
    ev = _event(flexibility="reducible", category="rent", minimum_allowed_amount=10.0)
    profile = _profile(expense_categories_user_is_willing_to_reduce=["dining"])
    ledger = Ledger(history=[ev, _event(event_id="e2", event_date=dt.date(2023, 12, 1), category="rent"),
                              _event(event_id="e3", event_date=dt.date(2023, 11, 1), category="rent")], known_future=[])
    eligible = planner.eligible_spending_change_events(ledger, profile)
    assert eligible == [], "category not in willing-to-reduce list -> not eligible even though flexibility permits it"


# --- ranking tie-break order, one fixture per level ---

def test_rank_prefers_completing_by_deadline():
    req = _request(desired_completion_date=dt.date(2024, 2, 1))
    late = planner.Candidate("wait", [(dt.date(2024, 3, 1), 1000.0)], [])
    on_time = planner.Candidate("wait", [(dt.date(2024, 1, 15), 1000.0)], [])
    ranked = sorted([late, on_time], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is on_time


def test_rank_prefers_fewer_spending_changes():
    req = _request()
    with_change = planner.Candidate("full_payment", [(req.request_date, 1000.0)],
                                     [planner.SpendingChange("stop", "e1")])
    without = planner.Candidate("full_payment", [(req.request_date, 1000.0)], [])
    ranked = sorted([with_change, without], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is without


def test_rank_prefers_lower_total_paid():
    req = _request()
    expensive = planner.Candidate("installments", [(req.request_date, 600.0), (dt.date(2024, 2, 1), 600.0)], [])
    cheap = planner.Candidate("installments", [(req.request_date, 500.0), (dt.date(2024, 2, 1), 500.0)], [])
    ranked = sorted([expensive, cheap], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is cheap


def test_rank_prefers_earlier_start():
    req = _request()
    later_start = planner.Candidate("wait", [(dt.date(2024, 2, 1), 1000.0)], [])
    earlier_start = planner.Candidate("wait", [(dt.date(2024, 1, 10), 1000.0)], [])
    ranked = sorted([later_start, earlier_start], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is earlier_start


def test_rank_prefers_fewer_payments():
    req = _request()
    two_pay = planner.Candidate("partial_payment", [(req.request_date, 500.0), (dt.date(2024, 1, 2), 500.0)], [])
    one_pay = planner.Candidate("full_payment", [(req.request_date, 1000.0)], [])
    ranked = sorted([two_pay, one_pay], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is one_pay


def test_rank_lowest_payment_option_id_is_final_tiebreak():
    req = _request()
    opt_b = planner.Candidate("installments", [(req.request_date, 1000.0)], [], payment_option_id="payment_option_09")
    opt_a = planner.Candidate("installments", [(req.request_date, 1000.0)], [], payment_option_id="payment_option_02")
    ranked = sorted([opt_b, opt_a], key=lambda c: planner._rank_key(c, req))
    assert ranked[0] is opt_a


# --- status-mapping table, one fixture per row ---

def _tiny_ledger() -> Ledger:
    return Ledger(history=[], known_future=[])


def test_status_affordable_now():
    profile = _profile(current_available_balance=100000.0, minimum_balance_to_keep=100.0,
                        payment_methods_user_will_consider=["full_payment"])
    req = _request(requested_amount=1000.0)
    plan = planner.build_plan(req, profile, _tiny_ledger(), [])
    assert plan.status == "affordable_now"
    assert plan.method == "full_payment"


def test_status_not_affordable():
    profile = _profile(current_available_balance=100.0, minimum_balance_to_keep=100.0,
                        payment_methods_user_will_consider=["full_payment"])
    req = _request(requested_amount=100000.0, desired_completion_date=dt.date(2024, 1, 5))
    plan = planner.build_plan(req, profile, _tiny_ledger(), [])
    assert plan.status == "not_affordable"
    assert plan.method == "not_recommended"
    assert plan.schedule == []


def test_status_affordable_later_via_wait():
    ev = Event(event_id="salary1", user_id="u1", event_type="income", description="salary", category="salary",
               direction="credit", amount=200000.0, currency="ZAR", event_date=dt.date(2024, 1, 20),
               settlement_date=dt.date(2024, 1, 20), status="scheduled", linked_event_id=None,
               flexibility="fixed", minimum_allowed_amount=None)
    ledger = Ledger(history=[], known_future=[ev])
    profile = _profile(current_available_balance=100.0, minimum_balance_to_keep=50.0,
                        payment_methods_user_will_consider=["full_payment"])
    req = _request(requested_amount=1000.0, desired_completion_date=dt.date(2024, 3, 1))
    plan = planner.build_plan(req, profile, ledger, [])
    assert plan.status == "affordable_later"
    assert plan.method == "wait"
    assert plan.schedule == [(dt.date(2024, 1, 20), 1000.0)]


def test_status_affordable_with_plan_via_spending_change():
    stoppable = _event(event_id="sub1", event_date=dt.date(2023, 10, 1), category="delivery_membership",
                        flexibility="stoppable", amount=50000.0)
    stoppable2 = _event(event_id="sub2", event_date=dt.date(2023, 11, 1), category="delivery_membership",
                         flexibility="stoppable", amount=50000.0)
    stoppable3 = _event(event_id="sub3", event_date=dt.date(2023, 12, 1), category="delivery_membership",
                         flexibility="stoppable", amount=50000.0)
    ledger = Ledger(history=[stoppable, stoppable2, stoppable3], known_future=[])
    profile = _profile(current_available_balance=50100.0, minimum_balance_to_keep=100.0,
                        payment_methods_user_will_consider=["full_payment"],
                        expense_categories_user_is_willing_to_stop=["delivery_membership"])
    req = _request(requested_amount=50000.0, request_date=dt.date(2023, 12, 15),
                    desired_completion_date=dt.date(2023, 12, 20))
    plan = planner.build_plan(req, profile, ledger, [])
    assert plan.status == "affordable_with_plan", f"expected affordable_with_plan, got {plan.status}"
    assert any(c.kind == "stop" and c.event_id == "sub3" for c in plan.spending_changes)


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
        except Exception as exc:  # noqa: BLE001 -- surface unexpected errors as failures too
            failed += 1
            print(f"ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
