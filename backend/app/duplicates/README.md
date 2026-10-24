# Cross-application duplicate detection (step 6, part B)

```
detect_duplicates(session, document_id, run_id, config,
                  certificate_number, qr_certificate_ids, qr_raw_payloads)
    -> DuplicateCheckResult
cross_references_for_document(session, document_id) -> list[dict]
```

```bash
python -m app.duplicates <document_id>
python -m app.duplicates --relationships <document_id>
```

Four comparisons, cheapest and most certain first:

1. **Exact file** — the same `file_sha256` under another document.
2. **Certificate number** — the same `validated` number from step 2.
3. **QR payload** — the same QR certificate id or raw payload.
4. **Near-duplicate page image** — perceptual hash of the rendered first page,
   compared by Hamming distance.

Reverse image search is **internal only**: the corpus is the documents already
in our own database. No external image service is contacted — a confirmed
decision, with a socket-trap test that would fail if someone added one.

## The measured distances

The threshold is not a guess. Measured on synthetic certificate pages, dhash at
`hash_size: 16` (256 bits), rendered at 150 DPI:

| Distance | Case |
|---|---|
| 0 | identical content, second file |
| 1 | same document, JPEG q70 |
| 3 | same document, JPEG q40 / downscaled ×2 |
| 1 | **same certificate, one digit of the number edited** |
| 4 | same document, rotated 0.5° |
| 6 | same document, rotated 1° |
| 2 – 6 | **same template, different institution** |
| 8 | same document, rotated 2° |
| 12 | same document, rotated 3° |
| 23 | same document, rotated 5° |
| 22 – 26 | unrelated documents |

`near_duplicate_max_distance: 12` catches every re-encode, rescale and rotation
up to 3°, and excludes every unrelated document (closest: 22). A 5°-rotated
scan is missed; that is accepted, because a rotated *photograph* is the photo
detector's problem and a resubmitted file is caught by the exact hash and the
certificate number regardless.

`tests/make_duplicate_fixtures.py --` running the module prints this table, and
a test asserts the margin has not narrowed, so a change to `compare_kind`,
`hash_size` or `render_dpi` fails loudly instead of quietly leaving a threshold
that no longer means what its comment says.

### Two results that shaped the design

**A 64-bit hash is useless on document pages.** At `hash_size: 8` an
*unrelated* document sat 8 bits from a certificate, and a certificate from a
different institution sat **0** bits away — indistinguishable from the same
file. A page of sparse black text on white is almost entirely white, and the
hash is dominated by that. 256 bits is the smallest size with usable
separation.

**dhash beats phash here**, which is the opposite of the usual advice. phash
keeps low-frequency DCT coefficients, and a text page has almost no
low-frequency structure, so small changes dominate: a 1° rotation moved phash
22 bits against dhash's 6, and a 3° rotation moved phash 58 bits — further than
an unrelated document. Both are computed and stored; `compare_kind` picks one.

### The finding no threshold can make

Look at the table again. A **one-digit edit** sits 1 bit away. A **template
sibling** sits 2–6 bits away. A genuine **re-save** sits 1–3 bits away. These
overlap completely. No threshold separates them, at any hash size we measured.

So a near-duplicate page image is **never a strong finding**, in any context.
That is a measurement, not caution, and a test asserts it against the shipped
config. The strong signals come from the hash, the number and the QR payload —
things that are equal or not.

It also means the image check cannot tell you a number was altered. It will say
"these are the same document"; the certificate-number comparison is what says
otherwise, and the number is what decides.

## Severity depends on context

The same observation means different things depending on who else holds the
thing observed. `config/duplicate_checks.yaml` keys strength by check **and**
context:

| Check | Context | Strength |
|---|---|---|
| `duplicate_file_exact` | other institution | **strong** |
| | same institution | weak — a resubmission is normal |
| `certificate_number_reuse` | other institution | **strong** |
| | same institution | weak |
| `qr_payload_reuse` | other institution | **strong** |
| | same institution | weak |
| `near_duplicate_page_image` | numbers differ | weak — most likely one issuer's template |
| | same number, other institution | medium — corroborates the number finding |
| | same number, same institution | weak |
| | number unknown on either side | weak |

An unlisted combination defaults to **weak**: a missing config entry is our
mistake and must not manufacture a strong finding against an institution.

"same number, other institution" is medium rather than strong because
`certificate_number_reuse` has already fired strongly on the number itself. The
image similarity adds weight to that one finding; it is not a second
independent accusation.

## Self-exclusion

A document is never a duplicate of its own earlier runs. Matches are found by
**document id**, never by run, so a second check run over the same document
cannot see its own stored hashes or numbers in the corpus.

## Order independence and `cross_references`

The set of relationships must not depend on which application was processed
first. That requirement collides with another: **runs are never overwritten**.
When B arrives and matches A, the fact belongs to both documents — but A's run
finished before B existed.

Resolved by storing the relationship on neither run:

```
cross_references(document_a_id, document_b_id, kind, same_institution,
                 evidence, discovered_in_run_id)
```

- The pair is **normalised** so the lower document id is always `document_a_id`.
- A unique constraint on `(document_a_id, document_b_id, kind)` means
  rediscovering a relationship adds nothing, and `discovered_in_run_id` keeps
  pointing at the run that saw it *first*.
- `cross_references_for_document()` matches on **either** column, so the
  relationship appears on both sides of `get_application_report`.

So A→B and B→A produce the identical row, and A's report shows the relationship
without a byte of A's finished run being touched. A test processes two
documents in both orders, on two fresh databases, and compares the final sets.

## `perceptual_hashes`

```
perceptual_hashes(document_id, run_id, page, hash_kind, hash_value, hash_size)
```

Every configured kind is stored even though only one is compared, because
re-hashing the corpus later would otherwise mean re-rendering every stored
document. `hash_size` is stored alongside the value because comparing two
different widths is meaningless — `hamming_distance` raises rather than
padding, since a short padded distance is exactly what this system reads as
"possibly the same document".

Hashing lives in `app/similarity` rather than here, so the photo detector can
reuse it for lab and classroom images.

## Nulls are never matches

Only `validated: true` certificate numbers are read. Step 2 returns null both
for an absent field and for one that failed its snippet guard, and neither is a
number this system may compare against anything. A comparison that could not
run lands in `comparisons_skipped` with its reason rather than being omitted —
a missing check and a check that found nothing are opposite facts.

## Language

A duplicate is a **possible** duplicate. Two institutions holding the same
certificate number is a discrepancy worth a human looking at; it is not
evidence that either of them did anything. Reasons say "appears in", "matches"
and "requires manual verification". A test asserts no reason contains "fake",
"forged", "fraudulent", "counterfeit" or "deepfake".

## What this module does not do

No risk score — findings and relationships only; combining them is step 7. No
LLM, no network, no external image API.
