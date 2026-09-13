"""Phase 5 — entry point. Deterministic pipeline: load -> L1 -> L2 -> L3 ->
L4 -> L5 -> write output.csv at the repo root. No required CLI args.

Uses code/params.json (frozen in Phase 3) and code/cache/*.json (built in
Phase 4) when present. If a cache is missing, ledger.py/enrich_*.py fall
back gracefully (blank amounts stay unresolved and are dropped, per
ledger.py's documented behavior) rather than calling a model here -- the
model path lives in enrich_images.py / enrich_messages.py, run separately
and offline, per ARCHITECTURE.md 4.0.

Run: python code/main.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import explain
import io_loader
import ledger
import planner
import recurrence
import safety

REPO_ROOT = Path(__file__).resolve().parent.parent
PARAMS_PATH = Path(__file__).resolve().parent / "params.json"
OUTPUT_PATH = REPO_ROOT / "output.csv"

COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
           "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation"]


def load_config() -> dict:
    if PARAMS_PATH.exists():
        raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        return {
            "params": {k: raw[k] for k in ("irregular_mode", "amount_estimator", "min_occurrences", "gap_tolerance")},
            "pending_debits": raw.get("pending_debits", True),
            "horizon_inclusive": raw.get("horizon_inclusive", True),
            "horizon_days": raw.get("horizon_days", 90),
        }
    return {"params": None, "pending_debits": True, "horizon_inclusive": True, "horizon_days": 90}


def fmt_num(x: float) -> str:
    if abs(x - round(x)) < 1e-6:
        return str(int(round(x)))
    return f"{x:.2f}"


def format_plan(schedule: list[tuple[dt.date, float]]) -> str:
    if not schedule:
        return "none"
    return "|".join(f"{d.isoformat()}:{fmt_num(a)}" for d, a in schedule)


def format_changes(changes: list) -> str:
    if not changes:
        return "none"
    parts = []
    for c in changes:
        if c.kind == "stop":
            parts.append(f"stop:{c.event_id}")
        else:
            parts.append(f"reduce_to:{c.event_id}:{fmt_num(c.new_amount)}")
    return "|".join(parts)


def assert_invariants(row: dict, req) -> None:
    amt = float(row["amount_safe_to_pay"])
    assert -1e-6 <= amt <= req.requested_amount + 1e-6, \
        f"{req.request_id}: amount_safe_to_pay {amt} out of [0, {req.requested_amount}]"

    if row["affordability_status"] == "affordable_now":
        assert row["earliest_date_for_full_payment"] == req.request_date.isoformat(), \
            f"{req.request_id}: affordable_now must have earliest_date == request_date"

    if row["recommended_payment_method"] == "partial_payment":
        assert row["affordability_status"] == "affordable_with_plan"
        parts = row["payment_plan"].split("|")
        assert len(parts) == 2, f"{req.request_id}: partial_payment must have exactly 2 payments"
        total = sum(float(p.split(":")[1]) for p in parts)
        assert abs(total - req.requested_amount) < 1.0, \
            f"{req.request_id}: partial_payment total {total} != requested_amount {req.requested_amount}"

    if row["payment_plan"] != "none":
        dates = [p.split(":")[0] for p in row["payment_plan"].split("|")]
        assert dates == sorted(dates), f"{req.request_id}: payment_plan dates not chronological"

    if row["spending_changes_needed"] != "none":
        entries = row["spending_changes_needed"].split("|")
        assert len(entries) <= 3, f"{req.request_id}: more than 3 spending changes"
        ids = [e.split(":")[1] for e in entries]
        assert len(ids) == len(set(ids)), f"{req.request_id}: same event referenced by two spending changes"


def build_row(ds: io_loader.Dataset, req: io_loader.Request, cfg: dict) -> dict:
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

    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": fmt_num(amount_safe),
        "affordability_status": plan.status,
        "recommended_payment_method": plan.method,
        "payment_plan": format_plan(plan.schedule),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": format_changes(plan.spending_changes),
        "decision_explanation": explanation,
    }
    assert_invariants(row, req)
    return row


def main() -> int:
    cfg = load_config()
    ds = io_loader.load()

    rows = [build_row(ds, req, cfg) for req in ds.requests]

    assert len(rows) == len(ds.requests), "output row count must equal requests.csv row count"

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {OUTPUT_PATH} with {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
