"""Layer 6 — calibration harness (Phase 3). Development-only, never in the
scoring path (ARCHITECTURE.md §4.6). Grids over the named Layer 2 params
plus the two Layer 3 conventions, runs the frozen L1/L3/L4 pipeline over
sample_requests.csv for every point, and writes the simplest configuration
within noise of the best to params.json.

Run: python code/calibrate.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import io_loader
import ledger
import planner
import recurrence
import safety

PARAMS_PATH = Path(__file__).resolve().parent / "params.json"

GRID = {
    "irregular_mode": ["none", "median_gap_repeat", "per_category_monthly_frequency", "flat_daily_burn"],
    "amount_estimator": ["last", "mean", "median", "trimmed_mean"],
    "min_occurrences": [2, 3, 4],
    "gap_tolerance": [2, 3, 5],
    "pending_debits": [True, False],
    "inclusive": [True, False],  # HORIZON: day 90 inclusive / exclusive
    "irregular_min_occurrences": [1, 2, 3],  # series shorter than this drop out of irregular projection entirely
}

# Simplicity rank, lowest = simplest. Used for "simplest within noise of
# best" selection (ARCHITECTURE.md §4.6) -- never pick the raw grid max.
# Order frozen in ARCHITECTURE.md §5.3a: none < flat_daily_burn <
# median_gap_repeat < per_category_monthly_frequency.
IRREGULAR_MODE_SIMPLICITY = {"none": 0, "flat_daily_burn": 1, "median_gap_repeat": 2,
                              "per_category_monthly_frequency": 3}

# request_ids where ground truth amount_safe_to_pay == requested_amount --
# "non-binding" samples the hard constraint in ARCHITECTURE.md §5.3a protects.
NON_BINDING_IDS = {"request_01", "request_09", "request_12", "request_16"}
AMOUNT_ESTIMATOR_SIMPLICITY = {"last": 0, "median": 1, "mean": 1, "trimmed_mean": 2}

# --- REPLACEMENT_GATE (frozen 2026-09-13, before this sweep) ---
# Replace the shipped config (params.json's current _config_hash) only if a
# candidate clears ALL three. Otherwise the incumbent ships unchanged and
# the script refuses to write params.json. Same principle as §5.3a: the
# threshold is on disk before the sweep runs, not chosen after seeing which
# config it would favor.
INCUMBENT_CONFIG_HASH = "8223a5904767"
INCUMBENT_LOO_MEDIAN_PCT_ERR = 59.82
REPLACEMENT_GATE = {
    "max_loo_median_pct_err": INCUMBENT_LOO_MEDIAN_PCT_ERR - 5.0,  # must beat incumbent's LOO error by 5pp
    "min_loo_fold_agreement": 20,  # same IRREGULAR_MODE selected in at least this many of 25 LOO folds
}


def simplicity_score(config: dict) -> tuple:
    return (
        IRREGULAR_MODE_SIMPLICITY[config["irregular_mode"]],
        AMOUNT_ESTIMATOR_SIMPLICITY[config["amount_estimator"]],
        config["min_occurrences"],
        config["gap_tolerance"],
        config.get("irregular_min_occurrences", 2),
    )


def format_plan(schedule) -> str:
    if not schedule:
        return "none"
    return "|".join(f"{d.isoformat()}:{amt:g}" for d, amt in schedule)


def parse_plan(s: str):
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


def score_config(ds, config: dict, requests: list | None = None) -> dict:
    """`requests` defaults to ds.sample_requests; leave-one-out (main() below)
    passes a subset so a fold's held-out sample never influences its own
    winner's selection."""
    params = {k: config[k] for k in ("irregular_mode", "amount_estimator", "min_occurrences", "gap_tolerance",
                                      "irregular_min_occurrences")}
    hits = dict(amount=0, status=0, method=0, plan=0, changes=0, earliest=0, row_exact=0)
    requests = ds.sample_requests if requests is None else requests
    n = len(requests)
    pct_errors = []          # binding samples only -- ARCHITECTURE.md §5.3a
    signed_pct_errors = []   # binding samples only, signed (pred-gt)/gt
    cap_pass = True          # hard constraint: every non-binding sample stays uncapped
    non_binding_shortfall = {}  # request_id -> max(0, requested_amount - amount_safe), only for non-binding rows seen

    for req in requests:
        profile = ds.profiles[req.user_id]
        options = ds.payment_options_by_request.get(req.request_id, [])
        lg = ledger.build_ledger(ds, req.user_id, req.request_date, pending_debits=config["pending_debits"])
        plan = planner.build_plan(req, profile, lg, options, params=params, inclusive=config["inclusive"])

        base_items_flows = recurrence.project_flow_items(lg, req.request_date, params=params)
        flows = recurrence.items_to_dict(base_items_flows)
        amount_safe = safety.amount_safe_to_pay(profile.current_available_balance, profile.minimum_balance_to_keep,
                                                 flows, req.request_date, req.requested_amount,
                                                 inclusive=config["inclusive"])
        earliest = safety.earliest_date_for_full_payment(profile.current_available_balance,
                                                           profile.minimum_balance_to_keep, flows,
                                                           req.request_date, req.requested_amount,
                                                           inclusive=config["inclusive"])
        earliest_str = earliest.isoformat() if earliest else ""

        gt = ds.sample_ground_truth[req.request_id]
        field_hits = []

        try:
            gt_amount = float(gt["amount_safe_to_pay"])
            amount_ok = abs(gt_amount - amount_safe) < 1.0
            if req.request_id in NON_BINDING_IDS:
                shortfall = max(0.0, gt_amount - amount_safe)  # how far below the cap this config landed
                non_binding_shortfall[req.request_id] = shortfall
                if not amount_ok:
                    cap_pass = False
            else:
                pct_errors.append(abs(gt_amount - amount_safe) / max(abs(gt_amount), 1.0))
                signed_pct_errors.append((amount_safe - gt_amount) / max(abs(gt_amount), 1.0))
        except (ValueError, TypeError):
            amount_ok = False
        field_hits.append(amount_ok)
        hits["amount"] += amount_ok

        earliest_ok = gt["earliest_date_for_full_payment"] == earliest_str
        field_hits.append(earliest_ok)
        hits["earliest"] += earliest_ok

        status_ok = gt["affordability_status"] == plan.status
        field_hits.append(status_ok)
        hits["status"] += status_ok

        method_ok = gt["recommended_payment_method"] == plan.method
        field_hits.append(method_ok)
        hits["method"] += method_ok

        plan_ok = plans_match(gt["payment_plan"], format_plan(plan.schedule))
        field_hits.append(plan_ok)
        hits["plan"] += plan_ok

        changes_str = "|".join(
            f"stop:{c.event_id}" if c.kind == "stop" else f"reduce_to:{c.event_id}:{c.new_amount:g}"
            for c in plan.spending_changes
        ) or "none"
        changes_ok = gt["spending_changes_needed"].strip() == changes_str.strip()
        field_hits.append(changes_ok)
        hits["changes"] += changes_ok

        hits["row_exact"] += all(field_hits)

    def median(vals):
        if not vals:
            return 999.0
        s = sorted(vals)
        m = len(s)
        return round(100 * (s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2), 2)

    hits["n"] = n
    hits["cap_pass"] = cap_pass
    hits["mape"] = round(100 * sum(pct_errors) / len(pct_errors), 2) if pct_errors else 999.0
    hits["median_pct_err"] = median(pct_errors)             # binding samples, absolute -- primary metric
    hits["median_signed_pct_err"] = median(signed_pct_errors)  # binding samples, signed -- diagnostic
    hits["non_binding_shortfall"] = non_binding_shortfall   # {request_id: $ short of the cap}, empty entries = passed
    return hits


def sweep(ds, grid: dict = GRID, requests: list | None = None) -> list[dict]:
    keys = list(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        config = dict(zip(keys, combo))
        hits = score_config(ds, config, requests)
        results.append({**config, **hits})
    return results


def select_winner(results: list[dict], noise_pct: float = 2.0) -> dict:
    """ARCHITECTURE.md §5.3a, frozen 2026-09-13: hard-disqualify any config
    that breaks a non-binding sample's cap, then take the simplest config
    within `noise_pct` points of the best median absolute percentage error
    on the 21 binding samples, tiebroken by IRREGULAR_MODE simplicity order.
    Falls back to the full (unfiltered) result set only if literally nothing
    passes the hard constraint, so the sweep never crashes -- but that
    situation should be reported, not silently accepted."""
    survivors = [r for r in results if r["cap_pass"]]
    pool = survivors if survivors else results
    best = min(r["median_pct_err"] for r in pool)
    contenders = [r for r in pool if r["median_pct_err"] <= best + noise_pct]
    contenders.sort(key=simplicity_score)
    return contenders[0]


LOO_CACHE_PATH = Path(__file__).resolve().parent / "cache" / "loo_folds_partial.json"


def leave_one_out(ds, grid: dict = GRID, held_out_subset: list | None = None) -> list[dict]:
    """ARCHITECTURE.md §5.3a step 4: for each of the 25 samples, select the
    winner on the other 24 under the frozen rule, then score that winner on
    the held-out sample alone. Returns one row per fold: which config won,
    and its held-out error. `held_out_subset` restricts which samples get
    held out (each fold's training set is still "all 25 minus that one",
    unaffected by chunking) -- lets a long LOO run be split into resumable
    chunks after the 1,728-config grid made a single run OOM-kill-prone."""
    targets = held_out_subset if held_out_subset is not None else ds.sample_requests
    folds = []
    for held_out in targets:
        train = [r for r in ds.sample_requests if r.request_id != held_out.request_id]
        train_results = sweep(ds, grid, requests=train)
        winner = select_winner(train_results)
        held_score = score_config(ds, winner, requests=[held_out])
        folds.append({
            "held_out": held_out.request_id,
            "selected_config": {
                "irregular_mode": winner["irregular_mode"], "amount_estimator": winner["amount_estimator"],
                "min_occurrences": winner["min_occurrences"], "gap_tolerance": winner["gap_tolerance"],
                "pending_debits": winner["pending_debits"], "inclusive": winner["inclusive"],
                "irregular_min_occurrences": winner["irregular_min_occurrences"],
            },
            "held_out_is_binding": held_out.request_id not in NON_BINDING_IDS,
            "held_out_cap_pass": held_score["cap_pass"],
            "held_out_pct_err": held_score["median_pct_err"],
        })
    return folds


def config_hash(config: dict) -> str:
    """sha256 of the 7 fields that fully determine a run, first 12 hex
    chars. Recompute and compare before trusting an output.csv came from a
    given params.json."""
    core = {
        "irregular_mode": config["irregular_mode"], "amount_estimator": config["amount_estimator"],
        "min_occurrences": config["min_occurrences"], "gap_tolerance": config["gap_tolerance"],
        "pending_debits": config["pending_debits"], "horizon_inclusive": config.get("inclusive", config.get("horizon_inclusive")),
        "horizon_days": config.get("horizon_days", 90),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


def request01_backsolve(ds) -> dict:
    """Diagnostic, not a selection input: request_01's ground truth says
    the full 25,256 is safe, i.e. the true 90-day trough never dips below
    minimum_balance_to_keep + requested_amount = 43,256. Holding every
    OTHER flow fixed (known_future + recurring_monthly, irregular_mode=none),
    bisect for the constant extra daily debit that would make our own
    trough land exactly there. Comparing that number to the fixed-window
    flat_daily_burn estimate and the user's real historical daily rate says
    whether the reference model projects roughly zero irregular spend for
    this user, or projects it somewhere the 90-day horizon doesn't reach."""
    req = next(r for r in ds.sample_requests if r.request_id == "request_01")
    profile = ds.profiles[req.user_id]
    lg = ledger.build_ledger(ds, req.user_id, req.request_date, pending_debits=True)
    base_params = {"irregular_mode": "none", "amount_estimator": "last",
                    "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 2}
    base_flows = recurrence.items_to_dict(recurrence.project_flow_items(lg, req.request_date, 90, base_params))
    target = profile.minimum_balance_to_keep + req.requested_amount

    def trough_with_extra_daily(rate: float) -> float:
        bal = profile.current_available_balance
        trough = bal
        for i in range(1, 91):
            d = req.request_date + dt.timedelta(days=i)
            bal += base_flows.get(d, 0.0) - rate
            trough = min(trough, bal)
        return trough

    lo, hi = 0.0, 5000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if trough_with_extra_daily(mid) > target:
            lo = mid
        else:
            hi = mid
    backsolved_rate = (lo + hi) / 2

    # historical comparison: same user's actual last-90-days debit total / 90
    horizon_start = req.request_date - dt.timedelta(days=90)
    recent = [e for e in lg.history if horizon_start <= e.event_date <= req.request_date]
    historical_rate = sum(e.amount for e in recent if e.direction == "debit") / 90

    # what the (window-days-fixed) flat_daily_burn config actually projects:
    # pick a day with no known_future/recurring event of its own so the diff
    # isolates the irregular contribution cleanly.
    fdb_params = {"irregular_mode": "flat_daily_burn", "amount_estimator": "last",
                  "min_occurrences": 2, "gap_tolerance": 2, "irregular_min_occurrences": 2}
    fdb_flows = recurrence.items_to_dict(recurrence.project_flow_items(lg, req.request_date, 90, fdb_params))
    probe_day = req.request_date + dt.timedelta(days=45)
    fdb_daily_rate = -(fdb_flows.get(probe_day, 0.0) - base_flows.get(probe_day, 0.0))  # positive = net debit/day

    return {
        "backsolved_daily_rate_for_exact_cap": round(backsolved_rate, 2),
        "flat_daily_burn_fixed_window_rate": round(fdb_daily_rate, 2),
        "historical_last_90d_daily_debit_rate": round(historical_rate, 2),
    }


def run_sweep_and_report(ds):
    """Steps 1-3 (sweep, per-mode diagnostics, backsolve) -- no LOO. Fast
    (~1min even at 1,728 configs), so safe to rerun standalone or at the
    start of --loo-finalize without re-paying the expensive LOO cost."""
    n_configs = len(list(itertools.product(*GRID.values())))
    print(f"Sweeping {n_configs} configurations over {len(ds.sample_requests)} samples "
          f"(21 binding, 4 non-binding: {sorted(NON_BINDING_IDS)})...")
    results = sweep(ds)
    results.sort(key=lambda r: (not r["cap_pass"], r["median_pct_err"], -r["amount"]))

    print("\ntop 10 by amount_safe_to_pay median ABSOLUTE percentage error on binding samples "
          "(cap_pass=False configs sort last regardless of score):")
    for r in results[:10]:
        print(f"{r['irregular_mode']:32s} {r['amount_estimator']:12s} "
              f"occ={r['min_occurrences']} gap={r['gap_tolerance']} pd={r['pending_debits']} "
              f"inc={r['inclusive']}  cap_pass={r['cap_pass']!s:5s} "
              f"median_abs_pct_err={r['median_pct_err']}% median_signed_pct_err={r['median_signed_pct_err']}% "
              f"amount_exact={r['amount']}/{r['n']} status={r['status']}/{r['n']} method={r['method']}/{r['n']} "
              f"plan={r['plan']}/{r['n']} earliest={r['earliest']}/{r['n']} changes={r['changes']}/{r['n']} "
              f"ROW_EXACT={r['row_exact']}/{r['n']}")

    print("\n--- step 2: best cap_pass=True config per IRREGULAR_MODE ---")
    for mode in ("none", "flat_daily_burn", "median_gap_repeat", "per_category_monthly_frequency"):
        mode_results = [r for r in results if r["irregular_mode"] == mode and r["cap_pass"]]
        if not mode_results:
            print(f"  {mode:32s} NO config passes the hard cap constraint")
            continue
        best = min(mode_results, key=lambda r: r["median_pct_err"])
        print(f"  {mode:32s} best median_abs_pct_err={best['median_pct_err']}% "
              f"median_signed_pct_err={best['median_signed_pct_err']}% "
              f"({best['amount_estimator']}, occ={best['min_occurrences']}, gap={best['gap_tolerance']}, "
              f"pd={best['pending_debits']}, inc={best['inclusive']}, "
              f"irr_occ={best['irregular_min_occurrences']})")

    print("\n--- per-mode disqualification detail: closest-to-passing config's non-binding shortfalls ---")
    print("(shortfall = ground_truth(requested_amount) - amount_safe; 0.0 = that row passed)")
    for mode in ("flat_daily_burn", "median_gap_repeat", "per_category_monthly_frequency"):
        mode_results = [r for r in results if r["irregular_mode"] == mode]
        if not mode_results:
            continue
        closest = min(mode_results, key=lambda r: sum(r["non_binding_shortfall"].values()))
        detail = ", ".join(f"{rid}=-{amt:,.0f}" for rid, amt in sorted(closest["non_binding_shortfall"].items()))
        print(f"  {mode:32s} {detail}  "
              f"[{closest['amount_estimator']}, occ={closest['min_occurrences']}, gap={closest['gap_tolerance']}, "
              f"pd={closest['pending_debits']}, inc={closest['inclusive']}, "
              f"irr_occ={closest['irregular_min_occurrences']}]")

    print("\n--- request_01 backsolve ---")
    backsolve = request01_backsolve(ds)
    print(f"  daily debit rate that makes request_01's trough land exactly at "
          f"minimum_balance_to_keep+requested_amount: {backsolve['backsolved_daily_rate_for_exact_cap']}/day")
    print(f"  flat_daily_burn (window-days-fixed, irr_occ=2) actually projects: "
          f"{backsolve['flat_daily_burn_fixed_window_rate']}/day")
    print(f"  user_01's real historical last-90-days daily debit rate: "
          f"{backsolve['historical_last_90d_daily_debit_rate']}/day")

    winner = select_winner(results)
    print("\nSELECTED (frozen rule, ARCHITECTURE.md §5.3a):")
    print(json.dumps(winner, indent=2))

    runner_ups = sorted([r for r in results if r != winner and r["cap_pass"]],
                         key=lambda r: r["median_pct_err"])[:3]
    print("\nrunner-ups (record these in the README per ARCHITECTURE §4.6):")
    for r in runner_ups:
        print(json.dumps(r, indent=2))

    return results, winner, runner_ups, backsolve


def main():
    argv = sys.argv[1:]
    skip_loo = "--skip-loo" in argv
    ds = io_loader.load()

    if "--loo-chunk" in argv:
        i = argv.index("--loo-chunk")
        start, end = int(argv[i + 1]), int(argv[i + 2])
        subset = ds.sample_requests[start:end]
        print(f"Running LOO for held-out samples [{start}:{end}) "
              f"({[r.request_id for r in subset]})...")
        folds = leave_one_out(ds, held_out_subset=subset)
        existing = []
        if LOO_CACHE_PATH.exists():
            existing = json.loads(LOO_CACHE_PATH.read_text(encoding="utf-8"))
        by_id = {f["held_out"]: f for f in existing}
        for f in folds:
            by_id[f["held_out"]] = f
        LOO_CACHE_PATH.write_text(json.dumps(list(by_id.values()), indent=2), encoding="utf-8")
        print(f"wrote {len(by_id)}/25 folds to {LOO_CACHE_PATH}")
        return 0

    if "--loo-finalize" in argv:
        if not LOO_CACHE_PATH.exists():
            print(f"no {LOO_CACHE_PATH} -- run --loo-chunk first"); return 1
        folds = json.loads(LOO_CACHE_PATH.read_text(encoding="utf-8"))
        have = {f["held_out"] for f in folds}
        want = {r.request_id for r in ds.sample_requests}
        if have != want:
            print(f"incomplete: have {len(have)}/25 folds, missing {sorted(want - have)}"); return 1
        results, winner, runner_ups, backsolve = run_sweep_and_report(ds)
        return finalize_and_write(ds, results, winner, runner_ups, backsolve, folds)

    results, winner, runner_ups, backsolve = run_sweep_and_report(ds)

    if skip_loo:
        print("\n--skip-loo passed: stopping after the sweep + diagnostics. "
              "params.json NOT written (REPLACEMENT_GATE needs LOO numbers -- run without --skip-loo to decide).")
        return 0

    print("\n--- step 4: leave-one-out ---")
    folds = leave_one_out(ds)
    return finalize_and_write(ds, results, winner, runner_ups, backsolve, folds)


def finalize_and_write(ds, results, winner, runner_ups, backsolve, folds) -> int:
    """Step 4 reporting + REPLACEMENT_GATE + params.json write, shared by
    the single-shot path and --loo-finalize (chunked LOO)."""
    from collections import Counter
    config_counts = Counter(json.dumps(f["selected_config"], sort_keys=True) for f in folds)
    print("selected-config frequency across 25 folds:")
    for cfg_json, count in config_counts.most_common():
        print(f"  {count}/25  {cfg_json}")
    binding_errs = [f["held_out_pct_err"] for f in folds if f["held_out_is_binding"]]
    cap_failures = [f["held_out"] for f in folds if not f["held_out_is_binding"] and not f["held_out_cap_pass"]]
    loo_median = sorted(binding_errs)[len(binding_errs) // 2] if binding_errs else None
    top_mode, top_mode_count = config_counts.most_common(1)[0] if config_counts else (None, 0)
    top_mode_irregular = json.loads(top_mode)["irregular_mode"] if top_mode else None
    fold_agreement = sum(count for cfg_json, count in config_counts.items()
                          if json.loads(cfg_json)["irregular_mode"] == top_mode_irregular)
    print(f"\nLOO median abs pct err on held-out binding samples: {loo_median}%")
    print(f"LOO all-25 median abs pct err (for comparison): {winner['median_pct_err']}%")
    print(f"non-binding held-out folds that failed cap_pass: {cap_failures or 'none'}")
    print(f"fold agreement on IRREGULAR_MODE={top_mode_irregular}: {fold_agreement}/25")

    print("\n--- REPLACEMENT_GATE (frozen 2026-09-13, before this sweep) ---")
    winner_hash = config_hash(winner)
    is_incumbent = winner_hash == INCUMBENT_CONFIG_HASH
    gate_1 = winner["cap_pass"]
    gate_2 = (loo_median is not None) and (loo_median < REPLACEMENT_GATE["max_loo_median_pct_err"])
    gate_3 = fold_agreement >= REPLACEMENT_GATE["min_loo_fold_agreement"]
    print(f"  [1] clears §5.3a hard constraint (non-binding rows uncapped): "
          f"{'PASS' if gate_1 else 'FAIL'}")
    print(f"  [2] LOO held-out median abs pct err < {REPLACEMENT_GATE['max_loo_median_pct_err']}% "
          f"(incumbent {INCUMBENT_LOO_MEDIAN_PCT_ERR}% - 5pp): "
          f"{'PASS' if gate_2 else 'FAIL'} (got {loo_median}%)")
    print(f"  [3] same IRREGULAR_MODE selected in >= {REPLACEMENT_GATE['min_loo_fold_agreement']}/25 LOO folds: "
          f"{'PASS' if gate_3 else 'FAIL'} (got {fold_agreement}/25 for {top_mode_irregular})")

    if is_incumbent:
        print(f"\n  Winner IS the incumbent (hash {winner_hash}) -- writing params.json unchanged.")
    elif gate_1 and gate_2 and gate_3:
        print(f"\n  ALL THREE GATES PASS -- replacing incumbent (hash {INCUMBENT_CONFIG_HASH}) "
              f"with new config (hash {winner_hash}).")
    else:
        print(f"\n  GATE FAILED -- refusing to write params.json. Incumbent (hash {INCUMBENT_CONFIG_HASH}) ships unchanged.")
        return 1

    params_out = {
        "irregular_mode": winner["irregular_mode"],
        "amount_estimator": winner["amount_estimator"],
        "min_occurrences": winner["min_occurrences"],
        "gap_tolerance": winner["gap_tolerance"],
        "pending_debits": winner["pending_debits"],
        "horizon_inclusive": winner["inclusive"],
        "horizon_days": 90,
        "irregular_min_occurrences": winner["irregular_min_occurrences"],
        "_config_hash": winner_hash,
        "_config_hash_note": ("sha256(json.dumps({irregular_mode,amount_estimator,min_occurrences,gap_tolerance,"
                               "pending_debits,horizon_inclusive,horizon_days}, sort_keys=True))[:12] -- recompute "
                               "and compare before trusting an output.csv came from this config."),
        "_calibration_scores_on_25_samples": {k: v for k, v in winner.items()
                                               if k not in ("irregular_mode", "amount_estimator",
                                                             "min_occurrences", "gap_tolerance",
                                                             "pending_debits", "inclusive",
                                                             "irregular_min_occurrences")},
        "_runner_ups": runner_ups,
        "_leave_one_out": {
            "config_frequency": {cfg_json: count for cfg_json, count in config_counts.most_common()},
            "loo_median_abs_pct_err_binding": loo_median,
            "all25_median_abs_pct_err_binding": winner["median_pct_err"],
            "non_binding_cap_failures": cap_failures,
            "fold_agreement_on_top_irregular_mode": fold_agreement,
        },
        "_request01_backsolve": backsolve,
    }
    PARAMS_PATH.write_text(json.dumps(params_out, indent=2), encoding="utf-8")
    print(f"\nwrote {PARAMS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
