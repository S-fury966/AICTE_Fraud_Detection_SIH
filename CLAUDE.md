# CLAUDE.md — AICTE Fraud-Signal Detection (SIH 2026 Prototype)

## Purpose
A decision-support platform that performs a first-pass scrutiny of AICTE Extension of
Approval (EoA) applications and surfaces **possible** fraud signals, with evidence, for a
human official to review. It never makes an approval decision and never asserts that
forgery or misconduct occurred. Every output is a *preliminary indicator*.

## Scope (deliberately narrow — four detectors only)
1. **Certificate authenticity** — OCR field extraction (institution, certificate number,
   issuing body, programme, validity dates); QR payload decode and cross-check against OCR
   fields; QR domain allowlist check; mock issuer-service lookup by certificate number;
   PDF digital-signature validation where present; duplicate detection via exact file hash,
   perceptual hash, and same certificate number across different institutions.
2. **Faculty count consistency** — compare declared (application) vs. college website vs.
   mock AICTE records; per-department comparison with configurable tolerance; record the
   capture date of each source; flag the same faculty member across multiple colleges.
3. **Photo authenticity (labs/classrooms)** — ranked by reliability:
   reused images (perceptual hash; reverse image search where feasible) > EXIF checks
   (capture date, GPS vs. institution location, editing-software tags; *missing EXIF is a
   weak signal only*) > manipulation checks (ELA, copy-move) > AI-generated image detection
   (low-confidence hint only) > claim-vs-image consistency via VLM (advisory only).
4. **Risk scoring and review** — each flag carries type, reason, linked evidence,
   confidence; flags combine into a weighted risk level (low/medium/high) with configurable
   weights. Risk level **prioritizes review order only**.

Out of scope: validating every parameter of every document; any automated
approval/rejection; access to real government databases.

## Core design principles
- **The LLM reads and explains; deterministic code decides.** LLM use is limited to:
  structured-field extraction from messy text, fuzzy name matching, extracting faculty
  lists from web pages, and writing plain-language explanations. **An LLM never decides
  pass/fail, authenticity, risk level, or verification status.**
- **Everything configurable.** Allowlists, tolerances, weights, thresholds live in
  versioned YAML/JSON config, never hard-coded.
- **Explainability.** Every flag must carry its reason and links to its evidence
  (file, page/region, source response, field compared).
- **Audit trail.** Append-only log of checks run, records consulted, raw source responses,
  flags produced, and human actions. Every evaluation stores the rule/config version used.
- **Role-based access.** Officer, senior reviewer, admin. Access is logged.
- **No restricted databases.** All external sources (issuer, AICTE records) are mocked.
- **Privacy.** Institutional and personal data is sensitive: log access, honour
  data-retention limits, avoid sending PII to external services unnecessarily.
- **Demo data only.** Synthetic institutions, certificates, faculty lists, photos.

## Fixed verification status taxonomy
Set by code logic only — never by an LLM, and never extended ad hoc:
- `verified`
- `mismatch_detected`
- `expired`
- `verification_pending`
- `source_unavailable`
- `manual_review_required`

## Language rules for user-facing output
- Never "fake", "forged", "fraudulent", or "deepfake detected".
- Use "possible synthetic content, manual verification needed", "mismatch detected
  between X and Y", "requires manual verification".
- Always pair a flag with its evidence and confidence.

## Evaluation
A synthetic fraud test set with injected frauds (duplicated certificate, edited certificate
number, QR to fake domain, inflated faculty counts, reused lab photo, AI-generated
classroom image). Report per-fraud-type detection rate, false-positive rate, and average
processing time per application.

## Stack
Python 3.10 / FastAPI backend, SQLite for the prototype (SQLAlchemy models so Postgres
remains a drop-in), React dashboard, config in YAML. (3.10 is a hard constraint, not a
preference — see Runtime below.)

## Working agreement
Build incrementally, one step per instruction. Do not expand scope beyond the four
detectors. Do not add an automated decision path.

## Confirmed decisions (2026-10-23)
- **LLM/VLM**: free-tier LLM APIs plus a free VLM. Therefore the `llm/` layer is
  provider-agnostic behind one interface, with on-disk response caching and recorded
  fixtures so a demo never depends on a live rate-limited call. Strict schema validation on
  every response; a malformed or unavailable response yields `verification_pending` or
  `source_unavailable`, never a guess.
- **Reverse image search**: internal only. Photo-reuse detection compares against the
  corpus of previously submitted photos in our own database. No external image APIs.
- **College website faculty lists**: synthetic HTML pages under `data/demo/`. No live
  scraping of real institution websites.
- **Authentication**: skipped for the demo. Roles (officer / senior reviewer / admin)
  remain in the data model and in audit records as an actor field, but there is no login.
- **PDF digital signatures**: scope is limited to **post-signature modification** detection
  — does the signature's ByteRange cover the whole file, are there incremental updates
  after signing. No certificate-chain or trust-store validation. Statuses:
  signature absent / present and byte-range intact / modified after signing /
  unverifiable.
- **Risk scoring**: both per-detector sub-scores and one combined application-level risk
  level. Weights for each configurable.
- **Throughput**: one application at a time. No batch pipeline, no job queue.

### Settled since
- **Mock records are per department** for the faculty detector, not totals only. A totals
  comparison cannot distinguish a genuine overall count from departments padded to cover a
  shortfall elsewhere, which is the pattern worth detecting.
- **One application at a time.** No batch pipeline, no job queue. (Confirmed 2026-10-23.)
- **LLM provider: Groq**, or any other OpenAI-compatible endpoint, selected in config. One
  client file serves all of them; see "LLM provider" above.

### Still open (ask when the relevant step is reached)
- Faculty identity matching: synthetic unique ID vs. fuzzy name+qualification matching.
- Admin UI for live editing of weights/allowlists vs. config files only. Now covers four
  config files: extraction, certificate extraction, QR domains, issuer verification.

## Runtime (set in step 1)
- **Python 3.10** in `.venv`, created from the 3.10 interpreter on the dev machine.
  Reason: `rapidocr-onnxruntime` 1.4.x declares `Requires-Python <3.13`, a hard metadata
  pin, so the machine's default 3.14 cannot install it. (onnxruntime itself does ship a
  cp314 wheel, so the constraint is the RapidOCR wrapper alone. Moving to 3.13+ would mean
  the newer `rapidocr` 3.x package, which is a different API and untested here.)
- **OCR engine: RapidOCR** (`rapidocr-onnxruntime`), behind a swappable one-method
  interface. Chosen over Tesseract because it pip-installs with bundled ONNX models and
  needs no system binary or PATH entry on a demo machine.
- Dependencies are pinned in `backend/requirements.txt`.
- Tests: `cd backend && python -m pytest`. Real-OCR tests carry the `ocr` marker;
  deselect with `-m "not ocr"`.

## Build status
- **Step 1 — document text extraction: done.** `backend/app/extraction`. Per-page text from a
  PDF text layer or OCR (RapidOCR), with the decision and its measurements recorded per page.
  `config/extraction.yaml`.
- **Step 2 — certificate field extraction: done.** `backend/app/certificates`, with the LLM
  seam in `backend/app/llm`. Nine fields from the accreditation certificate.
  `config/certificate_extraction.yaml`, prompt in
  `backend/app/llm/prompts/certificate_extraction.v1.md`.
- **Step 3 — QR detection and cross-check: done.** `backend/app/qr`, `config/qr_checks.yaml`.
  No LLM anywhere in this module. Eight finding types, each carrying evidence and a
  configured `strong`/`weak` label. Details in `backend/app/qr/README.md`.
- **Step 4 — mock issuer and verification client: done.** `backend/app/mock_issuer` (the
  synthetic registry and a FastAPI stand-in) and `backend/app/issuer` (the client,
  comparison and status mapping). `config/issuer_verification.yaml`,
  `data/demo/issuer_registry.json`. No LLM. First consumer of the status taxonomy, now
  defined once in `backend/app/core/verification.py`. Details in
  `backend/app/issuer/README.md`.

### The snippet-validation rule (step 2's core guard)
Every non-null value the model returns must be traceable to the source text, or code
discards it. Two tiers:
1. **Snippet check** — the model's `source_snippet` must appear in the page text, tolerant of
   whitespace, case and small differences (`min_similarity`, default 0.92) because OCR text is
   noisy. If it does not appear, the value is set to `null` with reason
   `snippet_not_in_source`. The claimed value and snippet are retained for the reviewer.
2. **Exact-value check** — for the fields in `exact_value_fields` (certificate number, grade,
   the three dates), the value itself must also appear verbatim after normalisation. Fuzzy
   matching alone is unsafe on an identifier: a certificate number altered by one digit scores
   ~0.97 against the original line. Failure gives reason `value_not_in_source`. Prose fields
   such as institution name stay fuzzy-only, since OCR routinely mangles names.

Also deterministic, never the model's call: date parsing to ISO (day-first, ambiguity
recorded), the validity-window and expiry report, and the `validated` flag on every field.
A missing field is a normal, successful result — later steps fall back to other evidence
such as the QR payload.

### LLM provider
One client serves every hosted provider: `app/llm/openai_compatible.py`, written against
the OpenAI-compatible `/v1/chat/completions` shape rather than a vendor SDK, because that
shape is what the vendors share. A provider is therefore a `base_url`, model and
`api_key_env` in `config/certificate_extraction.yaml` — `groq`, `mistral`, `openrouter`,
`together` and `local` are registered names, and adding another free tier is a config block,
not code. Free tiers appear and vanish, so switching must not be a code change. A `stub`
provider backs every offline test.

**Active: `groq`, model `openai/gpt-oss-120b`** (key in `.env` as `GROQ_API_KEY`). Verified
end to end on 2026-10-23: nine fields from the synthetic certificate, ~1.2k in / ~1.0k out
tokens, one attempt, no retries. gpt-oss-120b is a reasoning model, so output tokens run
high for the size of the answer.

The `mistral` block is kept and working, but that account reports a quota of 0 requests per
minute, so it cannot currently serve a call.

`temperature: 0` and server-side JSON mode are sent, but response stability rests on the
prompt contract plus the deterministic validation above, not on the sampling settings.

Live tests run against the configured provider only, skipping when its key is absent and
failing loudly when the key is present but rejected. `SIH_LIVE_PROVIDERS=groq,mistral`
checks others on demand. The CLI loads `.env` itself, so it needs no exported variables.

A free tier answers 429 constantly, so the provider waits and retries on its own
(`rate_limit_retries`, `rate_limit_backoff_seconds`, honouring `Retry-After`), separately
from the schema retries — those exist for a different problem. One 429 is *not* treated as
congestion: a response carrying `x-ratelimit-limit-req-minute: 0` means the workspace has no
request allowance at all, so it fails immediately as `provider_unavailable` with the console
link, instead of sleeping through a wait that cannot help.

### Step 3 decisions

**Never fetch a QR URL.** Nothing in `app/qr` fetches, opens, resolves (DNS) or follows a
URL from a QR code. A payload is a string chosen by whoever submitted the document; it is
parsed and reported, never acted on. Enforced by a test that poisons the `socket` module
and runs a full scan — and that first asserts its own trap fires, so it cannot pass
vacuously. Every demo domain is under the reserved `.test` TLD, which cannot resolve.

**Demo QR format.** Certificate ids contain slashes, so the id is percent-encoded into a
single path segment:
`https://verify.nbta-demo.test/c/NBTA%2FDEMO%2F2024%2F00417`. A `cert` or `id` query
parameter is the configured alternative. Path patterns live in config.

**Two image sources per PDF page, both required.** The rendered page catches a QR drawn as
vector paths (no image exists to extract); embedded images at native resolution catch a QR
pasted in above our render DPI, where rendering would blur it away. One code found twice is
deduplicated to one detection by (payload, page), with the second sighting kept in
`duplicate_sources`.

**Allowlist matching is equality or a dot boundary**, never `contains` or `endswith` on the
raw string: those accept `verify.nbta-demo.test.evil.test` and `official.com.evil.test`.
Both shapes have fixtures.

**Mixed-script detection is per host label**, not per host, because every internationalised
domain pairs a non-Latin name with an ASCII TLD and a whole-host comparison would flag all
of them. Within-label mixing is the actual homoglyph pattern.

**A skipped comparison is recorded, never silent.** No id in the payload, no validated
`certificate_number` from step 2, or no step 2 result at all each produce an entry in
`comparisons_skipped` with its reason. The certificate id comparison reuses step 2's own
`normalise()`, passed in rather than reimplemented.

**Dependency note.** `opencv-python`, not `opencv-python-headless`, even though headless is
the better fit for a server: `rapidocr-onnxruntime` from step 1 requires `opencv-python` by
name, and both distributions install the same `cv2` package. With both installed, whichever
landed last owns `cv2` and removing it deletes `cv2` while the other still claims to supply
it. One flavour only. Resolved versions on Python 3.10 / Windows: `opencv-python 5.0.0.93`,
`zxing-cpp 3.1.1`, `segno 1.6.6` (test-only).

Tests: `qr`-marked tests decode real images; deselect with `-m "not qr"`.

### Step 4 decisions

**The issuer URL comes only from config.** `config/issuer_verification.yaml` holds
`issuer.base_url`, and nothing else can set a request destination. A URL in a QR code or on
a document is evidence to be compared, never an address to contact — if a submitted
document could name the service that vouches for it, whoever prepared the document picks
their own verifier and the check becomes theatre. Enforced structurally: `IssuerClient`
takes the base URL at construction and `lookup()` accepts only a certificate id, so no
caller can redirect a request. Two tests assert it, one on the URLs attempted and one on
the method signature itself.

**Status mapping** — code picks the reason key, `status_mapping` in config names the status.
Precedence is code (logic); names are config (policy). Only the six taxonomy values are
accepted; an unknown value is rejected at load.

| Reason key | Default status |
|---|---|
| `no_certificate_id` | `manual_review_required` |
| `source_unavailable` | `source_unavailable` |
| `malformed_response` | `manual_review_required` |
| `not_found` | `manual_review_required` |
| `issuer_pending` | `verification_pending` |
| `issuer_status_revoked` | `mismatch_detected` |
| `issuer_status_suspended` | `mismatch_detected` |
| `field_mismatch` | `mismatch_detected` |
| `expired_per_issuer` | `expired` |
| `all_fields_match` | `verified` |

First match wins, top to bottom. A **not-found routes to a human, never to a conclusion**:
the issuer's records can be incomplete. revoked/suspended outranks a field mismatch (the
issuer actively withdrew the certificate). A mismatch outranks expiry (expiry is a fact
about dates; a mismatch is a question about the document). With two lookups, the document
status is the most severe, by `status_severity`.

**Which id is looked up.** The step 2 number is primary; a QR id is a fallback marked
`lower` trust, because the QR travels with the document and is easier to replace than the
printed text. If both exist and differ, both are looked up and reported separately —
collapsing them would discard the discrepancy. A QR id resolving to a real record held by
another institution gets its own strong finding.

**A null submitted field is never a mismatch**, only `not_compared` with the reason. Step 2
returns null both for an absent field and for one that failed the snippet guard; neither
conflicts with the issuer. Only `validated: true` step 2 values are read at all.

**Programmes are split on commas before comparison.** Step 2 may read one comma-joined
programme line as two items while the registry holds it as one string. Without the split,
every real extraction would report a programme mismatch. Grades, by contrast, compare
exactly: `A+` and `A++` are different outcomes.

**The registry is seeded deterministically** (`python -m app.mock_issuer.seed`, fixed seed):
19 records across 12 fictional institutions, all `.test` domains. A demo that reshuffles its
own data cannot be reasoned about, and the eval set needs the same registry every run or
detection rates are not comparable. Fault injection is inert unless explicitly set.

**Dependency note on `httpx2`.** It is the httpx 2.x line, published under a new
distribution name by the pydantic org (same author as httpx); the old `httpx` distribution
is frozen at 0.28.1. Not a typo and not a typosquat, and it imports as `httpx2`, which is
what our code imports. Starlette 1.7's `TestClient` supports it natively. Resolved on
Python 3.10 / Windows: `fastapi 0.162.0`, `uvicorn 0.54.0` (with `starlette 1.7.0`,
`pydantic 2.14.1`).

Step 4 tests are all fast: they drive the FastAPI app in-process via `TestClient`, so no
port is opened and no sleep is actually performed — the retry schedule is asserted exactly
rather than timed.
