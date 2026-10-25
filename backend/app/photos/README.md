# Photo authenticity (detector 3)

Five stages, ordered by how much their signal is worth. The strengths follow the
order; nothing here ever says an image is fake.

| Stage | Signal | Strongest it gets |
|---|---|---|
| `photo_reuse` | perceptual hash vs **our own** corpus of earlier photos, mirrored copies included | strong (other institution) |
| `photo_metadata` | EXIF capture date, GPS vs campus, editing-software tag; missing EXIF | strong only for GPS > 100 km; missing EXIF is weak |
| `photo_manipulation` | error level analysis (heatmap saved), copy-move (both regions) | medium |
| `photo_synthetic_hint` | image-generator markers in the file's own metadata | medium, always confidence `low` |
| `photo_claim_consistency` | claim vs image via a VLM — **advisory** | none configured → `source_unavailable` |

No network: reverse image search is internal (a confirmed decision), and the
synthetic-content wording is fixed — *"possible synthetic content, manual
verification needed"*.

## Measured thresholds (demo renders; `config/photo_checks.yaml`)

| Check | Untouched | Manipulated | Threshold |
|---|---|---|---|
| phash distance (256 bits) | unrelated rooms 66–160 | re-encode 2, ×0.5 2, mirrored+shrunk 4 (mirror-compared) | ≤ 24 |
| ELA ratio / hot blocks | ≤ 6.5 / ≤ 6 | ~9.7 / 81–91 | ≥ 8.0 and ≥ 20 |
| copy-move largest cluster | 18–55 (rows of desks are repetitive) | 163–291 | ≥ 100 |

**Not caught**: a 5% crop moves phash 104 bits — a whole-image hash cannot see
a crop. The spectral "AI image" heuristic exists in code but is **off**: it has
not been validated on real photographs, and the demo's own photos are synthetic
renderings, so any number it produced here would be meaningless.

Photo hashes are stored with `page = 0`, which keeps them out of the certificate
near-duplicate comparison (page 1); the duplicate detector also excludes photo
and faculty document types by config.
