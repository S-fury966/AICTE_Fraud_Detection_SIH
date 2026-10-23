# QR detection and cross-check (step 3)

Finds QR codes on a certificate, reads them, and reports what does not line up.
No LLM is involved anywhere in this module, and nothing here decides anything:
it emits findings with evidence, and risk scoring is a later step.

```
scan_document(path, certificate_number=..., issuing_body=...)
  -> QRScanResult(status, detections, findings, comparisons_skipped, audit)
```

## The safety rule

**Nothing in this module fetches, opens, resolves or follows a URL.** A QR
payload is a string chosen by whoever submitted the document, so it is treated
as text to be parsed and reported, never as an instruction to go somewhere. The
checks work entirely on the parsed string.

`test_nothing_in_a_full_scan_touches_the_network` enforces this by poisoning the
`socket` module and running a full scan over the payloads that most invite a
lookup — off-allowlist hosts, a shortener, a userinfo trick, an IP host. It
first asserts that its own trap fires, so it cannot pass vacuously.

## Why two image sources per PDF page

Both are needed, and each covers a case the other misses:

- **Rendered page** catches a QR drawn as vector paths. There is no image to
  extract, so extraction alone would find nothing — see the
  `certificate_vector_qr.pdf` fixture.
- **Embedded images** at native resolution catch a QR pasted in at higher
  detail than our render DPI, where rendering would blur away what makes it
  readable.

A code printed once is therefore often found twice. Identical payload on the
same page is treated as one detection, with the second sighting recorded in
`duplicate_sources` rather than reported as another QR.

## Why two decoders

OpenCV is primary because it is already installed for step 1's OCR. zxing-cpp
is the fallback, and it earns its place: on the fixtures, the rotated photograph
and the blurred low-resolution scan are both read by zxing after OpenCV returns
nothing.

Not pyzbar, which needs a native DLL on the PATH — exactly the Windows setup
problem that led to RapidOCR over Tesseract in step 1.

## The preprocessing ladder

Tried only when a plain decode finds nothing, in the order set in config, and
the rung that succeeded is recorded on the detection. A QR that needed three
transforms to read is itself worth knowing about, so the fact is kept rather
than smoothed over.

## The allowlist dot boundary

The one check most easily written wrong. `endswith()` on the allowlisted string
accepts `verify.nbta-demo.test.evil.test`; `contains()` accepts anything at all.
Matching is therefore equality, or equality of the part after a literal leading
dot, and only where the entry sets `allow_subdomains: true`. Both lookalike
shapes have fixtures and tests.

## Mixed-script detection is per label

A host is checked label by label, not as a whole. Nearly every
internationalised domain pairs a non-Latin name with an ASCII TLD, so comparing
scripts across the whole host would flag every legitimate IDN in the world.
Mixing *within* one label is the homoglyph pattern: a Cyrillic `а` inside an
otherwise Latin word.

An all-one-script lookalike (`аррӏе.test`, entirely Cyrillic) is not mixed
script and is caught by `punycode_host` instead.

## The demo QR format

Certificate ids contain slashes, so an un-encoded id would read as four path
segments. The id is percent-encoded into one:

```
https://verify.nbta-demo.test/c/NBTA%2FDEMO%2F2024%2F00417
```

Every demo domain is under the reserved `.test` TLD, which cannot resolve to a
real site. The path patterns and the `cert`/`id` query fallback are configurable.

## Comparison skipped is not the same as passed

When the certificate id cannot be compared — no id in the payload, no validated
`certificate_number` from step 2, or no step 2 result at all — the result says so
in `comparisons_skipped` with the reason. A check that could not run must never
read as a check that passed.

The comparison reuses step 2's own `normalise()` rather than reimplementing it.
Two modules normalising differently would make the comparison meaningless in
exactly the cases that matter.

## Config

`config/qr_checks.yaml`, stamped onto every result as `config_version`. DPI and
caps, decoder order, the ladder, the allowlist, the shortener list, the
per-finding strength labels, the certificate id patterns, and which issuers are
expected to carry a QR at all.

`strength` is `strong` or `weak` — a label read from config, never a number
computed here.

## CLI

```bash
python -m app.qr <file> [--extraction <step2_json>] [--compact]
```

Without `--extraction` the certificate id comparison is skipped and the output
says so. Only a `validated: true` value from step 2 is used: an unvalidated one
was already rejected as untraceable to the source, so comparing against it
would manufacture a mismatch out of our own uncertainty.

Exit code is 1 only on a decode error. A document with no QR is a normal result.
