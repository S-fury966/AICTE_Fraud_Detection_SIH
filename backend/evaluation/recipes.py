"""What each case type is, and what it should produce.

**This module is the independence rule made concrete.** A recipe says what the
generator *did* to a document, and the expectation beside it says what a
correct detector would therefore notice. Neither is obtained by asking a
detector. The generator knows it edited a digit because it edited the digit.

An expectation is a list of **groups**. A group is satisfied if *any* of its
listed finding types fires at or above `min_strength`. That shape is
deliberate: there is usually more than one honest way for a detector to notice
the same thing, and insisting on one particular finding type would turn a
design change into a false miss. For the duplicate cases a group may also be
satisfied by a finding on the *other* document of the pair, because which of
the two runs discovers a relationship depends on processing order and the
measurement must not.

`must_not` is the other half, and on a decision-support system it is the half
that matters more: a finding at medium or strong against an honest institution
costs a real review. Hard negatives therefore carry explicit `must_not` lists,
and the false-positive rate is measured on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Expectation vocabulary
# --------------------------------------------------------------------------

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"


@dataclass
class ExpectedGroup:
    """One thing a correct run should notice, and how strongly."""

    any_of: list[str]
    min_strength: str = STRONG
    # True when the finding may legitimately appear on the paired document
    # instead of this one, because a relationship is discovered by whichever
    # of the two was processed second.
    either_side: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "any_of": list(self.any_of),
            "min_strength": self.min_strength,
            "either_side": self.either_side,
            "note": self.note,
        }


@dataclass
class Recipe:
    """A case type: what to do to the document, and what to expect."""

    case_type: str
    is_fraud: bool
    fraud_types: list[str] = field(default_factory=list)
    expected: list[ExpectedGroup] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    expected_issuer_status: str | None = None
    # Which form this case forces, when it needs one. None means "pick from
    # the configured weights".
    force_form: str | None = None
    force_noise: str | None = None
    force_template: str | None = None
    # True when the case is built from an earlier clean document, so it must be
    # ordered after its source.
    needs_source: bool = False
    notes: str = ""


# Every strong finding type a clean document must not provoke. Listed once so
# the clean and genuine cases cannot drift apart from each other.
_NO_STRONG_FINDINGS = [
    "qr_domain_not_allowlisted",
    "qr_deceptive_url_pattern",
    "qr_certificate_id_mismatch",
    "qr_pdf_link_mismatch",
    "issuer_record_not_found",
    "issuer_field_mismatch",
    "issuer_status_not_active",
    "issuer_qr_id_other_institution",
    "issuer_expired",
    "signature_digest_mismatch",
    "signature_does_not_cover_document",
    "bytes_after_signed_revision",
    "duplicate_file_exact",
    "certificate_number_reuse",
    "qr_payload_reuse",
    "near_duplicate_page_image",
]


def _recipes() -> dict[str, Recipe]:
    """Every case type, keyed by name. Counts come from config."""
    table: dict[str, Recipe] = {}

    def add(recipe: Recipe) -> None:
        table[recipe.case_type] = recipe

    # ---------------------------------------------------------------- genuine
    add(
        Recipe(
            case_type="clean",
            is_fraud=False,
            expected=[],
            must_not=list(_NO_STRONG_FINDINGS),
            expected_issuer_status="verified",
            notes=(
                "Genuine and unremarkable, spread across layouts, forms and "
                "noise levels. The baseline for the false-positive rate."
            ),
        )
    )

    # ------------------------------------------------------- hard negatives
    add(
        Recipe(
            case_type="hard_negative_abbreviated_institution",
            is_fraud=False,
            must_not=["issuer_field_mismatch"] + _NO_STRONG_FINDINGS,
            expected_issuer_status="verified",
            notes=(
                "The page prints 'Inst. of Tech.' where the registry holds "
                "'Institute of Technology'. Same institution, written the way "
                "an applicant actually writes it. Step 4's name normalisation "
                "is supposed to absorb this."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_missing_grade",
            is_fraud=False,
            must_not=["issuer_field_mismatch"] + _NO_STRONG_FINDINGS,
            expected_issuer_status="verified",
            notes=(
                "No grade printed. A field absent from the document is "
                "'not_compared', never a mismatch - a missing value cannot "
                "contradict anything."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_noisy_scan",
            is_fraud=False,
            must_not=list(_NO_STRONG_FINDINGS),
            force_form="jpg_scan",
            force_noise="hard",
            expected_issuer_status="verified",
            notes=(
                "Genuine certificate, badly scanned. Degraded text must not "
                "turn into a mismatch: the honest outcome of unreadable input "
                "is a skipped comparison, not a finding."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_blurred_qr",
            is_fraud=False,
            must_not=[
                "qr_certificate_id_mismatch",
                "qr_domain_not_allowlisted",
            ]
            + _NO_STRONG_FINDINGS,
            force_form="scanned_pdf",
            force_noise="hard",
            notes=(
                "The QR is there but hard to decode. A QR that will not read "
                "is a weak signal at most; it must never become a mismatch, "
                "because 'I could not read it' and 'it disagrees' are "
                "opposite statements."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_same_institution_resubmission",
            is_fraud=False,
            needs_source=True,
            must_not=[
                "duplicate_file_exact",
                "certificate_number_reuse",
                "near_duplicate_page_image",
            ],
            expected_issuer_status="verified",
            notes=(
                "An institution resubmitting its own file - a correction, a "
                "re-upload, a second cycle. Entirely normal. The duplicate "
                "findings are expected to fire at WEAK and must not reach "
                "medium."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_same_template_pair",
            is_fraud=False,
            must_not=[
                "near_duplicate_page_image",
                "certificate_number_reuse",
                "duplicate_file_exact",
            ],
            expected_issuer_status="verified",
            notes=(
                "Two institutions, one issuer's template, two numbers the "
                "issuer knows. Legitimate certificates from one body do look "
                "alike. The near-duplicate finding may fire at weak; anything "
                "stronger is a false positive against an honest applicant."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_signed_untouched",
            is_fraud=False,
            force_form="signed_pdf",
            force_noise="none",
            must_not=[
                "signature_digest_mismatch",
                "signature_does_not_cover_document",
                "bytes_after_signed_revision",
                "signature_unparseable",
            ]
            + _NO_STRONG_FINDINGS,
            expected_issuer_status="verified",
            notes=(
                "Genuinely signed and untouched. Produces no findings and a "
                "stored signature record. Note for step 8: this is NEUTRAL, "
                "not reassuring - signer identity is never verified."
            ),
        )
    )
    add(
        Recipe(
            case_type="hard_negative_issuer_without_qr",
            is_fraud=False,
            force_template="plain",
            must_not=[
                "qr_domain_not_allowlisted",
                "qr_certificate_id_mismatch",
                "qr_deceptive_url_pattern",
            ]
            + _NO_STRONG_FINDINGS,
            expected_issuer_status="verified",
            notes=(
                "This issuing body prints no QR code at all. A missing QR is "
                "a property of the issuer, not a missing check, and must stay "
                "quiet - `default_expect_qr` is false for exactly this reason."
            ),
        )
    )

    # ------------------------------------------- genuine, awkward issuer state
    for case, status, finding, note in (
        (
            "issuer_state_revoked",
            "mismatch_detected",
            "issuer_status_not_active",
            "The issuing body withdrew the certificate. The document is "
            "honest; the registry is the news.",
        ),
        (
            "issuer_state_suspended",
            "mismatch_detected",
            "issuer_status_not_active",
            "Suspended by the issuer. Again the document is honest.",
        ),
        (
            "issuer_state_expired",
            "expired",
            "issuer_expired",
            "Expired according to the issuer, as of the fixed evaluation "
            "date. A fact about dates, not a question about the document.",
        ),
        (
            "issuer_state_pending",
            "verification_pending",
            None,
            "The issuer has the record but has not finished processing it. "
            "Pending is not a conclusion and must not be scored as one.",
        ),
    ):
        add(
            Recipe(
                case_type=case,
                is_fraud=False,
                expected=(
                    [
                        ExpectedGroup(
                            any_of=[finding],
                            min_strength=STRONG,
                            note="the issuer's own state, surfaced",
                        )
                    ]
                    if finding
                    else []
                ),
                expected_issuer_status=status,
                force_noise="none",
                notes=note,
            )
        )

    # ----------------------------------------------------------------- frauds
    add(
        Recipe(
            case_type="number_edited_qr_untouched",
            is_fraud=True,
            fraud_types=["certificate_number_edited"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["qr_certificate_id_mismatch"],
                    min_strength=STRONG,
                    note=(
                        "the printed number and the QR disagree, which is "
                        "only visible because both are read"
                    ),
                )
            ],
            notes=(
                "One digit of the printed number changed; the QR still "
                "encodes the original. The cross-check is the whole point."
            ),
        )
    )
    add(
        Recipe(
            case_type="number_edited_qr_consistent",
            is_fraud=True,
            fraud_types=["certificate_number_edited", "qr_payload_edited"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["issuer_record_not_found"],
                    min_strength=STRONG,
                    note=(
                        "both sources agree with each other and neither "
                        "agrees with the issuer - the independent source is "
                        "the only thing left that can notice"
                    ),
                )
            ],
            expected_issuer_status="manual_review_required",
            notes=(
                "Printed number and QR both changed to the same new value. "
                "The internal cross-check is defeated by construction; the "
                "issuer lookup is the backstop."
            ),
        )
    )
    add(
        Recipe(
            case_type="qr_lookalike_domain",
            is_fraud=True,
            fraud_types=["qr_domain"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["qr_domain_not_allowlisted"],
                    min_strength=STRONG,
                    note="allowlisted name present, but as a subdomain of another host",
                )
            ],
            notes=(
                "The QR points at verify.nbta-demo.test.evil-mirror.test. A "
                "naive `endswith` or `contains` check accepts it."
            ),
        )
    )
    add(
        Recipe(
            case_type="qr_userinfo_trick",
            is_fraud=True,
            fraud_types=["qr_domain"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=[
                        "qr_deceptive_url_pattern",
                        "qr_domain_not_allowlisted",
                    ],
                    min_strength=STRONG,
                    note="everything before the @ is decoration",
                )
            ],
            notes="https://verify.nbta-demo.test@evil-mirror.test/...",
        )
    )
    add(
        Recipe(
            case_type="qr_punycode_or_shortener",
            is_fraud=True,
            fraud_types=["qr_domain"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=[
                        "qr_deceptive_url_pattern",
                        "qr_domain_not_allowlisted",
                    ],
                    min_strength=STRONG,
                    note="homoglyph host, or a shortener hiding the destination",
                )
            ],
            notes="Alternates between a punycode homoglyph host and a shortener.",
        )
    )
    add(
        Recipe(
            case_type="qr_id_belongs_to_other_institution",
            is_fraud=True,
            fraud_types=["qr_payload_edited"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=[
                        "issuer_qr_id_other_institution",
                        "qr_certificate_id_mismatch",
                    ],
                    min_strength=STRONG,
                    note=(
                        "the QR resolves to a real record - held by somebody "
                        "else"
                    ),
                )
            ],
            notes=(
                "The QR carries another institution's genuine certificate id. "
                "Both the internal cross-check and the issuer lookup can "
                "notice, which is why the group accepts either."
            ),
        )
    )
    add(
        Recipe(
            case_type="unknown_certificate",
            is_fraud=True,
            fraud_types=["fabricated_certificate"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["issuer_record_not_found"],
                    min_strength=STRONG,
                    note="well formed and absent from the registry",
                )
            ],
            expected_issuer_status="manual_review_required",
            notes=(
                "Nothing about the document is malformed. Only an independent "
                "source can tell. Note the status: not-found routes to a "
                "human, never to a conclusion, because a registry can be "
                "incomplete."
            ),
        )
    )
    add(
        Recipe(
            case_type="institution_name_swapped",
            is_fraud=True,
            fraud_types=["institution_name_swapped"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["issuer_field_mismatch"],
                    min_strength=STRONG,
                    note="a real certificate number under the wrong name",
                )
            ],
            expected_issuer_status="mismatch_detected",
            notes=(
                "Another college's name placed on a genuine certificate "
                "number. The number verifies; the name does not."
            ),
        )
    )
    add(
        Recipe(
            case_type="dates_extended",
            is_fraud=True,
            fraud_types=["validity_extended"],
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["issuer_field_mismatch"],
                    min_strength=STRONG,
                    note="valid_to on the page outlasts the registry's",
                )
            ],
            expected_issuer_status="mismatch_detected",
            notes="An expired certificate's validity window extended on the page.",
        )
    )
    add(
        Recipe(
            case_type="signed_then_tampered",
            is_fraud=True,
            fraud_types=["signature_content_modified"],
            force_form="signed_pdf",
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["signature_digest_mismatch"],
                    min_strength=STRONG,
                    note="the covered bytes no longer hash to the signed digest",
                )
            ],
            notes=(
                "One printed digit edited inside the signed range, same "
                "length so no offset moves. The byte range still describes "
                "the file correctly, so only the digest can catch it."
            ),
        )
    )
    add(
        Recipe(
            case_type="signed_then_appended",
            is_fraud=True,
            fraud_types=["signature_appended_revision"],
            force_form="signed_pdf",
            force_noise="none",
            expected=[
                ExpectedGroup(
                    any_of=["bytes_after_signed_revision"],
                    min_strength=MEDIUM,
                    note=(
                        "medium, not strong: legitimate tools append "
                        "long-term validation data the same way"
                    ),
                )
            ],
            notes=(
                "A valid incremental revision added after signing. The "
                "signature over the earlier revision is intact, which is "
                "exactly why this is a coverage finding and not a digest one."
            ),
        )
    )
    add(
        Recipe(
            case_type="duplicate_file_other_institution",
            is_fraud=True,
            fraud_types=["duplicate_document"],
            needs_source=True,
            expected=[
                ExpectedGroup(
                    any_of=["duplicate_file_exact", "certificate_number_reuse"],
                    min_strength=STRONG,
                    either_side=True,
                    note="the identical file arriving from somewhere else",
                )
            ],
            notes="An exact copy of an earlier clean document, different institution.",
        )
    )
    add(
        Recipe(
            case_type="number_reuse_other_institution",
            is_fraud=True,
            fraud_types=["certificate_number_reuse"],
            needs_source=True,
            expected=[
                ExpectedGroup(
                    any_of=["certificate_number_reuse"],
                    min_strength=STRONG,
                    either_side=True,
                    note=(
                        "same number, freshly rendered on a different layout, "
                        "so neither the file hash nor the page image can see it"
                    ),
                )
            ],
            notes=(
                "The number is re-rendered on a DIFFERENT template, which "
                "isolates the number check: the bytes differ and the pages do "
                "not look alike."
            ),
        )
    )
    add(
        Recipe(
            case_type="resaved_copy_with_edit",
            is_fraud=True,
            fraud_types=["duplicate_document", "field_edited"],
            needs_source=True,
            expected=[
                ExpectedGroup(
                    any_of=[
                        "certificate_number_reuse",
                        "near_duplicate_page_image",
                    ],
                    min_strength=MEDIUM,
                    either_side=True,
                    note=(
                        "recompressed, so the file hash is useless; the "
                        "number and the page image are what remain"
                    ),
                )
            ],
            notes=(
                "An earlier document recompressed as a JPEG with the grade "
                "edited, submitted by another institution. The certificate "
                "number is kept, so the number check should fire strongly and "
                "the image check should corroborate it."
            ),
        )
    )

    return table


RECIPES = _recipes()


def recipe_for(case_type: str) -> Recipe:
    try:
        return RECIPES[case_type]
    except KeyError as exc:
        raise KeyError(
            f"unknown case type {case_type!r}; known: "
            f"{', '.join(sorted(RECIPES))}"
        ) from exc


def fraud_case_types() -> list[str]:
    return sorted(key for key, value in RECIPES.items() if value.is_fraud)


def genuine_case_types() -> list[str]:
    return sorted(key for key, value in RECIPES.items() if not value.is_fraud)


def hard_negative_case_types() -> list[str]:
    return sorted(key for key in RECIPES if key.startswith("hard_negative_"))
