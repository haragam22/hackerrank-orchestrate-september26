# IMPLEMENTATION.md — Phase-by-phase build plan

Companion to [`ARCHITECTURE.md`](./ARCHITECTURE.md) (design/why) and
[`CLAUDE.md`](./CLAUDE.md) (dev contract/rules). This file is the execution
order: what to build in each phase, what "done" means, and the exact tests to
run before moving to the next phase. Follow it top to bottom — later phases
assume earlier phases are green.

Do not skip a phase's test gate to "come back later." A wrong Layer 1 ledger
silently corrupts every later layer, and it's cheap to catch at the source
(ARCHITECTURE §6 debuggability note).

---

## Phase 0 — Scaffold & data access

**Goal:** load every dataset file into typed, joinable Python structures. No
business logic yet.

**Build**
- `code/io_loader.py`: CSV readers for all 8 dataset files → list of
  namedtuples/dataclasses per file, indexed by `user_id` / `request_id` /
  `event_id` where relevant.
- Confirm image path resolution: `dataset/media/images/<image_id>.png`.
- A tiny CLI check script (or `if __name__ == "__main__"` block in
  `io_loader.py`) that prints row counts per file.

**Test before moving on**
- Row counts match ARCHITECTURE §1 exactly: `requests.csv`=250,
  `sample_requests.csv`=25, `financial_profiles.csv`=275,
  `financial_events.csv`=25342, `request_payment_options.csv`=790,
  `messages.csv`=215, `images.csv`=16, `exchange_rates.csv`=134. A mismatch
  means the loader is dropping/misparsing rows — stop and fix before Phase 1.
- Zero user-id overlap between `requests.csv` and `sample_requests.csv`
  (confirms no accidental leakage assumption).
- Every `image_id` in `images.csv` resolves to an existing PNG on disk.
- Spot-join one request end-to-end by hand (profile + events + payment
  options + messages for one `request_id`) and eyeball it.

---

## Phase 1 — Ledger reconstruction + safety predicate (Layers 1 & 3)

**Goal:** prove the ledger and the 90-day forecaster are correct before any
parameter fitting. `IRREGULAR_MODE = none` (i.e., irregular/one-off series are
not projected forward at all yet — recurring-monthly series only).

**Build**
- `code/ledger.py` (L1): status filter (drop `cancelled`/`failed`/`unrealized`
  and `direction == non_cash`), `linked_event_id` chain resolution, blank-amount
  fill from `cache/image_amounts.json` (stub the cache empty for now — Phase 4
  populates it; use raw amount or skip rows needing it for now, flag as TODO),
  amendment application from `cache/amendments.json` (same stub situation), FX
  normalization via `exchange_rates.csv` (exact date match, else latest rate
  on/before), split into `history` vs `known_future`.
- `code/safety.py` (L3): `is_safe(profile, flows, schedule, spending_changes)`
  exactly as specified in ARCHITECTURE §4.3, plus `amount_safe_to_pay` and
  `earliest_date_for_full_payment` derivations.
- `code/recurrence.py` (L2) minimal version: classify `(user_id, description)`
  series as `recurring_monthly` (gap tolerance ±3 days, ≥3 occurrences — pick
  provisional defaults, real tuning is Phase 3) or `irregular`; irregular
  series contribute nothing to the forward flow yet (`IRREGULAR_MODE=none`).

**Test before moving on**
- Unit tests (`tests/test_ledger.py`) with hand-built fixtures:
  - a `linked_event_id` chain (authorization→settlement→refund) collapses to
    the right single net effect
  - a `cancelled`/`failed`/`unrealized` row never appears in the cleaned ledger
  - a pending debit is retained; a pending credit is not
  - a foreign-currency event picks the correct dated FX row, and falls back to
    the latest rate on/before when the exact date is missing (log the
    substitution)
- Unit tests (`tests/test_safety.py`) with hand-built fixtures:
  - flat balance, no flows → `is_safe` true iff payment leaves balance
    `>= minimum_balance_to_keep`
  - a flow that dips below minimum on day 40 → `is_safe` false regardless of
    day-90 balance (catches anyone tempted to only check the final balance)
  - `amount_safe_to_pay` matches a manually computed value on a synthetic
    3-flow scenario
  - `earliest_date_for_full_payment` returns `None`/empty when no day in
    `[request_date, +90]` is safe
- Run the partial engine (L1+L2-stub+L3 only, no L4/L5 yet) over
  `sample_requests.csv` and compare only `amount_safe_to_pay` and
  `earliest_date_for_full_payment` against sample ground truth. Expect low
  accuracy here (ARCHITECTURE §5.3: ~4/25 exact with no irregular projection)
  — that's the expected floor, not a bug. What must NOT happen: crashes, NaNs,
  negative amounts, or a violated invariant (`0 <= amount_safe_to_pay <=
  requested_amount`) on any of the 25 rows.

---

## Phase 2 — Candidate generation, eligibility, ranking (Layer 4)

**Goal:** get every non-`amount_safe_to_pay`-dependent field (status, method,
plan shape, spending-change validity) working against the rules that are
already 100% confirmed, independent of the still-unresolved Layer 2 accuracy.

**Build**
- `code/planner.py` (L4): candidate generation (`full_payment`, each
  installment option expanded from `request_payment_options.csv`,
  `partial_payment`, `wait`), eligibility filters (payment-method
  preference, `max_installment_months` as a payment-*count* cap, partial's
  `allows_partial_payment` + amount-range + deadline checks, wait's
  full-payment-acceptance + non-empty-earliest-date check, deadline
  completion), spending-change subset enumeration (≤3, eligibility = AND of
  `flexibility` and category permission, `reduce_to` = `minimum_allowed_amount`
  always, stop/reduce mutually exclusive per event, size-0 tried first), the
  6-way ranking, and the status-mapping table (ARCHITECTURE §4.4).

**Test before moving on**
- Unit tests (`tests/test_planner.py`) with hand-built profiles/options:
  - `max_installment_months=7` excludes an 18-payment option and selects a
    3-payment option (mirrors the confirmed request_02 rule)
  - installment dates = `first_payment_date + k * payment_frequency_days`,
    exact, for a synthetic option
  - a partial-payment candidate is rejected when `allows_partial_payment` is
    false, or when the completion date would fall after
    `desired_completion_date`
  - a spending-change subset never proposes both `stop` and `reduce_to` on the
    same `event_id`
  - every `reduce_to` value equals the fixture's `minimum_allowed_amount`,
    never a computed number
  - ranking tie-break order is exercised with ≥2 synthetic safe candidates
    that differ on each of the 6 criteria in turn (6 targeted fixtures, one
    per tie-break level)
  - status-mapping table: one fixture per row (affordable_now /
    affordable_with_plan / affordable_later / not_affordable) producing the
    exact expected `(status, method, plan-shape)` triple
- Run full L1→L4 (L5 still stubbed to raw field dump, no prose yet) over
  `sample_requests.csv`. Score `affordability_status`,
  `recommended_payment_method`, `payment_plan` shape (not exact amounts yet,
  since L2 is still unfit), and `spending_changes_needed` against sample
  ground truth. This is the first point a real per-field score table is
  meaningful — record it.

---

## Phase 3 — Calibration harness + Layer 2 sweep

**Goal:** close ARCHITECTURE §5.3's open problem — irregular/variable spending
projection — which is what the remaining `amount_safe_to_pay` /
`earliest_date_for_full_payment` / downstream-status errors trace back to.

**Build**
- `code/calibrate.py` (L6, dev-only, never in the scoring path): grid search
  over the ARCHITECTURE §5.2 parameter table
  (`IRREGULAR_MODE`, `AMOUNT_ESTIMATOR`, `MIN_OCCURRENCES`, `GAP_TOLERANCE`,
  `PENDING_DEBITS`, `HORIZON`), each point run through the frozen L1/L3/L4/L5
  pipeline over `sample_requests.csv`, producing the per-field score table
  from ARCHITECTURE §4.6.
- Extend `code/recurrence.py` to implement each named `IRREGULAR_MODE` value.
- Selection: pick the simplest config within noise of the best (not the
  max) — ARCHITECTURE §4.6. Write the winner to `params.json`. Record the
  runner-up configs and the reasoning in the README (not just in this repo —
  the submission README needs this per ARCHITECTURE §7).

**Test before moving on**
- The grid actually runs to completion in seconds-to-low-minutes over all
  combinations (a few hundred × 25 rows) — if it's slow, something in L1–L4
  is doing wasted recomputation per grid point; fix before trusting the sweep.
- The chosen config's per-field table is recorded, plus at least 2 runner-up
  configs, in the README calibration section.
- Regression sanity check from ARCHITECTURE §2.1: the majority of predicted
  `earliest_date_for_full_payment` values land on paydays (commonly the
  15th) — if the winning config produces mostly non-payday dates, that's a
  sign of overfitting to noise in the 25-row grid rather than the real
  mechanism; prefer the simpler runner-up.
- `amount_safe_to_pay` exact-match rate on samples materially beats the ~4/25
  floor from Phase 1 (ARCHITECTURE §5.3 target: aim toward ~20/25, but the
  selection rule above still governs — don't chase the single top grid point
  if it's not meaningfully ahead of simpler ones).
- Re-run every Phase 1 and Phase 2 unit test — the sweep must not have broken
  anything in L1/L3/L4 (it should only be touching `recurrence.py` and the two
  named L3 conventions).

---

## Phase 4 — Offline enrichment (Layer 0) + explanation formatter (Layer 5)

**Goal:** the two remaining pieces that don't affect the core forecast logic
but are required for a complete, accurate `output.csv`.

**Build**
- `code/enrich_images.py` (L0a, dev-only): for each of the 16 `images.csv`
  rows, resolve the blank-amount event via `related_event_id`, call the local
  VLM with the closed JSON schema from ARCHITECTURE §4.0a, write
  `cache/image_amounts.json`.
- `code/enrich_messages.py` (L0b, dev-only): batch the 215 messages (~20/call),
  forced JSON array output per the schema in ARCHITECTURE §4.0b, closed
  `action` set, default-to-`ignore` prompt discipline, write
  `cache/amendments.json`.
- Wire both caches into `code/ledger.py` for real (Phase 1 used stubs).
- `code/explain.py` (L5): the 7 templates from ARCHITECTURE §4.5, pure string
  formatting — currency-code prefix, thousands separators, conditional two
  decimals, `D Month YYYY` dates. Resolve the template-5-vs-6 open item
  empirically against the seven `not_affordable` samples (ARCHITECTURE §4.5
  open item) before freezing the discriminator rule.

**Test before moving on**
- **Mandatory manual verification**: open all 16 PNGs by hand and check every
  extracted `amount`/`currency` against `cache/image_amounts.json` before
  committing it. Log which ones needed the category-median fallback.
- **Mandatory manual review**: read every message classified as anything
  other than `ignore` in `cache/amendments.json` (expect ~30–50 of 215) and
  confirm against the actual message text — hedged language must never have
  produced a non-`ignore` action.
- Re-run the Phase 3 calibration table with the real caches wired in (not the
  Phase 1 stubs) — confirm the 4 large post-request-date blank-amount events
  (rent/telecom/grocery/hospital per ARCHITECTURE §1) now flow through
  correctly, and re-check whether the frozen `params.json` point still holds
  or needs a quick re-sweep now that caches are real.
- `tests/test_explain.py`: one fixture per template producing the exact
  expected string (formatting is exact-match sensitive — separators, decimal
  rules, date style).
- Confirm the template-5-vs-6 discriminator resolves all 7 `not_affordable`
  samples correctly before freezing it.

---

## Phase 5 — Full assembly, invariants, submission artifacts

**Goal:** one deterministic run over the real 250-row `requests.csv`, wired
end to end, producing a submission-ready package.

**Build**
- `code/main.py`: load → L1 → L2 → L3 → L4 → L5 → write `output.csv` at repo
  root, using `params.json` + `cache/*.json`. No required CLI args. Runs the
  live L0 model path only as a fallback when a cache file is absent, per
  ARCHITECTURE §4.0.
- `assert_invariants()` (see CLAUDE.md) called on every output row inside
  `main.py` itself, not just in tests — a violated invariant should fail the
  run loudly, never write a silently-bad `output.csv`.
- `evaluation/usage_report.md`: filled in from the real Phase 4 enrichment
  run's logs (provider, model name, call count, input/output tokens, totals,
  per-request averages, estimated cost; per-model and overall totals since
  L0a and L0b likely use different models).
- README: measured facts (ARCHITECTURE §2), the calibration grid + selected
  point + runner-ups (§5.2/§4.6), and a plain-language justification for every
  fitted parameter.

**Test before moving on (this is the final gate — nothing ships without it)**
- `python code/main.py` runs clean from a fresh checkout with no manual steps
  beyond what the README documents.
- `output.csv` row count == `requests.csv` row count (250), column order
  exactly `request_id,amount_safe_to_pay,affordability_status,
  recommended_payment_method,payment_plan,earliest_date_for_full_payment,
  spending_changes_needed,decision_explanation`.
- Every invariant in CLAUDE.md holds for all 250 rows (assert in-code, then
  double check with an independent post-hoc validation script that re-reads
  `output.csv` and re-derives each check from scratch, so a bug in
  `assert_invariants()` itself can't hide a violation).
- `tests/test_end_to_end.py` passes against `sample_requests.csv` with the
  frozen `params.json` — this is the last time samples are used, purely as a
  regression check, not for further tuning.
- No secrets/API keys anywhere in `code/`, `params.json`, or `cache/*.json`.
- `evaluation/usage_report.md` reflects the actual final enrichment run (not
  an earlier dev run) — re-generate it if L0 was re-run after this doc was
  drafted.
- Package `code.zip` per ARCHITECTURE §7 table: engine + `params.json` +
  `cache/*.json` + L0a/L0b prompts + README + `evaluation/`.

---

## Cross-phase rule

After every phase, log the phase's outcome (per-field score table where
applicable) in `log.txt` per AGENTS.md §5, and re-run all prior phases' unit
tests before starting the next phase — this build order is a dependency
chain, not a checklist to do in parallel.
