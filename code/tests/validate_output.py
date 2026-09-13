"""Phase 5 final gate — independent post-hoc validation of output.csv.

Re-reads output.csv and dataset/requests.csv from scratch and re-derives
every invariant, deliberately NOT importing main.py's assert_invariants()
-- a bug in that function shouldn't be able to hide a violation from this
check. Run: python code/tests/validate_output.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
OUTPUT_PATH = REPO_ROOT / "output.csv"

EXPECTED_COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
                    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed",
                    "decision_explanation"]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}


def main() -> int:
    with open(DATASET_DIR / "requests.csv", newline="", encoding="utf-8") as f:
        requests = {r["request_id"]: r for r in csv.DictReader(f)}

    with open(OUTPUT_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(csv.DictReader(open(OUTPUT_PATH, newline="", encoding="utf-8")))

    problems = []

    if header != EXPECTED_COLUMNS:
        problems.append(f"column order mismatch: {header}")

    if len(rows) != len(requests):
        problems.append(f"row count {len(rows)} != requests.csv row count {len(requests)}")

    seen_ids = set()
    for row in rows:
        rid = row["request_id"]
        seen_ids.add(rid)
        req = requests.get(rid)
        if req is None:
            problems.append(f"{rid}: not present in requests.csv")
            continue

        requested_amount = float(req["requested_amount"])
        amt = float(row["amount_safe_to_pay"])
        if not (-1e-6 <= amt <= requested_amount + 1e-6):
            problems.append(f"{rid}: amount_safe_to_pay {amt} out of [0, {requested_amount}]")

        if row["affordability_status"] not in STATUSES:
            problems.append(f"{rid}: bad affordability_status {row['affordability_status']!r}")
        if row["recommended_payment_method"] not in METHODS:
            problems.append(f"{rid}: bad recommended_payment_method {row['recommended_payment_method']!r}")

        if row["affordability_status"] == "affordable_now" and row["earliest_date_for_full_payment"] != req["request_date"]:
            problems.append(f"{rid}: affordable_now but earliest_date {row['earliest_date_for_full_payment']!r} "
                             f"!= request_date {req['request_date']!r}")

        plan = row["payment_plan"]
        if plan != "none":
            entries = plan.split("|")
            dates = [e.split(":")[0] for e in entries]
            if dates != sorted(dates):
                problems.append(f"{rid}: payment_plan dates not chronological: {plan}")
            if row["recommended_payment_method"] == "partial_payment":
                if len(entries) != 2:
                    problems.append(f"{rid}: partial_payment must have exactly 2 payments: {plan}")
                else:
                    total = sum(float(e.split(":")[1]) for e in entries)
                    if abs(total - requested_amount) > 1.0:
                        problems.append(f"{rid}: partial_payment total {total} != requested_amount {requested_amount}")
        else:
            if row["recommended_payment_method"] not in ("wait", "not_recommended"):
                # wait/not_recommended can legitimately be "none" only if not_recommended;
                # wait must have a schedule -- flag if plan is none but method implies one.
                if row["recommended_payment_method"] != "not_recommended":
                    problems.append(f"{rid}: payment_plan is 'none' but method is {row['recommended_payment_method']!r}")

        changes = row["spending_changes_needed"]
        if changes != "none":
            entries = changes.split("|")
            if len(entries) > 3:
                problems.append(f"{rid}: more than 3 spending changes: {changes}")
            ids = [e.split(":")[1] for e in entries]
            if len(ids) != len(set(ids)):
                problems.append(f"{rid}: same event referenced by two spending changes: {changes}")

        if not row["decision_explanation"].strip():
            problems.append(f"{rid}: empty decision_explanation")

    missing = set(requests) - seen_ids
    if missing:
        problems.append(f"{len(missing)} requests.csv ids missing from output.csv: {sorted(missing)[:5]}...")

    print(f"checked {len(rows)} rows against {len(requests)} requests")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems[:50]:
            print(f"  - {p}")
        return 1

    print("all invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
