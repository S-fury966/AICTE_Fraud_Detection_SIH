# PDF signature integrity (step 6, part A)

```
check_signatures(path, config, document_type, issuing_body) -> SignatureCheckResult
```

```bash
python -m app.signatures <file.pdf> [--document-type T] [--issuing-body B] [--compact]
```

## Scope: integrity only

This module answers **one** question: *was this file modified after it was
signed?*

It does **not** answer *who signed it*. There is no certificate-chain
validation and no trust store. That was a decision made before the module was
written, and it is enforced structurally rather than by convention:

- `SignatureDetail.signer_identity_verified` is hard-wired to `False`. No code
  path sets it otherwise, and a test iterates every fixture to assert that.
- The signer name and signing time are called
  `signer_name_unverified` and `claimed_signing_time_unverified`. The column
  names in the database match. A name is the last place that is still true at
  the point where someone reads the value out of a report.
- Every result carries a `scope_note` saying so in words, so a consumer
  reading one signature out of a report cannot miss it.

A corollary worth stating: certificate-chain validation is exactly the part of
signature verification that goes online — OCSP, CRL fetches, AIA chasing. Not
doing it is why this module can be, and is, tested under a socket trap.

## What it reports per signature

| Field | Meaning |
|---|---|
| `present` | a signature structure was located |
| `parsed` | it could be read well enough to judge |
| `byte_range` | the `/ByteRange` offset/length pairs |
| `coverage` | `entire_file` / `entire_revision` / `contiguous_block_from_start` / `unclear` |
| `covers_whole_file` | the range reaches the last byte |
| `digest_matches` | recomputed digest vs. the signed `messageDigest` — or `None` for "no comparison possible" |
| `computed_digest_hex` / `signed_digest_hex` | the two values actually compared |
| `appended` | byte length, object count and revisions after the signed one |
| `signer_name_unverified` | the signing certificate's subject, as printed |
| `claimed_signing_time_unverified` | `/M` or the CMS `signingTime` |

`digest_matches` is deliberately three-state. `False` means the content
changed; `None` means we could not compare, which is a different fact and
routes to a human rather than to a conclusion.

## Two libraries, and why the split is not arbitrary

**pyHanko** locates the signature fields, reads `/ByteRange`, and derives how
much of the file the range covers. Its `SignatureCoverageLevel` is precisely
the primitive this check needs, and re-deriving revision boundaries from
scratch is where subtle bugs live.

**hashlib plus asn1crypto**, ours, does the digest comparison: recompute over
the byte-range gaps, and read the signed `messageDigest` out of the CMS
`SignedData`.

That second half is ours because of a trap worth recording. pyHanko exposes
`external_digests`, which looks like the signed digest and **is not** — it is a
cache of *recomputed* digests. An early version of this module compared it
against our own recomputation and reported `digest_matches: True` on a file
with a visibly edited certificate number, because both sides were being
recomputed from the same tampered bytes. The check was vacuous and passed its
own test.

So the comparison is now ours end to end, and a separate test asserts our
recomputation agrees with pyHanko's `compute_digest()` — the part of pyHanko
that genuinely is independent of us.

## Findings

| Type | Strength | Fires when |
|---|---|---|
| `signature_digest_mismatch` | strong | covered bytes no longer hash to the signed digest |
| `signature_does_not_cover_document` | strong | coverage below `entire_revision`: the range stops at no boundary |
| `bytes_after_signed_revision` | **medium** | the signature is intact but bytes follow the revision it covers |
| `signature_unparseable` | weak | a signature exists that could not be read |
| `unsigned_where_signature_expected` | weak | no signature, and config lists this type or issuer as one that signs |
| `signature_weak_digest_algorithm` | weak | the digest algorithm is in `weak_digest_algorithms` |

Strengths come from `config/signature_checks.yaml`, and an unlisted type
defaults to **weak**, not strong: a missing config entry is our mistake and
must not manufacture a strong finding against an institution.

`medium` enters the project's vocabulary here. For an append after signing
neither label was honest — some legitimate tools add long-term validation data
or a second signature, so the reason carries that caveat in words, and the
evidence carries the *size and shape* of what was appended (byte length,
object count, revisions) rather than its content.

`unsigned_where_signature_expected` is **off by default**. Most EoA attachments
are scans that were never signed; flagging every one would bury the findings
that matter under a uniform layer of noise.

## A clean signed file still produces a record

No findings, and a full `signature_details` row. A findings table alone cannot
distinguish "one intact signature covering the whole file" from "no signature
at all", and those are different facts about a document.

## The unparseable fallback

A malformed CMS blob makes pyHanko's enumeration raise, and that takes out
**every** signature in the file rather than the one bad one. Without a
fallback, a file with one deliberately corrupted signature would report as
*unsigned* — the opposite of what is true. So when enumeration fails, the raw
bytes are scanned for `/ByteRange` occurrences and each is reported as present
but unparsed.

## Fixtures

`tests/make_signature_fixtures.py`. The signing key is a throwaway RSA key
generated **in memory on every run** and never written to disk, so there is no
key file to commit and nothing to leak.

The base document is hand-written as an **uncompressed** PDF rather than
produced by PyMuPDF, which compresses content streams. The tamper fixture has
to be an edit to a real printed digit — `00417` becoming `00418`, same length
so no offset moves — not a byte flipped at an arbitrary position.

A real signing library produces the fixtures while our own code does the digest
comparison, on purpose: if one piece of code both signed and verified, a shared
misreading of the spec would pass its own tests.

| Fixture | Coverage | `digest_matches` | Finding |
|---|---|---|---|
| `signed_clean` | `entire_file` | `True` | — |
| `signed_tampered_digit` | `entire_file` | `False` | digest mismatch |
| `signed_incremental_update` | `entire_revision` | `True` | bytes after signed revision |
| `signed_short_byte_range` | `contiguous_block_from_start` | `False` | does not cover document **+** digest mismatch |
| `signed_corrupt_cms` | — | `None` | unparseable |
| `unsigned` | — | — | — (unless config expects one) |

The two findings on `signed_short_byte_range` are correct rather than
redundant: the range was altered, so the digest over the altered range cannot
match either. A reviewer needs both, because "the signature lies about what it
covers" and "the covered bytes do not hash right" are different claims.

## What this module does not do

No certificate chain, no trust store, no revocation check, no timestamp
authority, no risk score. No LLM anywhere, and no network.
