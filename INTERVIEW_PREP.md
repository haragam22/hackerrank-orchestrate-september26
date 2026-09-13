# Judge interview prep — Buy or Wait?

Per HackerRank feedback: ground claims in one concrete walkthrough, pre-pick
three defendable details with evidence, define custom terms in plain
language, name one monitoring signal and the action on drift.

## The one walkthrough — rehearse this

**request_150** (from the scored 250, not a labelled sample — picked
because it exercises every layer and because it's the row where the
independent audit caught a real bug, which is a stronger story than a
clean pass).

1. **L1 (ledger.py)**: reads `financial_events.csv` for this user, filters
   `cancelled`/`failed`/`unrealized`/non-cash rows, resolves the
   `linked_event_id` chain (drops a cancelled/pending predecessor once its
   settled successor exists, keeps refunds as credits), converts any
   foreign-currency events via the exact-date `exchange_rates.csv` row,
   splits into `history` (≤ request_date) and `known_future` (pending/
   scheduled, > request_date). Output: one clean per-user event list.
2. **L2 (recurrence.py)**: classifies each `(user_id, description)` series
   as `recurring_monthly` (≥2 occurrences, ~30-day gaps) or `irregular`
   using `classify_series()`. Under the frozen `irregular_mode=none`,
   only `recurring_monthly` series and real `known_future` rows project
   forward 90 days; irregular one-offs contribute nothing beyond history.
   Output: a `{date: net_signed_flow}` map.
3. **L3 (safety.py)**: walks the 90-day flow map from
   `current_available_balance`, takes the running minimum, subtracts
   `minimum_balance_to_keep`, clamps to `[0, requested_amount]` →
   `amount_safe_to_pay`. Separately walks forward (no spending changes,
   ever) for the first day a *full* payment stays safe →
   `earliest_date_for_full_payment`.
4. **L4 (planner.py)**: generates every candidate (full payment now, wait,
   partial, each supplied installment option, each × every eligible
   spending-change subset), filters by the user's
   `payment_methods_user_will_consider` and `max_installment_months` (a
   payment-*count* cap, not a duration cap), ranks survivors by: completes
   by deadline → fewest spending changes → lowest total paid → earliest
   start → fewest payments → lowest `payment_option_id`. For request_150,
   `amount_safe_to_pay` alone (406.54) doesn't cover the EUR 496.10 course
   fee, and no changes-free candidate completes by the 16 March deadline —
   so the winner is a 3-installment option (171.98 × 3, starting the
   request date) *combined with* two spending changes (`stop` the family
   streaming plan, `reduce_to` the online retail series' EUR 16 floor) that
   free up enough headroom to keep every payment safe. It's the fewest
   spending changes among the candidates that both complete by the deadline
   and stay safe — a good moment to show the ranking rule isn't "avoid
   changes always," it's "avoid changes when a safe deadline-meeting plan
   exists without them."
5. **L5 (explain.py)**: a fixed template picked by `(status, method,
   spending_changes present?)`, not a model — string-formats the winning
   candidate. This is the exact spot the independent audit caught a real
   bug: the template dispatch checked "any spending changes present?"
   *before* checking `method`, so an installments+spending-change plan
   got the wrong single-payment sentence. Fixed, regression-tested.

**Why this is the strong example to use**: it shows the full stack in one
trace, and it's a real bug you found and fixed via an audit process you can
describe end-to-end (independent, non-importing validator script — a bug in
the engine can't hide from its own check).

## Three defendable details — know the evidence and the failure case for each

1. **`NOT_AFFORDABLE_RATIO_THRESHOLD = 0.08`** (explain.py template 5 vs 6
   discriminator). *Evidence*: computed `amount_safe_to_pay /
   requested_amount` for all 7 `not_affordable` labelled samples — template
   6 ("Although X is available today...") fires only where the ratio is
   0.110/0.122; template 5 ("None of the options...") covers the other five,
   all ≤0.048. The clusters don't overlap; 0.08 is the midpoint of the gap.
   *Failure case addressed*: without a rule, template choice on unlabelled
   requests would be a guess with no evidence trail. *Known limitation*:
   n=7 — flagged explicitly in ARCHITECTURE.md as untested in the gap zone,
   not asserted as certain.

2. **The frozen selection rule + replacement gate** (`ARCHITECTURE.md`
   §5.3a/§5.3b). *Evidence*: three failed metric choices in sequence —
   exact-match (too coarse, every config tied), MAPE (outlier-dominated by
   2-3 tiny-denominator samples, would have silently preferred a worse
   config), median % error unconstrained (picked a config that broke a
   known-safe sample's cap). *Failure case addressed*: picking a metric
   *after* seeing which config it favors — caught in this project, written
   up as a named risk, and prevented going forward by writing the rule and
   a numeric replacement bar to disk, dated, before the deciding sweep runs.
   Validated by 25-fold leave-one-out (24/25 fold agreement) — this is the
   number to lead with if asked "how do you know you didn't overfit 25
   rows."

3. **The window_days fix in `recurrence.py`**. *Evidence*: a candidate
   `flat_daily_burn` mode divided each irregular series' spend by *that
   series' own history span* instead of the user's whole observation
   window — a series that happened to start recently (e.g. 3 occurrences
   over 51 days out of a 177-day history) got a per-day rate 3x too high
   (46.70/day vs the correct 13.46/day). *Failure case addressed*:
   over-projection strong enough to make a genuinely-safe request look
   unsafe. Also a good story about correcting your own mistake: an earlier
   diagnosis blamed single-occurrence events instead — verified directly,
   found it was wrong, corrected the write-up. Good if asked "tell me about
   a time you were wrong."

## Custom terms — define before using

- **Binding vs non-binding sample**: a labelled sample where ground-truth
  `amount_safe_to_pay` is strictly below `requested_amount` (binding — the
  forecast actually matters) vs. equal to it (non-binding — trivially safe,
  any reasonable forecast gets it right).
- **IRREGULAR_MODE**: which of 4 strategies projects irregular (non-monthly)
  spending forward 90 days — `none` (don't), `flat_daily_burn`, `median_gap_repeat`,
  `per_category_monthly_frequency`.
- **cap_pass / hard constraint**: whether a candidate config keeps every
  non-binding sample uncapped (still safe for the full amount) — a
  disqualifying gate, not a scored metric.

## One monitoring signal (production judgment)

**Signal**: the fraction of decisions whose driving ratio sits inside a
known untested boundary zone — concretely, the share of `not_affordable`
rows whose `amount_safe_to_pay / requested_amount` falls in [0.048, 0.110],
the exact gap the 0.08 threshold was fit from with only 7 examples. Right
now that's **15 of 38** `not_affordable` rows on the scored 250 (~39%,
verified against the final `output.csv`, not a stale number) — a real,
disclosed figure, not hypothetical.

**Action on drift**: if that share grows as more real requests come in, it
means the threshold is being asked to discriminate on cases it was never
validated against. The action isn't to loosen or tighten the number
blindly — it's to pull the near-boundary rows into a review queue, get
real labels for them, and re-run the same evidence-based derivation
(cluster the ratios, take the new gap's midpoint) rather than hand-tuning.
Same principle as the frozen selection rule: re-derive from evidence, don't
eyeball a fix.
