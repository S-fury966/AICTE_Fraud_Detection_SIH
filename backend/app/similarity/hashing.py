"""Perceptual hashing, shared.

Lives in its own module because the photo detector will want exactly this: a
hash of an image, a Hamming distance between two hashes, and a stable hex
encoding to put in a database column. Nothing here knows about documents,
findings or duplicates.

**What the measurements showed.** The thresholds this feeds are not guesses;
they were measured on synthetic certificate pages (see `app/duplicates/README.md`
for the table). Two results shaped the design:

- A **64-bit** hash cannot discriminate document pages at all. A page of sparse
  black text on white is almost entirely white, so the hash is dominated by
  whiteness: an *unrelated* document sat 8 bits from a certificate, and a
  certificate from a different institution sat 0 bits away - indistinguishable
  from the same file. 256 bits (`hash_size: 16`) is the smallest size with
  usable separation.
- **dhash beats phash** on this material, which is the opposite of the usual
  advice. phash keeps low-frequency DCT coefficients, and a sparse text page
  has almost no low-frequency structure, so tiny changes dominate: a 1-degree
  rotation moved phash 22 bits while dhash moved 6. Both are computed and
  stored; the comparison kind is config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The hash kinds this module can compute. Names are stored in the database, so
# adding one is additive and removing one is not.
PHASH = "phash"
DHASH = "dhash"
AHASH = "ahash"

SUPPORTED_KINDS = (PHASH, DHASH, AHASH)


class UnsupportedHashKind(ValueError):
    pass


@dataclass(frozen=True)
class PerceptualHash:
    """One hash, with everything needed to compare it to another."""

    kind: str
    # The side length of the hash grid. 16 means a 256-bit hash.
    hash_size: int
    # Lowercase hex. Fixed width for a given kind and size, so two hashes of
    # the same kind and size are always comparable bit for bit.
    hex_value: str

    @property
    def bit_count(self) -> int:
        return len(self.hex_value) * 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "hash_size": self.hash_size,
            "hex_value": self.hex_value,
            "bit_count": self.bit_count,
        }


def compute(image: Any, kind: str, hash_size: int = 16) -> PerceptualHash:
    """Hash a PIL image.

    `image` is a `PIL.Image.Image`. Taking the image rather than a path keeps
    this module free of any opinion about where images come from - a PDF page,
    a submitted photograph, or a crop of one.
    """
    import imagehash

    functions = {
        PHASH: imagehash.phash,
        DHASH: imagehash.dhash,
        AHASH: imagehash.average_hash,
    }
    if kind not in functions:
        raise UnsupportedHashKind(
            f"{kind!r} is not a supported hash kind; "
            f"allowed: {', '.join(SUPPORTED_KINDS)}"
        )

    value = functions[kind](image.convert("RGB"), hash_size=hash_size)
    return PerceptualHash(kind=kind, hash_size=hash_size, hex_value=str(value))


def compute_all(
    image: Any, kinds: list[str], hash_size: int = 16
) -> list[PerceptualHash]:
    """Hash one image several ways.

    Every configured kind is stored even though only one is compared, because
    re-hashing a corpus later means re-rendering every stored document, and the
    photo detector may well want a different kind than the document one does.
    """
    return [compute(image, kind, hash_size=hash_size) for kind in kinds]


def hamming_distance(left: PerceptualHash | str, right: PerceptualHash | str) -> int:
    """Bits that differ between two hashes of the same kind and length.

    Raises on a length mismatch rather than padding. Comparing a 64-bit hash
    against a 256-bit one would silently produce a small, meaningless distance
    - and a small distance is precisely what this system reads as "these may be
    the same document".
    """
    left_hex = left.hex_value if isinstance(left, PerceptualHash) else str(left)
    right_hex = right.hex_value if isinstance(right, PerceptualHash) else str(right)

    if len(left_hex) != len(right_hex):
        raise ValueError(
            "cannot compare hashes of different lengths "
            f"({len(left_hex) * 4} bits vs {len(right_hex) * 4} bits); "
            "they were computed at different hash sizes"
        )

    try:
        difference = int(left_hex, 16) ^ int(right_hex, 16)
    except ValueError as exc:
        raise ValueError(f"not a hex hash value: {exc}") from exc
    return bin(difference).count("1")
