# Evaluation dataset and runner (step 7)

```bash
python -m evaluation.generate [--seed N] [--out DIR] [--only CASE,CASE] [--quiet]
python -m evaluation.run [--mode oracle|live] [--split dev|holdout|all]
                         [--limit N] [--provider NAME] [--model NAME]
                         [--shuffle-seed N] [--no-cache] [--out DIR]
python -m evaluation.distances [--kind dhash] [--size 16] [--json]
```

Config: `config/eval_generation.yaml`, `config/eval_run.yaml`.
Output: `data/eval/` — gitignored in full, reproducible from the seed.

## The independence rule

**No module here imports or calls a detector, a validator or the pipeline in
order to decide what a case is.** Labels come from the recipe that built each
document: the generator knows it edited a digit because it edited the digit.

`run.py` is the one module that touches `app`, and only in two ways — it calls
`process_document()` to produce results, and it reads results back out of the
database to compare against labels. It never asks a detector what a document
is. Two tests enforce the boundary by scanning this package's imports.

Without that rule the measurement would be circular: the labels would be the
detectors' own opinion, and a detector that was confidently wrong would score
perfectly.

## What gets generated

80 applications, one certificate document each, from `config/eval_generation.yaml`:

- **12 fictional institutions**, 5 fictional issuing bodies, all domains under
  the reserved `.test` TLD. `world.json` is written separately so the faculty
  and photo steps can extend the *same* world — a certificate detector measured
  against twelve institutions and a faculty detector measured against a
  different twelve cannot be combined into an application-level number.
- **5 layouts** differing in wording, table-versus-prose, fonts, colour,
  abstract seal, watermark and QR placement. One has a Devanagari heading; one
  issuing body prints no QR at all.
- **4 forms**: digital PDF with a text layer, scanned-style PDF (image only,
  forces OCR), JPG scan, and signed PDF.
- **4 noise levels** recorded per document, so the QR decode rate can be
  reported *by* noise level rather than as an average that hides where the
  failures are.

Nothing draws a real logo, emblem or seal. The seals are a circle, a hexagon
and a square with invented initials.

## Labels

`data/eval/labels.json`, one entry per application. The fields that matter:

| Field | Meaning |
|---|---|
| `printed_fields` | what is actually on the page — the ground truth for extraction |
| `qr_payload` | what is actually encoded in the QR |
| `expected` | finding groups a correct run should satisfy |
| `must_not` | finding types that must not fire at medium or above |
| `expected_issuer_status` | where the case defines one |
| `pair_application_id` | the other half of a duplicate pair |
| `processing_order` | authoritative; duplicates come after their sources |

`expected` is a list of **groups**, and a group is satisfied when *any* of its
listed types fires at or above its own `min_strength`. There is usually more
than one honest way for a detector to notice the same thing, and demanding one
particular type would turn a design change into a false miss. A group marked
`either_side` may also be satisfied by a finding on the paired document,
because which run discovers a relationship depends on processing order and the
measurement must not.

`must_not` is the half that matters more on a decision-support system: a
finding at medium or strong against an honest institution costs a real review.

## Dev and holdout

Deterministic from the seed and **stratified by case type** — a random split
over eighty applications would leave some case types absent from holdout, and a
holdout number that does not cover a case type says nothing about it.

> **Tune on dev. Read holdout once, at the end.** The runner prints this before
> and after every run. Nothing in the code can enforce it: changing a threshold
> after looking at holdout turns the holdout number into another dev number.

## Oracle mode

The stub provider returns exactly the printed fields, with `source_snippet`
values grounded in the **real extracted page text**. That isolates the
question: how well do the deterministic detectors work when the model is not
the weak link?

Two things it deliberately does not do:

- **It does not report extraction accuracy.** The answers are the ground truth
  by construction, so the figure would be 100% and would mean nothing.
  Extraction is measured in live mode only.
- **It does not bypass the snippet guard.** On a badly OCR'd scan the printed
  value genuinely is not in the page text, the snippet does not match, and step
  2 discards the field exactly as it would for a real model. The guard is part
  of the deterministic system under test, not part of the model.

## Determinism

The same seed must give byte-identical output, or this week's detection rate
cannot be compared with last week's — and a rate you cannot compare is not a
measurement. Three things had to be fixed:

1. PyMuPDF writes a **random trailer `/ID`** on every save, and regenerates its
   second element even when the first is pinned. Normalised to zeros at equal
   length.
2. **Uncompressed output is nondeterministic** — about 1 render in 60 differed
   with `deflate=False`, 0 in 60 with `deflate=True`. Documents are therefore
   compressed. Only `signed_then_tampered` renders uncompressed, because it has
   to edit a printed digit inside the signed byte range and that needs the text
   literally present in the bytes.
3. **MuPDF is occasionally nondeterministic anyway**, for any layout once an
   image is involved — roughly 1 render in 25. `templates.stable_bytes` renders
   until two consecutive attempts agree, pinning the output to the common
   answer at the cost of one extra render per document. It raises rather than
   giving up: a generator that silently produced a non-reproducible dataset
   would undermine every number taken from it.

**Signed documents are the one exception**, stated rather than hidden. A
signature embeds a signing time and a freshly generated throwaway key, so
signed bytes differ between runs. Manifest and labels record
`document_sha256: null` and `reproducible_bytes: false` for them, and the
manifest names them in `non_reproducible_documents` with the reason. The
manifest and labels themselves are byte-identical.

## Keys

The signing key is a throwaway RSA key generated **in memory on each run** and
never written to disk. A test asserts that generation writes no file with a
key-ish extension *and* that no PEM header appears in any generated byte
stream — the second matters more, because a key accidentally embedded in a JSON
file would not have a `.pem` name.

## The issuer registry, and the allowlist

The evaluation writes its **own** registry at `data/eval/issuer_registry.json`.
`data/demo/` is never touched: it backs step 4's tests, and a generator that
mutated it would silently change what those tests mean.

The runner also **widens the QR allowlist** to this world's five issuing
bodies, derived from `world.json`. That is policy config, not a detector
change: without it, every genuine certificate from four of five bodies is
reported as pointing at an unapproved domain. Each host is added with
`allow_subdomains: false`, so step 3's equality-or-dot-boundary rule still
rejects the lookalike, userinfo and punycode fraud shapes — and a test asserts
exactly that.

## Metrics

All in `metrics.py` as pure functions from (labels, results) to numbers, so
they can be tested on hand-built sets with hand-computed answers. A bug in the
harness quietly moves a detection rate and nothing else would notice.

- **flagged** — at least one finding at or above `flagged_min_strength`
  (medium). Weak findings are informational by design, so counting them would
  make precision a measure of how talkative the detectors are.
- **detected** — every expected group satisfied.
- **false positive** — a genuine document flagged, or a `must_not` type firing
  at medium or above. The second is more useful: it names which finding was
  wrong.

**Read the precision breakdown with the headline number.** As specified,
precision is "flagged versus `is_fraud`", which charges as a false positive
every genuine document the system was *right* to surface — a certificate the
issuer revoked, suspended or let expire is honest and still needs a reviewer.
So `document_level` also reports `genuine_flagged_as_expected`,
`genuine_flagged_unexpectedly` and
`precision_counting_expected_flags_as_correct`. Without them the headline
number invites someone to "improve" precision by suppressing the issuer-state
findings, which would be strictly worse.

## Threshold re-measurement

`python -m evaluation.distances` measures the perceptual-hash distance
distributions across the five layouts, using the production settings, and says
whether `near_duplicate_max_distance` still separates what it should.

**Report only.** It never writes config — a threshold that moved itself would
make every earlier run incomparable.

The verdict is two one-sided checks rather than one accuracy figure, because
the two errors are not interchangeable: a missed resubmission costs a reviewer
nothing to catch later, and an unrelated document flagged as a duplicate costs
an honest institution a review.

## No network

The mock issuer runs in-process through Starlette's `TestClient`; no port is
opened and nothing outside the process is contacted. Oracle mode makes no model
call at all. A test arms a socket trap over a short run — and asserts the trap
fires before trusting it.
