"""End-to-end regression against sample_requests.csv.

Phase 1 use: only amount_safe_to_pay / earliest_date_for_full_payment are
meaningful (L4/L5 don't exist yet) — this just proves L1+L2+L3 don't crash
or violate invariants, and gives a first accuracy baseline. Phase 5 use:
the same script, full pipeline, as the final regression gate before
freezing params.json. Run: python code/tests/test_end_to_end.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io_loader
import ledger
import main as main_module
import planner
import recurrence
import safety


def run_partial(ds, cfg) -> list[dict]:
    """L1+L2+L3 only (Phase 1 state) -- no L4 candidate/ranking, no L5 text.
    Uses params.json's frozen config via `cfg` (main.load_config()) -- a
    caller that skips this silently re-tests recurrence.py's module
    defaults instead of what output.csv was actually built with."""
    rows = []
    for req in ds.sample_requests:
        profile = ds.profiles[req.user_id]
        lg = ledger.build_ledger(ds, req.user_id, req.request_date, pending_debits=cfg["pending_debits"])
        flows = recurrence.project_flows(lg, req.request_date, cfg["horizon_days"], cfg["params"])
        safe = safety.amount_safe_to_pay(
            profile.current_available_balance, profile.minimum_balance_to_keep,
            flows, req.request_date, req.requested_amount,
            cfg["horizon_days"], cfg["horizon_inclusive"],
        )
        earliest = safety.earliest_date_for_full_payment(
            profile.current_available_balance, profile.minimum_balance_to_keep,
            flows, req.request_date, req.requested_amount,
            cfg["horizon_days"], cfg["horizon_inclusive"],
        )
        rows.append({
            "request_id": req.request_id,
            "amount_safe_to_pay": safe,
            "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        })
    return rows


def format_plan(schedule: list[tuple]) -> str:
    if not schedule:
        return "none"
    return "|".join(f"{d.isoformat()}:{amt:g}" for d, amt in schedule)


def parse_plan(s: str) -> list[tuple]:
    s = (s or "").strip()
    if not s or s == "none":
        return []
    out = []
    for part in s.split("|"):
        d, amt = part.split(":")
        out.append((d, float(amt)))
    return out


def plans_match(a: str, b: str, tol: float = 1.0) -> bool:
    pa, pb = parse_plan(a), parse_plan(b)
    if len(pa) != len(pb):
        return False
    return all(da == db and abs(aa - ab) < tol for (da, aa), (db, ab) in zip(pa, pb))


def run_full(ds, cfg) -> list[dict]:
    """L1+L2+L3+L4 (Phase 2 state) -- no L5 text explanation yet."""
    rows = []
    for req in ds.sample_requests:
        profile = ds.profiles[req.user_id]
        options = ds.payment_options_by_request.get(req.request_id, [])
        lg = ledger.build_ledger(ds, req.user_id, req.request_date, pending_debits=cfg["pending_debits"])
        plan = planner.build_plan(req, profile, lg, options, params=cfg["params"],
                                   horizon_days=cfg["horizon_days"], inclusive=cfg["horizon_inclusive"])
        rows.append({
            "request_id": req.request_id,
            "affordability_status": plan.status,
            "recommended_payment_method": plan.method,
            "payment_plan": format_plan(plan.schedule),
            "spending_changes_needed": (
                "|".join(
                    f"stop:{c.event_id}" if c.kind == "stop" else f"reduce_to:{c.event_id}:{c.new_amount:g}"
                    for c in plan.spending_changes
                ) or "none"
            ),
        })
    return rows


def run_full_check(ds, cfg) -> int:
    rows = {r["request_id"]: r for r in run_full(ds, cfg)}
    n = len(ds.sample_requests)
    hits = {"affordability_status": 0, "recommended_payment_method": 0, "payment_plan": 0, "spending_changes_needed": 0}
    for req in ds.sample_requests:
        gt = ds.sample_ground_truth[req.request_id]
        row = rows[req.request_id]
        for field in ("affordability_status", "recommended_payment_method"):
            if gt[field] == row[field]:
                hits[field] += 1
        if plans_match(gt["payment_plan"], row["payment_plan"]):
            hits["payment_plan"] += 1
        if gt["spending_changes_needed"].strip() == row["spending_changes_needed"].strip():
            hits["spending_changes_needed"] += 1
    print("\n--- Layer 4 (candidates/eligibility/ranking) sample scores ---")
    for field, count in hits.items():
        print(f"{field}: {count}/{n}")
    return 0


def check_invariants(req, row) -> list[str]:
    problems = []
    if not (0 - 1e-6 <= row["amount_safe_to_pay"] <= req.requested_amount + 1e-6):
        problems.append(f"amount_safe_to_pay {row['amount_safe_to_pay']} out of [0, {req.requested_amount}]")
    return problems


def main():
    ds = io_loader.load()
    cfg = main_module.load_config()
    rows = run_partial(ds, cfg)
    by_id = {r["request_id"]: r for r in rows}

    exact_amount = 0
    exact_date = 0
    problems_total = 0
    for req in ds.sample_requests:
        gt = ds.sample_ground_truth[req.request_id]
        row = by_id[req.request_id]
        problems = check_invariants(req, row)
        problems_total += len(problems)
        for p in problems:
            print(f"  INVARIANT VIOLATION {req.request_id}: {p}")

        try:
            gt_amount = float(gt["amount_safe_to_pay"])
            if abs(gt_amount - row["amount_safe_to_pay"]) < 1.0:
                exact_amount += 1
        except (ValueError, TypeError):
            pass
        if gt["earliest_date_for_full_payment"] == row["earliest_date_for_full_payment"]:
            exact_date += 1

    n = len(ds.sample_requests)
    print(f"\namount_safe_to_pay exact (±1): {exact_amount}/{n}")
    print(f"earliest_date_for_full_payment exact: {exact_date}/{n}")
    print(f"invariant violations: {problems_total}")
    run_full_check(ds, cfg)
    return 1 if problems_total else 0


if __name__ == "__main__":
    sys.exit(main())
