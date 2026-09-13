"""Phase 0 — dataset loaders.

Reads every file in dataset/ into typed, joinable structures. No business
logic here (that starts in ledger.py / L1). Keep this file boring: parse,
type-convert, index by id. See code/docs/IMPLEMENTATION.md Phase 0.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
IMAGES_DIR = DATASET_DIR / "media" / "images"


def _date(s: str) -> Optional[dt.date]:
    s = (s or "").strip()
    return dt.date.fromisoformat(s) if s else None


def _num(s: str) -> Optional[float]:
    s = (s or "").strip()
    return float(s) if s else None


def _bool(s: str) -> bool:
    return (s or "").strip().lower() == "true"


def _pipe_list(s: str) -> list[str]:
    s = (s or "").strip()
    return s.split("|") if s else []


def _rows(filename: str) -> list[dict]:
    path = DATASET_DIR / filename
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@dataclass
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: list[str]
    expense_categories_to_protect: list[str]
    expense_categories_user_is_willing_to_reduce: list[str]
    expense_categories_user_is_willing_to_stop: list[str]
    payment_methods_user_will_consider: list[str]
    max_installment_months: Optional[int]  # actually a payment-COUNT cap, see CLAUDE.md


@dataclass
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str  # debit | credit | non_cash
    amount: Optional[float]  # None means blank -> resolve via images cache
    currency: str
    event_date: dt.date
    settlement_date: Optional[dt.date]
    status: str  # settled|pending|scheduled|cancelled|failed|unrealized
    linked_event_id: Optional[str]
    flexibility: str  # fixed|reducible|stoppable|reducible_or_stoppable
    minimum_allowed_amount: Optional[float]


@dataclass
class ExchangeRate:
    rate_date: dt.date
    from_currency: str
    to_currency: str
    rate: float


@dataclass
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: float
    number_of_payments: int
    first_payment_date: Optional[dt.date]
    payment_frequency_days: Optional[int]
    financing_fee: float
    total_payable_amount: float


@dataclass
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: str
    source_type: str
    message_text: str


@dataclass
class ImageRef:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]

    @property
    def path(self) -> Path:
        return IMAGES_DIR / f"{self.image_id}.png"


@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: dt.date
    request_type: str
    requested_amount: float
    desired_completion_date: dt.date
    allows_partial_payment: bool
    request_text: str


@dataclass
class Dataset:
    profiles: dict[str, Profile] = field(default_factory=dict)
    events_by_user: dict[str, list[Event]] = field(default_factory=dict)
    events_by_id: dict[str, Event] = field(default_factory=dict)
    exchange_rates: list[ExchangeRate] = field(default_factory=list)
    payment_options_by_request: dict[str, list[PaymentOption]] = field(default_factory=dict)
    messages_by_request: dict[str, list[Message]] = field(default_factory=dict)
    messages_by_user: dict[str, list[Message]] = field(default_factory=dict)
    messages_by_event: dict[str, list[Message]] = field(default_factory=dict)
    images: list[ImageRef] = field(default_factory=list)
    images_by_request: dict[str, list[ImageRef]] = field(default_factory=dict)
    images_by_event: dict[str, list[ImageRef]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    sample_requests: list[Request] = field(default_factory=list)
    sample_ground_truth: dict[str, dict] = field(default_factory=dict)


def load() -> Dataset:
    ds = Dataset()

    for r in _rows("financial_profiles.csv"):
        p = Profile(
            user_id=r["user_id"],
            home_currency=r["home_currency"],
            current_available_balance=_num(r["current_available_balance"]),
            minimum_balance_to_keep=_num(r["minimum_balance_to_keep"]),
            financial_priorities=_pipe_list(r["financial_priorities"]),
            expense_categories_to_protect=_pipe_list(r["expense_categories_to_protect"]),
            expense_categories_user_is_willing_to_reduce=_pipe_list(
                r["expense_categories_user_is_willing_to_reduce"]
            ),
            expense_categories_user_is_willing_to_stop=_pipe_list(
                r["expense_categories_user_is_willing_to_stop"]
            ),
            payment_methods_user_will_consider=_pipe_list(
                r["payment_methods_user_will_consider"]
            ),
            max_installment_months=(
                int(r["max_installment_months"]) if (r["max_installment_months"] or "").strip() else None
            ),
        )
        ds.profiles[p.user_id] = p

    for r in _rows("financial_events.csv"):
        e = Event(
            event_id=r["event_id"],
            user_id=r["user_id"],
            event_type=r["event_type"],
            description=r["description"],
            category=r["category"],
            direction=r["direction"],
            amount=_num(r["amount"]),  # may be None -> blank, resolve via image cache
            currency=r["currency"],
            event_date=_date(r["event_date"]),
            settlement_date=_date(r["settlement_date"]),
            status=r["status"],
            linked_event_id=(r["linked_event_id"] or None),
            flexibility=r["flexibility"],
            minimum_allowed_amount=_num(r["minimum_allowed_amount"]),
        )
        ds.events_by_id[e.event_id] = e
        ds.events_by_user.setdefault(e.user_id, []).append(e)

    for r in _rows("exchange_rates.csv"):
        ds.exchange_rates.append(
            ExchangeRate(
                rate_date=_date(r["rate_date"]),
                from_currency=r["from_currency"],
                to_currency=r["to_currency"],
                rate=_num(r["rate"]),
            )
        )

    for r in _rows("request_payment_options.csv"):
        po = PaymentOption(
            payment_option_id=r["payment_option_id"],
            request_id=r["request_id"],
            payment_method=r["payment_method"],
            payment_amount=_num(r["payment_amount"]),
            number_of_payments=int(r["number_of_payments"]),
            first_payment_date=_date(r["first_payment_date"]),
            payment_frequency_days=(
                int(r["payment_frequency_days"]) if (r["payment_frequency_days"] or "").strip() else None
            ),
            financing_fee=_num(r["financing_fee"]) or 0.0,
            total_payable_amount=_num(r["total_payable_amount"]),
        )
        ds.payment_options_by_request.setdefault(po.request_id, []).append(po)

    for r in _rows("messages.csv"):
        m = Message(
            message_id=r["message_id"],
            user_id=r["user_id"],
            request_id=(r["request_id"] or None),
            related_event_id=(r["related_event_id"] or None),
            sent_at=r["sent_at"],
            source_type=r["source_type"],
            message_text=r["message_text"],
        )
        ds.messages_by_user.setdefault(m.user_id, []).append(m)
        if m.request_id:
            ds.messages_by_request.setdefault(m.request_id, []).append(m)
        if m.related_event_id:
            ds.messages_by_event.setdefault(m.related_event_id, []).append(m)

    for r in _rows("images.csv"):
        img = ImageRef(
            image_id=r["image_id"],
            user_id=r["user_id"],
            request_id=(r["request_id"] or None),
            related_event_id=(r["related_event_id"] or None),
        )
        ds.images.append(img)
        if img.request_id:
            ds.images_by_request.setdefault(img.request_id, []).append(img)
        if img.related_event_id:
            ds.images_by_event.setdefault(img.related_event_id, []).append(img)

    for r in _rows("requests.csv"):
        ds.requests.append(
            Request(
                request_id=r["request_id"],
                user_id=r["user_id"],
                request_date=_date(r["request_date"]),
                request_type=r["request_type"],
                requested_amount=_num(r["requested_amount"]),
                desired_completion_date=_date(r["desired_completion_date"]),
                allows_partial_payment=_bool(r["allows_partial_payment"]),
                request_text=r["request_text"],
            )
        )

    for r in _rows("sample_requests.csv"):
        ds.sample_requests.append(
            Request(
                request_id=r["request_id"],
                user_id=r["user_id"],
                request_date=_date(r["request_date"]),
                request_type=r["request_type"],
                requested_amount=_num(r["requested_amount"]),
                desired_completion_date=_date(r["desired_completion_date"]),
                allows_partial_payment=_bool(r["allows_partial_payment"]),
                request_text=r["request_text"],
            )
        )
        ds.sample_ground_truth[r["request_id"]] = {
            "amount_safe_to_pay": r["amount_safe_to_pay"],
            "affordability_status": r["affordability_status"],
            "recommended_payment_method": r["recommended_payment_method"],
            "payment_plan": r["payment_plan"],
            "earliest_date_for_full_payment": r["earliest_date_for_full_payment"],
            "spending_changes_needed": r["spending_changes_needed"],
            "decision_explanation": r["decision_explanation"],
        }

    return ds


def _self_check() -> None:
    ds = load()
    counts = {
        "profiles": len(ds.profiles),
        "events": len(ds.events_by_id),
        "exchange_rates": len(ds.exchange_rates),
        "payment_options": sum(len(v) for v in ds.payment_options_by_request.values()),
        "messages": sum(len(v) for v in ds.messages_by_user.values()),
        "images": len(ds.images),
        "requests": len(ds.requests),
        "sample_requests": len(ds.sample_requests),
    }
    expected = {
        "profiles": 275,
        "events": 25342,
        "exchange_rates": 134,
        "payment_options": 790,
        "messages": 215,
        "images": 16,
        "requests": 250,
        "sample_requests": 25,
    }
    print("counts:", counts)
    for k, v in expected.items():
        status = "OK" if counts[k] == v else f"MISMATCH (expected {v})"
        print(f"  {k}: {counts[k]} {status}")

    req_users = {r.user_id for r in ds.requests}
    sample_users = {r.user_id for r in ds.sample_requests}
    overlap = req_users & sample_users
    print("user overlap requests<->sample_requests:", len(overlap), "(expect 0)")

    missing_images = [img.image_id for img in ds.images if not img.path.exists()]
    print("missing image files:", missing_images or "none")

    r0 = ds.requests[0]
    print(f"\nsample join for {r0.request_id} (user {r0.user_id}):")
    print(f"  profile: {ds.profiles.get(r0.user_id)}")
    print(f"  events for user: {len(ds.events_by_user.get(r0.user_id, []))}")
    print(f"  payment options: {[po.payment_option_id for po in ds.payment_options_by_request.get(r0.request_id, [])]}")
    print(f"  messages: {[m.message_id for m in ds.messages_by_request.get(r0.request_id, [])]}")


if __name__ == "__main__":
    _self_check()
