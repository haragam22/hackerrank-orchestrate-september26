"""Phase 1 — Layer 1: ledger reconstruction.

Per-user, per-request clean event list: status filter, linked_event_id
chain resolution, blank-amount fill (image cache), amendment application
(message cache), FX normalization to home_currency, history/known_future
split. No free parameters — a test failure here is a bug, not a tuning
knob. See ARCHITECTURE.md §4.1, code/docs/CLAUDE.md.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from pathlib import Path

from io_loader import Dataset, Event

DROP_STATUSES = {"cancelled", "failed", "unrealized"}
CACHE_DIR = Path(__file__).resolve().parent / "cache"


def load_cache(name: str) -> dict:
    path = CACHE_DIR / name
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def clean_events(ds: Dataset, user_id: str,
                  image_amounts: dict | None = None,
                  amendments: dict | None = None) -> list[Event]:
    """Status/direction filter + linked_event_id resolution + blank-amount
    fill + amendment application. No FX, no history/future split — see
    build_ledger() for the full per-request pipeline."""
    image_amounts = load_cache("image_amounts.json") if image_amounts is None else image_amounts
    if amendments is None:
        raw_amendments = load_cache("amendments.json")
        # enrich_messages.py writes {"event_amendments": {...}, "series_amendments": [...]};
        # unwrap to the event_id-keyed dict clean_events() actually applies.
        amendments = raw_amendments.get("event_amendments", raw_amendments)

    raw = ds.events_by_user.get(user_id, [])

    # 1. status/direction filter
    kept = [e for e in raw if e.status not in DROP_STATUSES and e.direction != "non_cash"]

    # 2. linked_event_id chains: authorization/pending predecessor is
    # dropped once its settled successor exists; refunds are a further row
    # linking to the settlement and are kept as their own credit.
    by_id = {e.event_id: e for e in kept}
    drop_ids = set()
    for e in kept:
        if e.linked_event_id and e.linked_event_id in by_id:
            predecessor = by_id[e.linked_event_id]
            if predecessor.status in {"cancelled", "pending"} and e.status == "settled":
                drop_ids.add(predecessor.event_id)
    kept = [e for e in kept if e.event_id not in drop_ids]

    # 3. blank-amount fill from image cache (Phase 4 populates this; empty
    # cache in earlier phases means these rows stay amount=None and are
    # dropped at the end rather than silently treated as zero)
    resolved = []
    for e in kept:
        if e.amount is None:
            cached = image_amounts.get(e.event_id)
            if cached and cached.get("amount") is not None:
                e = replace(e, amount=cached["amount"], currency=cached.get("currency") or e.currency)
        resolved.append(e)

    # 4. amendments: amend_amount / amend_date / cancel_event / confirm_event
    # (no-op) / new_one_off (not applicable to an existing event_id, ignored
    # here — new_one_off rows are synthesized separately if ever needed).
    final = []
    for e in resolved:
        amd = amendments.get(e.event_id)
        if not amd or amd.get("action") in (None, "ignore", "confirm_event", "new_one_off"):
            final.append(e)
            continue
        action = amd["action"]
        if action == "cancel_event":
            continue
        if action == "amend_amount" and amd.get("new_amount") is not None:
            e = replace(e, amount=amd["new_amount"], currency=amd.get("currency") or e.currency)
        if action == "amend_date" and amd.get("effective_date"):
            e = replace(e, event_date=dt.date.fromisoformat(amd["effective_date"]))
        final.append(e)

    return [e for e in final if e.amount is not None]


def fx_to_home(amount: float, from_ccy: str, to_ccy: str, on_date: dt.date, rates: list) -> float:
    if from_ccy == to_ccy:
        return amount
    forward = [r for r in rates if r.from_currency == from_ccy and r.to_currency == to_ccy and r.rate_date <= on_date]
    if forward:
        best = max(forward, key=lambda r: r.rate_date)
        return amount * best.rate
    reverse = [r for r in rates if r.from_currency == to_ccy and r.to_currency == from_ccy and r.rate_date <= on_date]
    if reverse:
        best = max(reverse, key=lambda r: r.rate_date)
        return amount / best.rate
    raise ValueError(f"no exchange rate {from_ccy}->{to_ccy} on or before {on_date}")


@dataclass
class Ledger:
    history: list[Event]        # event_date <= request_date
    known_future: list[Event]   # pending/scheduled, event_date > request_date


def build_ledger(ds: Dataset, user_id: str, request_date: dt.date,
                  image_amounts: dict | None = None,
                  amendments: dict | None = None,
                  pending_debits: bool = True) -> Ledger:
    """pending_debits: Phase-3 calibrated convention (ARCHITECTURE.md §5.2).
    The problem statement says to ignore pending *credits*; True (the
    confirmed default -- user_02's pending merchant debit) also counts
    pending debits as real future outflows. False excludes them too, kept
    only as a grid point to sweep against."""
    profile = ds.profiles[user_id]
    events = clean_events(ds, user_id, image_amounts, amendments)

    normalized = [
        replace(e, amount=fx_to_home(e.amount, e.currency, profile.home_currency, e.event_date, ds.exchange_rates),
                currency=profile.home_currency)
        for e in events
    ]

    history = [e for e in normalized if e.event_date <= request_date]
    known_future = [
        e for e in normalized
        if e.event_date > request_date and e.status in {"pending", "scheduled"}
        and (pending_debits or not (e.status == "pending" and e.direction == "debit"))
    ]
    return Ledger(history=history, known_future=known_future)
