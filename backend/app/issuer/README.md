# Issuer verification (step 4)

Asks a mock issuer whether a certificate exists, compares the issuer's record
with the submitted details field by field, and sets one of the six fixed
verification statuses. No LLM is involved anywhere in this module.

```
verify_certificate(submitted, qr_certificate_id=None)
  -> IssuerVerificationResult(status, status_reasons, lookups, findings, audit)
```

## The destination rule

**The issuer URL comes from `config/issuer_verification.yaml` and from nowhere
else.** A URL in a QR code or printed on a document is evidence to be compared,
never an address to contact.

This is the load-bearing rule of the whole step. If a submitted document could
name the service that vouches for it, whoever prepared the document chooses
their own verifier and the check becomes theatre.

It is enforced structurally rather than by convention: `IssuerClient` takes its
base URL at construction and `lookup()` accepts only a certificate id, so there
is no argument through which a caller could redirect a request.
`test_the_qr_host_is_never_contacted` asserts every attempted URL starts with
the configured base, and `test_the_client_offers_no_per_request_destination_override`
asserts the signature itself.

## Which id gets looked up

| Situation | Lookups | Trust |
|---|---|---|
| Step 2 number present | that number | `normal` |
| Step 2 number null, QR id present | the QR id | `lower` |
| Both present and different | **both**, reported separately | `normal`, `lower` |
| Neither | none | `manual_review_required` |

The printed number is primary because it has already survived step 2's snippet
and exact-value guards. A QR id is a fallback: the QR travels with the document
and is easier to replace than the printed text, so a record found only by the
QR id is weaker evidence than the same record found by the number. That is what
the `lower` trust label records.

When both exist and differ, collapsing them into one answer would discard the
discrepancy — which is the most interesting thing on the page. A QR id that
resolves to a real record held by a *different institution* gets its own strong
finding: a valid certificate, just not this document's.

## Status mapping

Code decides which reason key applies; the config table decides what the key is
called. Precedence lives in code (it is logic); the names live in config (they
are policy). Only the six taxonomy values are accepted, and an unknown value in
config is rejected at load rather than silently passed through.

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

Tested top to bottom; first match wins. Three judgements in that order:

- An absent or unreadable answer is decided before anything about the record,
  because without an answer there is no record to reason about.
- revoked/suspended outranks a field mismatch: the issuer actively withdrew the
  certificate, a stronger statement than a disagreeing field.
- A mismatch outranks expiry. Expiry is a fact about dates; a mismatch is a
  question about the document.

A **not-found is `manual_review_required`, not a mismatch.** The issuer's
records can be incomplete, so an absent record routes to a human instead of
becoming a conclusion about the document.

With two lookups, the document-level status is the most severe of them, by the
`status_severity` order in config.

## A null submitted field is never a mismatch

Step 2 returns null both when a field is absent from the certificate and when
its value failed the snippet guard. Neither conflicts with the issuer, so both
produce `not_compared` with the reason recorded. Treating missing evidence as
adverse evidence would flag every poorly scanned document.

Only `validated: true` values are read out of a step 2 result, for the same
reason: an unvalidated value was already rejected as untraceable, and comparing
it would manufacture a mismatch out of our own uncertainty.

## Comparison rules per field

- **Names** (`institution_name`, `issuing_body`): normalised for case,
  punctuation, an abbreviations table and filler words, then compared by
  similarity against `min_similarity`. "Konkan Coast Inst. of Engg." and
  "Konkan Coast Institute of Engineering" are the same college; two genuinely
  different colleges stay different.
- **Programmes**: compared as sets, and **split on commas first**. A certificate
  printing `Bachelor of Technology, Computer Science and Engineering` as one
  line may be read by step 2 as two items while the registry holds it as one
  string. Splitting both sides makes the comparison answer whether these are
  the same programmes, rather than how the text was punctuated. Without this,
  every real extraction would report a programme mismatch.
- **Grade**: exact after case folding. `A+` and `A++` are different outcomes, so
  similarity has no place here.
- **Dates**: parsed to ISO and compared as dates, so formatting is not a
  mismatch.

## Retries

Transport errors, 429 and 5xx are retried: conditions where the same request
could legitimately succeed. A **404 and a malformed body are not** — they are
answers, and repeating the request cannot change either.

`Retry-After` is honoured and capped by `max_retry_after_seconds`, so a server
asking for five minutes surfaces as `source_unavailable` for a reviewer rather
than stalling the run.

The attempt history travels on the exception, so a failed lookup still records
how many times we tried and what came back. "Source unavailable" is only
meaningful next to that.

## Running it

```bash
# Regenerate the registry (deterministic; same output every run)
python -m app.mock_issuer.seed

# Serve the mock issuer on localhost:8088
python -m app.mock_issuer

# Verify, against a running service
python -m app.issuer step2.json [--qr step3.json] [--as-of 2026-01-15]

# Verify, with the issuer in-process: no server, no port
python -m app.issuer step2.json --in-process
```

Exit code is 1 only when nothing could be checked (`source_unavailable`). A
mismatch is a successful run that found something.

## The mock issuer

`app/mock_issuer` stands in for an accrediting body's lookup API, because the
real thing is a restricted government database we do not have and must not
pretend to have. Everything it serves is synthetic, every domain is under the
reserved `.test` TLD, and the registry is built from a fixed seed so the demo
and the evaluation set are reproducible.

Failure injection (`FaultInjection`) covers timeouts, 429 with `Retry-After`,
500 and malformed bodies. **Every switch is inert unless explicitly set**: a
mock that failed by default would make a passing test meaningless.

Certificate ids contain slashes, so the id is taken from a query parameter. A
percent-encoded path route is also provided and tested, including the
doubly-encoded case, because a real issuer could expose either shape.
