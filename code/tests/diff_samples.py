"""Detailed row-by-row, field-by-field diff of the full L1-L5 pipeline
against sample_requests.csv's labelled ground truth. Unlike
test_end_to_end.py (aggregate pass/fail counts), this prints every field's
predicted vs actual value for every one of the 25 samples, so mismatches
can be inspected and root-caused individually.

Run: python code/tests/diff_samples.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import explain
import io_loader
import ledger
import main as main_module
import planner
import recurrence
import safety


def format_plan(schedule):
    if not schedule:
        return "none"
    def fmt(a):
        return str(int(round(a))) if abs(a - round(a)) < 1e-6 else f"{a:.2f}"
    return "|".join(f"{d.isoformat()}:{fmt(a)}" for d, a in schedule)


def format_changes(changes):
    if not changes:
        return "none"
    parts = []
    for c in changes:
        if c.kind == "stop":
            parts.append(f"stop:{c.event_id}")
        else:
            amt = c.new_amount
            parts.append(f"reduce_to:{c.event_id}:{int(round(amt)) if abs(amt-round(amt))<1e-6 else amt}")
    return "|".join(parts)


def predict(ds, req, cfg):
    profile = ds.profiles[req.user_id]
    options = ds.payment_options_by_request.get(req.request_id, [])
    lg = ledger.build_ledger(ds, req.user_id, req.request_date, pending_debits=cfg["pending_debits"])
    plan = planner.build_plan(req, profile, lg, options, params=cfg["params"],
                               horizon_days=cfg["horizon_days"], inclusive=cfg["horizon_inclusive"])

    base_items = recurrence.project_flow_items(lg, req.request_date, cfg["horizon_days"], cfg["params"])
    flows = recurrence.items_to_dict(base_items)
    amount_safe = safety.amount_safe_to_pay(profile.current_available_balance, profile.minimum_balance_to_keep,
                                             flows, req.request_date, req.requested_amount,
                                             cfg["horizon_days"], cfg["horizon_inclusive"])
    earliest = safety.earliest_date_for_full_payment(profile.current_available_balance,
                                                      profile.minimum_balance_to_keep, flows,
                                                      req.request_date, req.requested_amount,
                                                      cfg["horizon_days"], cfg["horizon_inclusive"])
    explanation = explain.generate_explanation(req, profile, plan, amount_safe, ds.events_by_id)

    return {
        "amount_safe_to_pay": amount_safe,
        "affordability_status": plan.status,
        "recommended_payment_method": plan.method,
        "payment_plan": format_plan(plan.schedule),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": format_changes(plan.spending_changes),
        "decision_explanation": explanation,
    }


def amounts_close(a: str, b: float, tol: float = 1.0) -> bool:
    try:
        return abs(float(a) - b) < tol
    except (ValueError, TypeError):
        return False


def plans_match(a: str, b: str, tol: float = 1.0) -> bool:
    def parse(s):
        s = (s or "").strip()
        if not s or s == "none":
            return []
        out = []
        for part in s.split("|"):
            d, amt = part.split(":")
            out.append((d, float(amt)))
        return out
    pa, pb = parse(a), parse(b)
    if len(pa) != len(pb):
        return False
    return all(da == db and abs(aa - ab) < tol for (da, aa), (db, ab) in zip(pa, pb))


def main():
    ds = io_loader.load()
    cfg = main_module.load_config()
    print(f"Using params.json config: {cfg['params']}, pending_debits={cfg['pending_debits']}, "
          f"horizon_inclusive={cfg['horizon_inclusive']}\n")
    field_hits = {"amount_safe_to_pay": 0, "affordability_status": 0, "recommended_payment_method": 0,
                  "payment_plan": 0, "earliest_date_for_full_payment": 0, "spending_changes_needed": 0,
                  "decision_explanation": 0}
    row_exact = 0
    mismatch_log = []

    for req in ds.sample_requests:
        gt = ds.sample_ground_truth[req.request_id]
        pred = predict(ds, req, cfg)

        print(f"\n{'='*100}\n{req.request_id}  (user {req.user_id}, {req.request_type}, "
              f"requested {req.requested_amount}, request_date {req.request_date}, "
              f"deadline {req.desired_completion_date})")

        row_ok = True
        checks = [
            ("amount_safe_to_pay", str(pred["amount_safe_to_pay"]), gt["amount_safe_to_pay"],
             amounts_close(gt["amount_safe_to_pay"], pred["amount_safe_to_pay"])),
            ("affordability_status", pred["affordability_status"], gt["affordability_status"],
             pred["affordability_status"] == gt["affordability_status"]),
            ("recommended_payment_method", pred["recommended_payment_method"], gt["recommended_payment_method"],
             pred["recommended_payment_method"] == gt["recommended_payment_method"]),
            ("payment_plan", pred["payment_plan"], gt["payment_plan"],
             plans_match(gt["payment_plan"], pred["payment_plan"])),
            ("earliest_date_for_full_payment", pred["earliest_date_for_full_payment"],
             gt["earliest_date_for_full_payment"],
             pred["earliest_date_for_full_payment"] == gt["earliest_date_for_full_payment"]),
            ("spending_changes_needed", pred["spending_changes_needed"], gt["spending_changes_needed"],
             pred["spending_changes_needed"].strip() == gt["spending_changes_needed"].strip()),
            ("decision_explanation", pred["decision_explanation"], gt["decision_explanation"],
             pred["decision_explanation"].strip() == gt["decision_explanation"].strip()),
        ]

        for field, predicted, actual, ok in checks:
            field_hits[field] += ok
            mark = "OK  " if ok else "MISS"
            print(f"  [{mark}] {field:32s} pred={predicted!r:60s} gt={actual!r}")
            if not ok:
                row_ok = False
                mismatch_log.append((req.request_id, field, predicted, actual))

        row_exact += row_ok

    n = len(ds.sample_requests)
    print(f"\n{'='*100}\nFIELD SCORE TABLE ({n} samples)")
    for field, hits in field_hits.items():
        print(f"  {field:32s} {hits}/{n}")
    print(f"  {'ROW-EXACT (all 7 fields)':32s} {row_exact}/{n}")

    print(f"\n{len(mismatch_log)} total field-level mismatches. By field:")
    from collections import Counter
    by_field = Counter(m[1] for m in mismatch_log)
    for field, count in by_field.most_common():
        print(f"  {field}: {count}")


if __name__ == "__main__":
    main()
