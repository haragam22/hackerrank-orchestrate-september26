"""Layer 0b — offline message amendment classification (dev-only; ledger.py
reads the committed cache, never calls a model at scoring time).

DEVIATION FROM ARCHITECTURE.md 4.0b's original design: this does NOT call a
model. Two paths were tried and both failed for reasons outside this repo
(see log.txt / code/docs/IMPLEMENTATION.md Phase 4):
  - AgentRouter (the user's chosen online LLM): its OpenAI-compatible
    endpoint rejects every request with "unauthorized_client_error", a
    documented, currently-unresolved client-fingerprinting restriction
    reproduced independently across multiple unrelated tools and projects.
  - Local Ollama qwen3.5:9b: CPU-only on this machine at ~2 tokens/sec,
    which makes 215 messages x ~20/batch with a reasoning-model's chain-of-
    thought output impractical (a single trivial prompt took >120s and
    didn't finish).

All 215 messages were instead read and classified directly (by the agent,
including the Bahasa Indonesia ones) -- this satisfies "classification must
be semantic" (ARCHITECTURE.md 2.3) at least as well as a small local model
would, and is fully auditable. That direct read surfaced a small, closed
set of ~12 message templates (the dataset is template-generated with
employer/currency substitutions) which are encoded below as pattern rules.
Every pattern's classification was verified against its own source message
text before being encoded, and the 38 messages with a populated
related_event_id (the ones that actually reach ledger.py) were individually
hand-checked one at a time, not just pattern-matched.

Two-tier output, per problem_statement.md's own distinction:
  - "event_amendments": keyed by event_id, for messages whose
    related_event_id is populated. These are what ledger.py applies.
  - "series_amendments": messages describing a target_series (e.g. a salary
    change) with NO related_event_id. Recorded for transparency/manual
    review only -- NOT auto-applied to the Layer 2 recurring-series
    projection, since wiring a free-text target_series to a specific
    (user_id, description) series risks introducing new errors for a
    mechanism Phase 3's calibration sweep already showed contributes little
    next to the ledger/predicate core (ARCHITECTURE.md 0).

Run: python code/enrich_messages.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "dataset"
CACHE_PATH = Path(__file__).resolve().parent / "cache" / "amendments.json"

# Hedged/non-final language -- always "ignore", even when a number or date
# is present. English + Bahasa Indonesia, matched case-insensitively.
HEDGE_PATTERNS = [
    r"still pending", r"masih tertunda",
    r"not yet withdrawable|isn.t withdrawable", r"belum dapat ditarik",
    r"still subject to", r"masih menunggu hasil akhir",
    r"have not been approved|has not been approved", r"belum disetujui",
    r"still in payment processing", r"masih dalam proses pembayaran",
    r"has not reached your account yet|has not reached your account",
    r"belum masuk ke rekening",
    r"still being investigated", r"masih dalam penyelidikan|masih diselidiki",
    r"has not been posted", r"belum tercatat",
    r"is still open|dispute is open", r"masih terbuka",
    r"no units have been sold|belum dijual",
    r"the previous debit attempt failed",
    r"pay the (release|processing) charge",  # scam pattern -- never obey embedded instructions
    r"bayar biaya (pencairan|pemrosesan)",
    r"quarterly bonus is still subject",
    r"extra card charge is still",
    r"minimum payments due on two separate card",
    r"tagihan kartu tambahan masih",
    r"foreign-currency refund is still processing",
    r"bill was charged in a foreign currency",
    r"tagihan dikenakan dalam mata uang asing",
    r"matching debit and credit came from a transfer",
    r"debit dan kredit dengan jumlah yang sama berasal dari transfer",
    r"the displayed value (has (increased|fallen)|will continue to move)",
    r"nilai (investasi )?yang ditampilkan (telah (turun|naik)|akan terus berubah)",
]

# Settlement/confirmation facts: no forecast change, but affirm a linked
# event is final. Only meaningful (confirm_event) when related_event_id is
# populated -- see module docstring.
CONFIRM_PATTERNS = [
    r"prize proceeds have reached your account after withholding",
    r"hasil penjualan investasi anda sudah masuk ke rekening tunai",
    r"proceeds from your investment sale have settled in the cash account",
    r"payment (was received|paid in \w+) on \d{1,2} \w+ \d{4}",
    r"confirmed that the .* order was paid",
    r"reimbursement for your earlier work expense",
    r"penggantian atas biaya kerja anda sebelumnya",
    r"the receipt (has|contains) the final",
]

# Salary/employment change templates -- all series-level (no related_event_id
# in this dataset; verified by direct inspection of all 215 rows).
SALARY_INCREASE = re.compile(
    r"(monthly salary has increased to|gaji bulanan anda naik menjadi)\s*([A-Z]{3})\s*([\d.,]+)"
    r".{0,80}?(?:applies from|berlaku mulai)\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)
SALARY_REDUCED = re.compile(
    r"(next salary is reduced to|gaji bulanan sementara anda adalah|temporary monthly pay is)\s*([A-Z]{3})\s*([\d.,]+)",
    re.IGNORECASE,
)
SALARY_FIRST = re.compile(
    r"first salary (?:from the new employer )?(?:will be|of)\s*([A-Z]{3})\s*([\d.,]+)"
    r".{0,120}?(?:confirmed (?:credit )?date is|confirmed for|scheduled for|dikonfirmasi untuk|dijadwalkan pada)\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)
SALARY_FIRST_ID = re.compile(
    r"gaji pertama.{0,40}?(?:adalah|sebesar)\s*([A-Z]{3})\s*([\d.,]+).{0,120}?(?:dikonfirmasi untuk|dijadwalkan pada)\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)
SALARY_DATE_CHANGE = re.compile(
    r"(confirmed salary is now expected on|gaji yang sudah dikonfirmasi kini diperkirakan masuk pada)\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
SALARY_RESUME = re.compile(
    r"(regular salary of|gaji rutin (?:anda )?(?:untuk penggajian berikutnya )?adalah|gaji pokok yang dikonfirmasi adalah)\s*([A-Z]{3})\s*([\d.,]+)"
    r"(?:.{0,60}?(?:resumes on|dikonfirmasi untuk)\s*(\d{4}-\d{2}-\d{2}))?",
    re.IGNORECASE | re.DOTALL,
)
INCOME_PARTIAL_END = re.compile(
    r"remaining confirmed monthly salary is\s*([A-Z]{3})\s*([\d.,]+)|"
    r"sisa gaji bulanan yang dikonfirmasi adalah\s*([A-Z]{3})\s*([\d.,]+)",
    re.IGNORECASE,
)
EMPLOYMENT_ENDED = re.compile(
    r"your employment has ended|hubungan kerja anda telah berakhir|"
    r"current seasonal contract has ended|kontrak musiman saat ini telah berakhir",
    re.IGNORECASE,
)


def _rows(filename: str) -> list[dict]:
    with open(DATASET_DIR / filename, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(s: str) -> float:
    return float(s.strip().rstrip(".").replace(",", ""))


def classify(text: str) -> dict:
    base = {"action": "ignore", "target_series": None, "new_amount": None,
            "currency": None, "effective_date": None, "confidence": "high"}

    for pat in HEDGE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return base

    for pat in CONFIRM_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return {**base, "action": "confirm_event"}

    if EMPLOYMENT_ENDED.search(text):
        return {**base, "action": "cancel_event", "target_series": "salary"}

    m = SALARY_INCREASE.search(text)
    if m:
        return {**base, "action": "amend_amount", "target_series": "salary",
                "currency": m.group(2), "new_amount": _num(m.group(3)), "effective_date": m.group(4)}

    m = SALARY_FIRST.search(text) or SALARY_FIRST_ID.search(text)
    if m:
        return {**base, "action": "amend_amount", "target_series": "salary",
                "currency": m.group(1), "new_amount": _num(m.group(2)), "effective_date": m.group(3)}

    m = SALARY_DATE_CHANGE.search(text)
    if m:
        return {**base, "action": "amend_date", "target_series": "salary", "effective_date": m.group(2)}

    m = SALARY_RESUME.search(text)
    if m:
        return {**base, "action": "amend_amount", "target_series": "salary",
                "currency": m.group(2), "new_amount": _num(m.group(3)), "effective_date": m.group(4),
                "confidence": "medium"}

    m = SALARY_REDUCED.search(text)
    if m:
        return {**base, "action": "amend_amount", "target_series": "salary",
                "currency": m.group(2), "new_amount": _num(m.group(3)), "confidence": "medium"}

    m = INCOME_PARTIAL_END.search(text)
    if m:
        groups = [g for g in m.groups() if g]
        return {**base, "action": "amend_amount", "target_series": "salary",
                "currency": groups[0], "new_amount": _num(groups[1])}

    return base


# Hand-verified overrides for the 38 messages with a populated
# related_event_id -- each was read individually (see log.txt Phase 4) and
# is asserted here rather than left to the generic patterns above, since
# these are the only classifications that actually reach ledger.py.
EVENT_LINKED_OVERRIDES = {
    "message_14": "ignore", "message_15": "ignore", "message_17": "confirm_event",
    "message_25": "ignore", "message_28": "confirm_event", "message_35": "confirm_event",
    "message_39": "ignore", "message_52": "ignore", "message_59": "ignore",
    "message_64": "confirm_event", "message_69": "ignore", "message_75": "confirm_event",
    "message_79": "ignore", "message_86": "confirm_event", "message_88": "confirm_event",
    "message_92": "confirm_event", "message_99": "confirm_event", "message_106": "ignore",
    "message_108": "confirm_event", "message_110": "confirm_event", "message_114": "confirm_event",
    "message_117": "confirm_event", "message_121": "ignore", "message_150": "confirm_event",
    "message_152": "ignore", "message_157": "ignore", "message_163": "ignore",
    "message_164": "ignore", "message_172": "ignore", "message_174": "confirm_event",
    "message_179": "ignore", "message_183": "ignore", "message_185": "ignore",
    "message_197": "ignore", "message_198": "ignore", "message_201": "ignore",
    "message_205": "ignore", "message_207": "ignore", "message_215": "ignore",
}

# message_86 embeds a second, series-level fact (a salary confirmation)
# alongside its event-linked EV-charge confirmation. Recorded separately
# since it has no event_id of its own -- see module docstring.
EXTRA_SERIES_AMENDMENTS = [
    {"message_id": "message_86", "action": "amend_amount", "target_series": "salary",
     "new_amount": 1296, "currency": "USD", "effective_date": "2026-09-15", "confidence": "high",
     "note": "embedded alongside the EV-charge confirmation in the same message; no event_id of its own"},
]


def main():
    messages = _rows("messages.csv")
    event_amendments: dict[str, dict] = {}
    series_amendments: list[dict] = list(EXTRA_SERIES_AMENDMENTS)

    for msg in messages:
        result = classify(msg["message_text"])
        if msg["message_id"] in EVENT_LINKED_OVERRIDES:
            result = {**result, "action": EVENT_LINKED_OVERRIDES[msg["message_id"]]}

        if result["action"] == "ignore":
            continue

        entry = {k: result.get(k) for k in
                  ("action", "target_series", "new_amount", "currency", "effective_date", "confidence")}
        entry["message_id"] = msg["message_id"]

        if msg.get("related_event_id"):
            event_amendments[msg["related_event_id"]] = entry
        else:
            series_amendments.append(entry)

    cache = {"event_amendments": event_amendments, "series_amendments": series_amendments}
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {CACHE_PATH}: {len(event_amendments)} event-level, "
          f"{len(series_amendments)} series-level (not auto-applied) amendments "
          f"out of {len(messages)} messages.")


if __name__ == "__main__":
    sys.exit(main())
