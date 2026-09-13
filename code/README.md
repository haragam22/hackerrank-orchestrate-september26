# Buy or Wait? — Solution README

Deterministic Python engine. No model runs in the scoring path — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the phase-by-phase
build log with test gates. This file is the practical "how to run it" and
"why does it do X" reference for the submission package.

## Setup and run

Requires Python 3.10+ (uses `list[str] | None` style type hints), stdlib
only — no dependencies to install.

```bash
python code/main.py
```

Reads `dataset/*.csv` (relative to the repo root), applies the frozen
`code/params.json` and the committed `code/cache/*.json`, and writes
`output.csv` at the repo root. Takes about 2 seconds for all 250 requests.
Every output row's invariants are asserted inline (`main.py`'s
`assert_invariants()`) — a violation raises immediately rather than writing
a silently-bad row.

Independent post-hoc check (re-derives every invariant from scratch,
without importing `main.py`):

```bash
python code/tests/validate_output.py
```

Unit tests, one file per layer, hand-built fixtures only:

```bash
python code/tests/test_ledger.py
python code/tests/test_safety.py
python code/tests/test_planner.py
python code/tests/test_explain.py
python code/tests/test_end_to_end.py    # sample_requests.csv regression + per-field score table
```

## File map

| File | Layer | Role |
|---|---|---|
| `io_loader.py` | — | CSV loaders, typed dataclasses, joins by id |
| `ledger.py` | L1 | Status filter, `linked_event_id` resolution, blank-amount fill, amendments, FX, history/future split |
| `recurrence.py` | L2 | Recurring-series classification + 90-day flow projection (calibrated) |
| `safety.py` | L3 | `is_safe()`, `amount_safe_to_pay()`, `earliest_date_for_full_payment()` |
| `planner.py` | L4 | Candidate generation, eligibility, spending-change subsets, ranking, status mapping |
| `explain.py` | L5 | 8 explanation templates, pure string formatting |
| `calibrate.py` | L6 | Dev-only grid search over `sample_requests.csv` → `params.json` |
| `enrich_images.py` | L0a | Offline image-amount extraction (see "Model access" below) |
| `enrich_messages.py` | L0b | Offline message classification (see "Model access" below) |
| `main.py` | — | Orchestrates L1→L5, writes `output.csv` |
| `params.json` | — | Frozen calibration output (Phase 3) |
| `cache/image_amounts.json` | — | 16 verified blank-amount resolutions |
| `cache/amendments.json` | — | Message-derived amendments (event-level + series-level) |

## Model access: what actually happened

**The committed `cache/*.json` files were NOT produced by a model call.**
Both intended model paths were blocked for reasons documented in full in
`log.txt` and summarized in `evaluation/usage_report.md`:

- **AgentRouter** (the online LLM the developer had a key for): its API
  rejects every request with `unauthorized_client_error`, a documented,
  currently-unresolved client-fingerprinting restriction affecting multiple
  unrelated tools and projects, not something fixable from this side without
  impersonating an allow-listed client.
- **Local Ollama**: `moondream` (vision) hallucinated badly on the first
  test image (real answer IDR 4,365,000; model answer "GBP 0.82") and
  `qwen3.5:9b` runs at ~2 tokens/second CPU-only on the development machine
  — impractical for 215 messages.

Given both were blocked, the values were produced directly instead:

- **`cache/image_amounts.json`**: all 16 images were read directly and the
  amount extracted by hand, with a `note` per entry explaining which figure
  was used and why (e.g. "balance due" vs. "amount already received" on a
  partially-paid rent receipt).
- **`cache/amendments.json`**: all 215 messages were read directly
  (including the Bahasa Indonesia ones), which surfaced that the dataset is
  generated from a small closed set of ~12 templates. That understanding is
  encoded as a deterministic regex classifier in `enrich_messages.py`. The
  38 messages with a populated `related_event_id` — the only ones that
  reach `ledger.py` — were individually hand-verified rather than left to
  the general patterns.

`enrich_images.py` and `enrich_messages.py`'s model-call paths are still
implemented and runnable (so the submission is reproducible end-to-end with
working model access — point `enrich_images.py` at a running Ollama with a
better vision model, or fix `enrich_messages.py`'s LLM path), they were
simply not the source of the committed caches this time.

## Calibration result

`code/calibrate.py` sweeps all 1,728 combinations of `irregular_mode ×
amount_estimator × min_occurrences × gap_tolerance × pending_debits ×
horizon_inclusive × irregular_min_occurrences` against the 25 labelled
samples (576 before `irregular_min_occurrences` was added — see point 4
below). This went through four iterations before landing on the frozen
rule in `ARCHITECTURE.md` §5.3a/§5.3b — worth recording honestly rather
than only showing the final number:

1. **Exact-match scoring** (the literal published metric) tied every
   configuration at 4/25 on `amount_safe_to_pay` — too coarse to see real
   differences between modes.
2. **Mean absolute percentage error (MAPE)** replaced it, but turned out to
   be dominated by 2–3 samples with tiny ground-truth denominators (e.g.
   gt=$83), so it stayed flat (~195–239%) regardless of mode and would have
   picked `none` by a hair while being blind to `flat_daily_burn` cutting
   *median* error and *total* dollar error substantially on the other 22
   samples.
3. **Median percentage error**, unconstrained, picked `flat_daily_burn` —
   until closer inspection showed it broke the cap on `request_01`, one of
   the 4 samples where ground truth says the full request is safe
   (`amount_safe_to_pay == requested_amount`). Root cause: `flat_daily_burn`
   divided each irregular series' total spend by *that series' own history
   span* (time since that series' first occurrence) rather than the user's
   whole observation window. Series with few occurrences that happened to
   start recently got an inflated per-day rate — e.g. user_01's
   "Supermarket basket" (3 occurrences spanning 51 days) ran at **46.70/day**
   under the old code vs **13.46/day** once divided by the true 177-day
   window, a >3x reduction from that one series alone; summed across every
   affected series this alone accounts for the bulk of request_01's
   over-projection. (Single-occurrence series were never the cause — they
   were already excluded by a pre-existing `len(events) < 2` guard in both
   the old and new code, an earlier version of this note said otherwise and
   was corrected after direct verification.) Fixed in `recurrence.py` by
   dividing every irregular series (and `per_category_monthly_frequency`'s
   per-category pool) by the user's whole observation window instead
   (shared across every series) — this cut the over-projection sharply but
   request_01 still didn't clear the cap under any tested
   `flat_daily_burn`/`median_gap_repeat`/`per_category_monthly_frequency`
   configuration.

That finding produced the **frozen selection rule** (`ARCHITECTURE.md`
§5.3a, written before the deciding sweep and not edited after): median
absolute percentage error on the 21 *binding* samples is the primary
metric, but any configuration that breaks the cap on any of the 4
*non-binding* samples (`request_01/09/12/16`) is disqualified outright — a
model that turns a genuinely-affordable request into a falsely-binding one
is a worse failure than a dollar-amount miss on a request that was already
binding. Under that constraint, **every** tested configuration of all three
irregular-spending mechanisms fails on at least one non-binding sample —
only `irregular_mode=none` ever passes. `none` wins by default, not by
tie-break.

4. **`IRREGULAR_MIN_OCCURRENCES`** (∈ {1, 2, 3}) was added afterward to test
   whether excluding series with too few occurrences from the irregular
   pool (a series' own history span otherwise dilutes the per-day rate
   unreliably) could close the gap. It helped, materially: at
   `irregular_min_occurrences=3`, `per_category_monthly_frequency`'s
   closest-to-passing configuration missed `request_01`'s cap by only
   **-$4,638** (down from `flat_daily_burn`'s original -$17,408, and better
   than `flat_daily_burn`'s own improved -$10,040); `median_gap_repeat`
   stayed the worst at -$25,256 (a full wipeout — `amount_safe_to_pay`
   driven to 0). None of the three cleared the constraint. A direct
   backsolve — what constant daily debit would make request_01's own
   trough land exactly at its cap — gives **12.95/day**, an order of
   magnitude below both `flat_daily_burn`'s fixed-window estimate
   (206.38/day) and the user's real historical last-90-days debit rate
   (685.79/day), reinforcing that the irregular-spend mechanism itself
   (not just its parameters) is still unidentified — see `ARCHITECTURE.md`
   §5.3's "Update" note.

A 25-fold leave-one-out check (select the winner on the other 24, score the
held-out sample), rerun on the full 1,728-config grid, confirms this is a
stable choice, not an artifact of these particular 25 rows: **`none` is
selected in 24 of 25 folds** (the 1 exception holds out `request_01`
itself, and the config it selects instead — confirmed when scored on that
held-out sample — fails exactly the cap `request_01` exists to test, i.e.
the constraint working as intended). Held-out (out-of-fold) median absolute
% error on the binding samples is 59.82%, vs. 51.07% in-sample on all 25 —
an ~8.75-point optimism gap, disclosed rather than hidden.

A separate, independently pre-registered **replacement gate**
(`ARCHITECTURE.md` §5.3b, dated the same day but a distinct pre-registration
from §5.3a) governs whether any new winner is allowed to replace what's
shipped: it must clear the §5.3a hard constraint, beat the incumbent's LOO
error by 5 points, and have its `IRREGULAR_MODE` selected in ≥20/25 LOO
folds. Run on the full 1,728-config grid: gate [1] PASS, [2] **FAIL** (LOO
error is unchanged at 59.82%, not <54.82%), [3] PASS — moot regardless,
since the winner already *is* the incumbent by config hash. `calibrate.py`
enforces this explicitly (prints PASS/FAIL per check, refuses to write
`params.json` on any FAIL unless the winner is the incumbent) rather than
leaving it to be eyeballed from output.

`params.json` freezes `irregular_mode=none, amount_estimator=last,
min_occurrences=2, gap_tolerance=2, pending_debits=true,
horizon_inclusive=true` (`_config_hash: 8223a5904767` — recompute and
compare before trusting an `output.csv` came from this config;
`irregular_min_occurrences` is stored but has no effect under `none`).
Runner-ups and the full leave-one-out record are in `params.json`'s
`_runner_ups` / `_leave_one_out` / `_request01_backsolve` fields.

| Field | Score on 25 samples |
|---|---|
| `amount_safe_to_pay` (exact, ±1) | 4/25 |
| `affordability_status` | 17/25 |
| `recommended_payment_method` | 19/25 |
| `payment_plan` | 15/25 |
| `spending_changes_needed` | 22/25 |
| `earliest_date_for_full_payment` | 8/25 |
| all fields exact (row-exact) | 4/25 |

**Read the 4/25 `amount_safe_to_pay` number correctly — don't let it look
better than it is.** All 4 exact matches are the 4 *non-binding* samples
(`request_01/09/12/16`, where ground truth is simply `amount_safe_to_pay ==
requested_amount` — no forecast needed to get these right). **On the 21
binding samples, where the forecast actually does work, the exact-match
count is 0/21.** The error there isn't random noise either: **20 of the 21
binding samples are over-projected** (we predict *more* safe-to-pay than
the reference model does) and only 1 is under-projected — a one-directional
systematic bias, consistent with `ARCHITECTURE.md` §5.3's original finding
that the reference forecaster projects irregular/variable spending forward
and `irregular_mode=none` does not. The two headline error numbers also
diverge sharply because of that same skew: **mean** signed error across the
21 binding samples is +233%, but the **median** is +47% — a handful of
samples with tiny ground-truth denominators (e.g. gt≈$83) inflate the mean
far past what a typical binding sample actually looks like, which is why
this document uses median throughout (see the metric-selection history
above) rather than reporting the mean number on its own. The frozen
selection rule's own headline metric, LOO held-out median absolute %
error, is **59.82%** — i.e. on a typical held-out binding sample, expect
`amount_safe_to_pay` to be off by roughly half, in the direction of
over-stating what's safe to spend. That is the honest current accuracy of
this system's central forecast, not the 4/25 table row in isolation.

The non-`amount` fields already score well because Layer 4's rules
(installment-count caps, ranking order, spending-change eligibility) are
implemented exactly per the published specification and don't depend on
Layer 2's still-unresolved irregular-spend projection.

Two side-questions worth recording: (1) of the 25 samples, only **1**
`decision_explanation` mismatch (`request_09`) has a correct
amount/status/method — and it isn't a fixable formatter bug, ground truth
itself uses two different phrasings ("leaves at least X available" vs.
"keeps the X minimum available") for the same template across
`request_01`/`request_16` vs. `request_09`. (2) 8 of the 25 samples predict
the *identical* `amount_safe_to_pay` under both `none` and
`flat_daily_burn` despite the user having 10–19 irregular series each —
confirmed mechanically for all 8: the extra projected burn genuinely moves
the 90-day trough, but never far enough to drop the raw safe-balance below
`requested_amount`, so `amount_safe_to_pay`'s own `clamp(0, requested_amount)`
ceiling absorbs the difference. Not a bug in either case.

## Known limitations

- **Series-level message amendments are not auto-applied.** A message like
  "your salary increased to X, effective DATE" with no `related_event_id`
  is recorded in `cache/amendments.json`'s `series_amendments` list for
  transparency, but doesn't change the Layer 2 projection. Wiring a
  free-text `target_series` label to a specific `(user_id, description)`
  series reliably needs more signal than these 88 messages give in
  isolation, and risks introducing new errors for a mechanism that Phase 3
  showed contributes little relative to the ledger/predicate core.
- **Irregular/one-off spending is not projected forward** (frozen
  `irregular_mode=none`) — see "Calibration result" above.
- One blank-amount receipt (`event_1700`, image_04) has its total partially
  cropped in the source screenshot; the clearly legible "Item Bill" subtotal
  was used rather than guessing at an off-screen delivery fee.
