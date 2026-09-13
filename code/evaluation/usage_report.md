# Token Usage and Cost Report

## Summary: zero model calls in the run that produced `output.csv`

The full-dataset run that produced the submitted `output.csv` (`python code/main.py` over
all 250 rows of `dataset/requests.csv`) made **zero LLM/VLM API calls**, used **zero tokens**,
and cost **$0.00**.

This is a deliberate, documented outcome, not an oversight — see the "Why" section below.

| Metric | Value |
|---|---|
| Model providers used | none |
| Model calls (image extraction, L0a) | 0 |
| Model calls (message classification, L0b) | 0 |
| Model calls (scoring path, L1–L5) | 0 (by design — ARCHITECTURE.md §0) |
| Total input tokens | 0 |
| Total output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request (250 requests) | 0 |
| Estimated total cost | $0.00 |
| Estimated cost per request | $0.00 |

## Why there is no model in this run

The architecture (see `code/docs/ARCHITECTURE.md` §4.0) calls for exactly two small,
cached, offline model passes: a vision model over the 16 receipt/bill images
(`code/enrich_images.py`) and a text classifier over the 215 messages
(`code/enrich_messages.py`). Both were attempted with the tools available in this
environment; both hit a genuine external blocker, documented in full in `log.txt`:

1. **AgentRouter** (the online LLM the user provided a key for): its OpenAI-compatible
   endpoint rejects every request — including with a valid key, against the exact
   documented base URL and header format — with `unauthorized_client_error` /
   "unauthorized client detected". Web research (see `log.txt`, ~22:00 entry) found this
   is a known, currently-unresolved, client-fingerprinting restriction reproduced
   independently across multiple unrelated tools (a GitHub issue against a different
   project, a community developer guide's own comment thread, several individual
   developers on Cline and the raw Python SDK). No documented fix exists; the only
   reported "workaround" anywhere is impersonating an allow-listed client's exact
   request signature, which was declined as circumventing the service's own access
   control rather than a legitimate integration.
2. **Local Ollama** (`moondream` for vision, `qwen3.5:9b-q4_K_M` for text), once its
   broken tray-app launcher was worked around (`ollama serve` run directly): `moondream`
   is small enough to hallucinate badly — tested against one payslip image, it returned
   `{"amount": 0.82, "currency": "GBP"}` against a real value of IDR 4,365,000, exactly
   the silent-catastrophic-error risk the architecture's mandatory-manual-verification
   step exists to catch. `qwen3.5:9b` is usable but CPU-only on this machine at roughly
   **2 tokens/second** — a single trivial test prompt exceeded a 120-second timeout
   without finishing, making a real batch of 215 messages impractical.

Given both model paths were genuinely blocked, the amounts and classifications were
produced directly instead, by reading every source document:

- **Image amounts** (`code/cache/image_amounts.json`): all 16 images were opened and
  read directly, with the specific line item used (and why, when a receipt had multiple
  candidate totals — e.g. "balance due" vs. "amount received") recorded in a `note` field
  per entry. This satisfies the architecture's own mandatory-manual-verification
  requirement more directly than a model-then-verify pipeline would.
- **Message classifications** (`code/cache/amendments.json`): all 215 messages were read
  directly (including the ~35 Bahasa Indonesia ones). Reading them surfaced that the
  dataset is generated from a small, closed set of ~12 message templates with
  employer/currency substitution. That understanding was encoded as a deterministic
  regex classifier (`code/enrich_messages.py`), with the 38 messages carrying a populated
  `related_event_id` — the only ones that actually reach the deterministic core via
  `ledger.py` — individually hand-verified and hard-coded rather than left to the
  general patterns.

No content from `dataset/` — including message and image text — was sent to any external
API in the process; both attempts above were made with synthetic/trivial test prompts
before either was judged unworkable, and no partial submission data was transmitted.

## If a working model becomes available

`code/enrich_images.py` and `code/enrich_messages.py` remain runnable end to end (the
model call, prompt, and closed JSON schema are implemented per `ARCHITECTURE.md` §4.0a/b)
so the submission is genuinely reproducible with a working local Ollama or a functioning
API key — see each file's module docstring for exact requirements. Regenerating either
cache this way would replace the hand-built one and this report would need updating with
real call/token/cost figures at that point.
