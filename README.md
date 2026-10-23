# AICTE Fraud-Signal Detection

**Smart India Hackathon 2026 — prototype**

A decision-support tool that performs a first-pass scrutiny of AICTE Extension
of Approval (EoA) applications and surfaces **possible** fraud signals, each
with its evidence, for a human official to review.

It does not approve or reject anything. Every output is a preliminary
indicator attached to the evidence it rests on, and a risk level only decides
whose application a reviewer should open first.

> **All data in this repository is synthetic.** Every institution, accrediting
> body, certificate and faculty list is invented, and every domain uses the
> reserved `.test` TLD, which cannot resolve to a real site. Nothing here is
> derived from a real accreditation record, and no real government database is
> contacted. See [Demo data](#demo-data-and-safety-rules).

---

## What it refuses to do

These are design constraints, not missing features. They are the reason the
output is worth reading.

| | |
|---|---|
| **No automated decisions.** | Nothing in the system approves, rejects or declares a document forged. It flags, explains, and routes to a person. |
| **The LLM reads; code decides.** | A language model is used only to pull structured fields out of messy text and to write plain-language explanations. It never decides authenticity, pass/fail, a risk level or a verification status. |
| **No unsourced values.** | Every value a model returns must be traceable to the source document, or deterministic code discards it. |
| **No restricted databases.** | Real AICTE and issuer systems are not touched. Both are mocked. |
| **Never fetch a URL from a document.** | A URL in a QR code is evidence to be compared, never an address to contact. Enforced by test. |
| **Careful language.** | Findings say "mismatch detected" and "requires manual verification". Never "fake", "forged" or "deepfake detected". Asserted by test, not left to discipline. |

## Scope: four detectors

1. **Certificate authenticity** — field extraction from the certificate; QR
   decode and cross-check; domain allowlist; issuer lookup; PDF signature
   (post-signing modification only); duplicate detection.
2. **Faculty count consistency** — declared vs. college website vs. mock AICTE
   records, per department, with configurable tolerance.
3. **Photo authenticity** (labs/classrooms) — ranked by how much the signal is
   worth: image reuse, then EXIF, then manipulation checks, then AI-generation
   as a low-confidence hint only.
4. **Risk scoring** — per-detector sub-scores and one application-level level,
   with configurable weights. **Prioritises review order only.**

Out of scope: validating every parameter of every document, any automated
approval path, and access to real government databases.

---

## Build status

| Step | Module | State |
|---|---|---|
| 1. Document text extraction | [`app/extraction`](backend/app/extraction/) | **Done** |
| 2. Certificate field extraction (LLM) | [`app/certificates`](backend/app/certificates/), [`app/llm`](backend/app/llm/) | **Done** |
| 3. QR detection and cross-check | [`app/qr`](backend/app/qr/) | **Done** |
| 4. Mock issuer + verification client | [`app/mock_issuer`](backend/app/mock_issuer/), [`app/issuer`](backend/app/issuer/) | **Done** |
| 5. Duplicate detection, PDF signatures | — | Not started |
| 6. Faculty and photo detectors | — | Not started |
| 7. Risk scoring, API, dashboard | — | Not started |

213 tests pass. There is no database, no application-level HTTP API and no
dashboard yet — the four finished detectors run from the command line and emit
JSON, which is what the later steps will consume.

---

## The pipeline so far

```
PDF / image
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│ 1. Text extraction          app/extraction              │
│    PDF text layer, scored; OCR fallback (RapidOCR)      │
└──────────────────────────────────────────────────────────┘
    │ per-page text + the decision and its measurements
    ▼
┌──────────────────────────────────────────────────────────┐
│ 2. Field extraction         app/certificates + app/llm  │
│    9 fields, each checked back against the source text  │
└──────────────────────────────────────────────────────────┘
    │ validated fields (certificate_number, dates, …)
    ├────────────────────────────┐
    ▼                            ▼
┌───────────────────────┐   ┌──────────────────────────────┐
│ 3. QR cross-check     │   │ 4. Issuer verification        │
│    app/qr             │──▶│    app/issuer                 │
│    8 finding types    │   │    ↔ app/mock_issuer (FastAPI)│
└───────────────────────┘   └──────────────────────────────┘
         │                            │
         └──────────┬─────────────────┘
                    ▼
        findings + evidence + audit trail
        (risk scoring: step 7, not yet built)
```

Each stage returns errors **as data** — a status and a reason — rather than
raising. A corrupt file, an unreachable issuer and a certificate with no grade
printed on it are all ordinary results that a reviewer can read.

---

## Quickstart

### Requirements

**Python 3.10 specifically.** This is a hard constraint, not a preference:
`rapidocr-onnxruntime` declares `Requires-Python <3.13`, so a newer interpreter
cannot install the OCR engine. See [CLAUDE.md](CLAUDE.md) for the full
reasoning.

### Setup

```bash
# Create the venv with a 3.10 interpreter explicitly
py -3.10 -m venv .venv          # Windows
python3.10 -m venv .venv        # macOS / Linux

.venv/Scripts/activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r backend/requirements.txt
```

### An API key, for step 2 only

Step 2 is the one stage that calls a language model. Put a key in `.env` at the
repository root (gitignored):

```ini
GROQ_API_KEY="gsk_..."
```

Groq's free tier is the default. Any OpenAI-compatible endpoint works by
editing [`config/certificate_extraction.yaml`](config/certificate_extraction.yaml)
— no code change. Steps 1, 3 and 4 need no key at all.

### Run the detectors

All commands run from `backend/`.

```bash
# 1 — text extraction
python -m app.extraction <file.pdf|jpg|png>

# 2 — certificate fields (needs an API key)
python -m app.certificates <file.pdf> --no-raw > step2.json

# 3 — QR detection and cross-check (never contacts any URL)
python -m app.qr <file.pdf> --extraction step2.json > step3.json

# 4 — issuer verification, with the mock issuer in-process
python -m app.issuer step2.json --qr step3.json --in-process
```

Every command prints JSON. `--compact` for one line.

To run the mock issuer as a real service instead of in-process:

```bash
python -m app.mock_issuer.seed      # regenerate the registry (deterministic)
python -m app.mock_issuer           # serves on 127.0.0.1:8088
python -m app.issuer step2.json     # uses issuer.base_url from config
```

### Tests

```bash
cd backend
python -m pytest                                        # 213 tests
python -m pytest -m "not ocr and not llm and not qr"    # 166, ~2s
```

| Marker | What it costs |
|---|---|
| *(none)* | Fast. No network, no models, no API calls. |
| `ocr` | Loads the real OCR models. Slower. |
| `qr` | Decodes real images with OpenCV and zxing-cpp. |
| `llm` | **Calls a real API and costs money.** Skipped when no key is configured; *fails* when a key is present but rejected, so a broken credential cannot hide. |

Test fixtures are generated, not committed:

```bash
python -m tests.make_fixtures        # certificates: PDFs, scans, corrupt files
python -m tests.make_qr_fixtures     # QR cases: lookalike domains, rotated scans, …
```

---

## How each step works

Each module has a README covering its design decisions and the traps it avoids.

### 1. Text extraction — [`app/extraction/README.md`](backend/app/extraction/README.md)

Per-page text from a PDF's text layer or, when that is not usable, from OCR.

The text layer is **scored, not merely checked for presence** — character
count, word count and the ratio of readable characters, all configurable. A
tampered or badly generated PDF can carry a text layer full of garbage, and a
simple "does it have text?" test would trust it. Each page records which route
was taken, the measurements behind that choice, and the reason in words.

OCR is RapidOCR behind a one-method interface, chosen over Tesseract because it
pip-installs with bundled models and needs no system binary on a demo machine.

### 2. Certificate fields — [`app/certificates`](backend/app/certificates/)

Nine fields via an LLM: institution, certificate number, issuing body,
programme, grade, three dates, signatory.

**Every non-null value must be traceable to the source text, or code discards
it.** Two tiers:

- **Snippet check.** The model must quote the text it read the value from, and
  that quote must appear in the page, tolerant of whitespace, case and small
  OCR differences. Otherwise the value becomes `null` with reason
  `snippet_not_in_source`.
- **Exact-value check.** For identifiers — certificate number, grade, the three
  dates — the value itself must also appear verbatim. Fuzzy matching alone is
  unsafe here: a certificate number altered by one digit scores ~0.97 against
  the original line and sails through. Failure gives `value_not_in_source`.

The claimed value is kept for the reviewer either way. Date parsing to ISO,
the validity-window report and the `validated` flag are all deterministic.

A missing field is a normal, successful result.

### 3. QR cross-check — [`app/qr/README.md`](backend/app/qr/README.md)

Eight finding types: off-allowlist domain, deceptive URL pattern, insecure
scheme, certificate-id mismatch, multiple distinct QRs, none found, unreadable,
and PDF-link-vs-QR host mismatch.

**Nothing fetches, opens, resolves or follows a QR URL.** A payload is a string
chosen by whoever submitted the document. A test poisons the `socket` module,
runs a full scan over the payloads that most invite a lookup, and first asserts
its own trap fires so it cannot pass vacuously.

Two details that are easy to get wrong and are tested directly:

- **Allowlist matching is equality or a dot boundary.** A bare `endswith`
  accepts `verify.nbta-demo.test.evil.test`; `contains` accepts anything.
- **Mixed-script detection is per host label**, not per host. Every
  internationalised domain pairs a non-Latin name with an ASCII TLD, so a
  whole-host comparison would flag every legitimate IDN in the world.

PDF pages are both rendered *and* mined for embedded images, because each
catches a case the other misses: a QR drawn as vector paths has no image to
extract, and a QR pasted in above the render DPI would be blurred away.

### 4. Issuer verification — [`app/issuer/README.md`](backend/app/issuer/README.md)

Asks a mock issuer whether the certificate exists, compares the record field by
field, and sets one of the six fixed statuses.

**The issuer URL comes from config and from nowhere else.** This is the
load-bearing rule: if a submitted document could name the service that vouches
for it, whoever prepared the document picks their own verifier and the check
becomes theatre. Enforced structurally — the client takes its base URL at
construction and the lookup method accepts only a certificate id, so no caller
can redirect a request.

Other decisions worth knowing:

- **A null submitted field is never a mismatch**, only `not_compared` with the
  reason. Treating missing evidence as adverse evidence would flag every
  poorly scanned document.
- **A not-found routes to a human, not to a conclusion.** The issuer's records
  can be incomplete.
- **Programmes are compared as sets, split on commas first.** A certificate
  printing one comma-joined programme line may be read as two items while the
  registry holds one string. Grades, by contrast, compare exactly: `A+` and
  `A++` are different outcomes.
- Retries cover transport errors, 429 and 5xx. A 404 and a malformed body are
  answers, and repeating the request cannot change either.

---

## Verification status taxonomy

A closed set, defined once in
[`app/core/verification.py`](backend/app/core/verification.py) and **set by code
logic only**. No LLM ever chooses one.

`verified` · `mismatch_detected` · `expired` · `verification_pending` ·
`source_unavailable` · `manual_review_required`

Which outcome maps to which status is a table in config, so an organisation can
route (say) a suspended certificate to manual review instead of reporting a
mismatch, without a code change. Precedence between outcomes stays in code,
because it is logic. A status outside the six is rejected at load.

## Configuration

No threshold, weight, allowlist or tolerance is hard-coded. Each file carries a
`config_version` that is **stamped onto every result**, so an evaluation can
always be traced to the rules that produced it.

| File | Governs |
|---|---|
| [`config/extraction.yaml`](config/extraction.yaml) | render DPI, page caps, text-layer scoring thresholds, OCR confidence |
| [`config/certificate_extraction.yaml`](config/certificate_extraction.yaml) | LLM provider and model, prompt version, retries, snippet/exact-value rules, date handling |
| [`config/qr_checks.yaml`](config/qr_checks.yaml) | decoder order, preprocessing ladder, domain allowlist, shortener list, finding strengths |
| [`config/issuer_verification.yaml`](config/issuer_verification.yaml) | issuer base URL, timeouts and retries, name/programme/grade comparison rules, status mapping |

`SIH_CONFIG_DIR` points the loader at a different directory — useful for an
offline demo or a reproducible evaluation run.

## Audit trail

Every result carries an audit block, because a flag a reviewer cannot
interrogate is not worth acting on. Depending on the stage it records: the
config and prompt versions, a SHA-256 of the input, the provider and model,
token counts, decoder versions, the issuer URL and the raw response, every HTTP
attempt with its latency and retry wait, and a UTC timestamp.

## Demo data and safety rules

- Every institution, accrediting body, certificate and faculty list is
  **invented**. The registry is built from a fixed seed
  (`python -m app.mock_issuer.seed`) so the demo and the evaluation set are
  reproducible — a demo that reshuffles its own data cannot be reasoned about.
- Every domain uses the reserved **`.test`** TLD, which by specification cannot
  resolve. A test asserts no real TLD appears in the registry.
- **No URL from any submitted document is ever contacted.**
- The mock issuer binds to **localhost only**, and its failure injection is
  inert unless explicitly switched on: a mock that failed by default would make
  a passing test meaningless.
- Institutional and personal data is treated as sensitive. The prototype keeps
  roles (officer / senior reviewer / admin) in the audit record as an actor
  field; login is deliberately out of scope for the demo.

## Evaluation plan

A synthetic test set with deliberately injected frauds — duplicated
certificate, certificate number altered by one digit, QR pointing at a
lookalike domain, inflated faculty counts, reused lab photo, AI-generated
classroom image — reporting **detection rate per fraud type**, the
**false-positive rate**, and **average processing time per application**.

The false-positive rate matters as much as the detection rate here. A detector
that flags everything prioritises nothing, and a reviewer who learns to ignore
a category of flag is worse off than one who never saw it.

## Repository layout

```
backend/
  app/
    core/           config loading, .env, the status taxonomy
    extraction/     step 1: text layer scoring + OCR
    certificates/   step 2: field extraction, snippet guard, date parsing
    llm/            the provider seam, prompts, versioned prompt files
    qr/             step 3: decoders, preprocessing, payload parsing, checks
    mock_issuer/    the synthetic registry, seed script, FastAPI stand-in
    issuer/         step 4: client, comparison, status mapping
  tests/            fixtures are generated by script, not committed
config/             all thresholds, allowlists and mappings
data/demo/          the seeded issuer registry
CLAUDE.md           design decisions, constraints, and why each was made
```

## Stack

Python 3.10 · PyMuPDF · RapidOCR · OpenCV + zxing-cpp · FastAPI · PyYAML ·
pytest. SQLite via SQLAlchemy and a React dashboard are planned for step 7.

The LLM layer is provider-agnostic behind a single `complete(prompt) -> text`
method, written against the OpenAI-compatible REST shape rather than a vendor
SDK. Free tiers come and go, so switching between Groq, Mistral, OpenRouter,
Together or a local server is a config edit.

---

## Further reading

- **[CLAUDE.md](CLAUDE.md)** — the running record of design decisions, what was
  confirmed when, and why. Start here to understand *why* the code looks like
  this.
- Module READMEs: [extraction](backend/app/extraction/README.md) ·
  [qr](backend/app/qr/README.md) · [issuer](backend/app/issuer/README.md)
