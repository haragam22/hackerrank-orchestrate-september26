"""Phase 4 gate — unit tests for code/explain.py, one fixture per template.
Every fixture reproduces the exact decision_explanation string of a real
sample_requests.csv row (formatting is exact-match sensitive).
Run: python code/tests/test_explain.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from io_loader import Event, Profile, Request
from explain import generate_explanation
from planner import Plan, SpendingChange


def _profile(**kw) -> Profile:
    base = dict(
        user_id="u1", home_currency="ZAR", current_available_balance=0.0, minimum_balance_to_keep=18000.0,
        financial_priorities=[], expense_categories_to_protect=[], expense_categories_user_is_willing_to_reduce=[],
        expense_categories_user_is_willing_to_stop=[], payment_methods_user_will_consider=[],
        max_installment_months=None,
    )
    base.update(kw)
    return Profile(**base)


def _request(**kw) -> Request:
    base = dict(
        request_id="r1", user_id="u1", request_date=dt.date(2024, 3, 3), request_type="purchase",
        requested_amount=25256.0, desired_completion_date=dt.date(2024, 3, 20),
        allows_partial_payment=True, request_text="",
    )
    base.update(kw)
    return Request(**base)


# template 1 -- request_01
def test_template_full_payment_now():
    profile = _profile(home_currency="ZAR", minimum_balance_to_keep=18000.0)
    req = _request(request_date=dt.date(2024, 3, 3))
    plan = Plan("affordable_now", "full_payment", [(dt.date(2024, 3, 3), 25256.0)], [], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=25256.0, events_by_id={})
    assert got == "Pay ZAR 25,256 today. This leaves at least ZAR 18,000 available over the next 90 days.", got


# template 2 -- request_02
def test_template_installments():
    profile = _profile(home_currency="IDR", minimum_balance_to_keep=29158400.0)
    req = _request(request_date=dt.date(2025, 8, 5))
    plan = Plan("affordable_with_plan", "installments",
                [(dt.date(2025, 8, 8), 15952906.67), (dt.date(2025, 9, 7), 15952906.67),
                 (dt.date(2025, 10, 7), 15952906.67)], [], "payment_option_05")
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0, events_by_id={})
    assert got == ("Use 3 installments of IDR 15,952,906.67, starting 8 August 2025. "
                    "This leaves at least IDR 29,158,400 available."), got


# template 3 -- request_03 (wait)
def test_template_wait():
    profile = _profile(home_currency="IDR", minimum_balance_to_keep=2668700.0)
    req = _request()
    plan = Plan("affordable_later", "wait", [(dt.date(2019, 11, 15), 5491000.0)], [], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0, events_by_id={})
    assert got == ("Pay IDR 5,491,000 in full on 15 November 2019. Paying earlier would take the "
                    "balance below the IDR 2,668,700 minimum."), got


# template 5 -- request_05 (not_affordable, low ratio -> deadline template)
def test_template_not_affordable_deadline():
    profile = _profile(home_currency="ZAR", minimum_balance_to_keep=13100.0)
    req = _request(requested_amount=15488.0, desired_completion_date=dt.date(2026, 1, 12))
    plan = Plan("not_affordable", "not_recommended", [], [], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=737.0, events_by_id={})
    assert got == ("Do not make this payment by 12 January 2026. None of the available options "
                    "keeps the ZAR 13,100 minimum protected."), got


# template 6 -- request_14 (not_affordable, high ratio -> insufficient template)
def test_template_not_affordable_insufficient():
    profile = _profile(home_currency="EUR", minimum_balance_to_keep=1000.0)
    req = _request(requested_amount=5414.20, desired_completion_date=dt.date(2025, 10, 4))
    plan = Plan("not_affordable", "not_recommended", [], [], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=597.74, events_by_id={})
    assert got == ("Do not proceed with the EUR 5,414.20 request. Although EUR 597.74 is available "
                    "today, the full amount cannot be completed safely within 90 days."), got


# template 7a -- request_06 (single stop)
def test_template_spending_change_stop():
    profile = _profile(home_currency="EUR", minimum_balance_to_keep=800.0)
    req = _request(request_date=dt.date(2026, 1, 3))
    ev = Event(event_id="event_476", user_id="u1", event_type="expense", description="Family streaming plan",
               category="streaming", direction="debit", amount=0, currency="EUR", event_date=dt.date(2025, 10, 1),
               settlement_date=None, status="settled", linked_event_id=None, flexibility="stoppable",
               minimum_allowed_amount=None)
    plan = Plan("affordable_with_plan", "full_payment", [(dt.date(2026, 1, 3), 620.40)],
                [SpendingChange("stop", "event_476")], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0, events_by_id={"event_476": ev})
    assert got == "Stop the family streaming plan, then pay EUR 620.40 today. This leaves at least EUR 800 available.", got


# template 7b -- request_11 (single reduce_to)
def test_template_spending_change_reduce():
    profile = _profile(home_currency="IDR", minimum_balance_to_keep=34140600.0)
    req = _request(request_date=dt.date(2025, 5, 3))
    ev = Event(event_id="event_989", user_id="u1", event_type="expense", description="Weekend food delivery",
               category="dining", direction="debit", amount=0, currency="IDR", event_date=dt.date(2025, 3, 1),
               settlement_date=None, status="settled", linked_event_id=None, flexibility="reducible",
               minimum_allowed_amount=665950.0)
    plan = Plan("affordable_with_plan", "full_payment", [(dt.date(2025, 5, 3), 13110000.0)],
                [SpendingChange("reduce_to", "event_989", 665950.0)], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0, events_by_id={"event_989": ev})
    assert got == ("Reduce the weekend food delivery to IDR 665,950, then pay IDR 13,110,000 today. "
                    "This leaves at least IDR 34,140,600 available."), got


# template 7c -- request_21 (stop + reduce_to combined)
def test_template_spending_change_stop_and_reduce():
    profile = _profile(home_currency="USD", minimum_balance_to_keep=1800.0)
    req = _request(request_date=dt.date(2026, 4, 3))
    ev1 = Event(event_id="event_1815", user_id="u1", event_type="expense", description="Online backup subscription",
                category="cloud_storage", direction="debit", amount=0, currency="USD", event_date=dt.date(2025, 12, 1),
                settlement_date=None, status="settled", linked_event_id=None, flexibility="stoppable",
                minimum_allowed_amount=None)
    ev2 = Event(event_id="event_1816", user_id="u1", event_type="expense", description="Streaming subscription",
                category="streaming", direction="debit", amount=0, currency="USD", event_date=dt.date(2025, 12, 1),
                settlement_date=None, status="settled", linked_event_id=None, flexibility="reducible",
                minimum_allowed_amount=23.50)
    plan = Plan("affordable_with_plan", "full_payment", [(dt.date(2026, 4, 3), 1574.40)],
                [SpendingChange("stop", "event_1815"), SpendingChange("reduce_to", "event_1816", 23.50)], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0,
                                events_by_id={"event_1815": ev1, "event_1816": ev2})
    assert got == ("Stop the online backup subscription and reduce the streaming subscription to USD 23.50, "
                    "then pay USD 1,574.40 today. This leaves at least USD 1,800 available."), got


# regression: installments + spending change must NOT use the single-payment
# "pay X today" phrasing (caught via manual audit on request_150's real output)
def test_template_installments_with_spending_change():
    profile = _profile(home_currency="EUR", minimum_balance_to_keep=700.0)
    req = _request(request_date=dt.date(2026, 1, 6))
    ev = Event(event_id="event_13786", user_id="u1", event_type="expense", description="Family streaming plan",
               category="streaming", direction="debit", amount=0, currency="EUR", event_date=dt.date(2025, 10, 1),
               settlement_date=None, status="settled", linked_event_id=None, flexibility="stoppable",
               minimum_allowed_amount=None)
    plan = Plan("affordable_with_plan", "installments",
                [(dt.date(2026, 1, 6), 171.98), (dt.date(2026, 2, 5), 171.98), (dt.date(2026, 3, 7), 171.98)],
                [SpendingChange("stop", "event_13786")], "payment_option_x")
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=0.0, events_by_id={"event_13786": ev})
    assert got == ("Stop the family streaming plan, then use 3 installments of EUR 171.98, "
                    "starting 6 January 2026. This leaves at least EUR 700 available."), got
    assert "today" not in got, "installments plan must never claim a single same-day payment"


# template 8 -- request_19 (partial_payment)
def test_template_partial_payment():
    profile = _profile(home_currency="INR", minimum_balance_to_keep=92800.0)
    req = _request(request_date=dt.date(2024, 9, 4))
    plan = Plan("affordable_with_plan", "partial_payment",
                [(dt.date(2024, 9, 4), 28820.0), (dt.date(2024, 9, 15), 10840.0)], [], None)
    got = generate_explanation(req, profile, plan, amount_safe_to_pay=28820.0, events_by_id={})
    assert got == ("Pay INR 28,820 today and the remaining INR 10,840 on 15 September 2024. "
                    "This completes the full request and keeps the INR 92,800 minimum protected."), got


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
