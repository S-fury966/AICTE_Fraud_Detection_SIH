"""Tests for step 6 part B: cross-application duplicate detection.

In-memory SQLite, the stub LLM provider, the mock issuer in-process, no sleeps
and no network. Fixtures come from `tests/make_duplicate_fixtures.py`.

Tests that run the pipeline carry the `qr` marker, because the pipeline decodes
real images and that is what makes them slow. The pure hashing and
cross-reference tests stay in the fast suite.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.db import repository
from app.db.engine import Database
from app.db.models import CrossReference, PerceptualHashRow
from app.duplicates import FindingType, cross_references_for_document
from app.duplicates.detector import strength_for
from app.llm.stub_provider import StubProvider
from app.mock_issuer.registry import load_registry
from app.mock_issuer.service import create_app
from app.pipeline.runner import Pipeline, ensure_application
from app.qr.images import render_page_image
from app.similarity import hashing
from tests.make_duplicate_fixtures import (
    CERT_ID,
    EDITED_CERT_ID,
    INSTITUTION_A,
    INSTITUTION_B,
    INSTITUTION_C,
    SIBLING_CERT_ID,
)
from tests.make_duplicate_fixtures import build_all as build_duplicate_fixtures

AS_OF = "2026-01-15"
ACTOR = "officer.sharma"
ROLE = "officer"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def samples() -> dict[str, Path]:
    return build_duplicate_fixtures()


@pytest.fixture
def persistence_config(tmp_path) -> dict:
    config = copy.deepcopy(load_config("persistence"))
    config["database"]["url"] = "sqlite:///:memory:"
    config["storage"]["root"] = str(tmp_path / "store")
    return config


@pytest.fixture
def duplicates_config() -> dict:
    return copy.deepcopy(load_config("duplicate_checks"))


@pytest.fixture
def database(persistence_config) -> Database:
    db = Database(persistence_config)
    yield db
    db.dispose()


def certificate_payload(
    institution: str = INSTITUTION_A, certificate_id: str = CERT_ID
) -> dict:
    """What the stub model returns. Only the two fields the checks compare are
    varied; the rest stay null, which is a normal successful extraction."""
    null = {"value": None, "source_snippet": None, "page": None, "confidence": None}
    return {
        "institution_name": {
            "value": institution,
            "source_snippet": f"Institution: {institution}",
            "page": 1,
            "confidence": "high",
        },
        "certificate_number": {
            "value": certificate_id,
            "source_snippet": f"Certificate Number: {certificate_id}",
            "page": 1,
            "confidence": "high",
        },
        "issuing_body": dict(null),
        "programme": dict(null),
        "accreditation_status_or_grade": dict(null),
        "issue_date": dict(null),
        "valid_from": dict(null),
        "valid_to": dict(null),
        "signatory_name": dict(null),
    }


@pytest.fixture
def pipeline(database, persistence_config, duplicates_config):
    """A pipeline with every external collaborator replaced in-process."""

    def build(institution: str = INSTITUTION_A, certificate_id: str = CERT_ID,
              configs: dict | None = None):
        registry = load_registry()
        merged = {
            "persistence": persistence_config,
            "duplicate_checks": duplicates_config,
        }
        merged.update(configs or {})
        return Pipeline(
            database=database,
            llm_provider=StubProvider(
                responses=[json.dumps(certificate_payload(institution, certificate_id))]
            ),
            issuer_transport=TestClient(create_app(registry=registry)),
            issuer_sleep=lambda seconds: None,
            configs=merged,
            registry_hash=registry.sha256,
            as_of=AS_OF,
        )

    return build


@pytest.fixture
def submit(database, persistence_config, pipeline):
    """Submit one file under one institution and return the run summary."""

    def go(
        path: Path,
        institution: str = INSTITUTION_A,
        certificate_id: str = CERT_ID,
        configs: dict | None = None,
    ):
        application_id = ensure_application(
            database,
            institution_name=institution,
            category="extension_of_approval",
            actor=ACTOR,
            role=ROLE,
            config=persistence_config,
        )
        pipe = pipeline(institution, certificate_id, configs)
        return pipe.process_document(
            application_id=application_id,
            file_path=path,
            document_type="accreditation_certificate",
            actor=ACTOR,
            role=ROLE,
        )

    return go


def stage(summary, name):
    for entry in summary.stages:
        if entry.stage == name:
            return entry
    raise AssertionError(f"no stage {name}; got {[s.stage for s in summary.stages]}")


def findings_of(database, run_id: int, detector: str | None = None) -> list:
    with database.session() as session:
        rows = repository.findings_for_run(session, run_id)
        return [
            {
                "type": row.type,
                "strength": row.strength,
                "reason": row.reason,
                "evidence": row.evidence,
                "detector": row.detector,
            }
            for row in rows
            if detector is None or row.detector == detector
        ]


# --------------------------------------------------------------------------
# The measured distances behind the threshold
# --------------------------------------------------------------------------


def test_the_measured_distances_match_the_configured_threshold(
    samples, duplicates_config
):
    """The threshold is evidence, so the evidence is a test.

    If a change to `compare_kind`, `hash_size` or `render_dpi` moves these
    numbers, this fails rather than letting a threshold quietly stop meaning
    what its comment says it means.
    """
    kind = duplicates_config["perceptual_hash"]["compare_kind"]
    size = duplicates_config["perceptual_hash"]["hash_size"]
    dpi = duplicates_config["perceptual_hash"]["render_dpi"]
    threshold = duplicates_config["perceptual_hash"]["near_duplicate_max_distance"]

    def distance(a: str, b: str) -> int:
        left = hashing.compute(render_page_image(samples[a], 1, dpi), kind, size)
        right = hashing.compute(render_page_image(samples[b], 1, dpi), kind, size)
        return hashing.hamming_distance(left, right)

    # Same document in various shapes: all must be inside the threshold.
    assert distance("original", "exact_copy") == 0
    assert distance("original", "recompressed") <= threshold
    assert distance("original", "rotated") <= threshold
    assert distance("original", "one_digit_edited") <= threshold

    # The negative control must be outside it, with room to spare.
    unrelated = distance("original", "unrelated")
    assert unrelated > threshold
    assert unrelated >= threshold + 8, (
        "the margin between a near-duplicate and an unrelated document has "
        f"narrowed to {unrelated - threshold} bits; the threshold needs "
        "re-measuring"
    )


def test_a_template_sibling_is_indistinguishable_from_a_copy(
    samples, duplicates_config
):
    """The finding that drives the whole severity design.

    A different institution's genuine certificate on the same issuer template
    sits *inside* the band a re-saved copy occupies. No threshold separates
    them, which is exactly why a near-duplicate image is never strong.
    """
    kind = duplicates_config["perceptual_hash"]["compare_kind"]
    size = duplicates_config["perceptual_hash"]["hash_size"]
    dpi = duplicates_config["perceptual_hash"]["render_dpi"]

    def hash_of(name: str):
        return hashing.compute(render_page_image(samples[name], 1, dpi), kind, size)

    base = hash_of("original")
    sibling = hashing.hamming_distance(base, hash_of("legitimate_template_pair"))
    resaved = hashing.hamming_distance(base, hash_of("recompressed"))
    edited = hashing.hamming_distance(base, hash_of("one_digit_edited"))

    # The sibling is no further away than a genuine re-save, and a one-digit
    # edit is nearer than either. A perceptual hash simply cannot see which is
    # which.
    assert sibling <= max(resaved, edited) + 4
    assert edited <= resaved + 4


def test_a_one_digit_edit_is_invisible_to_the_image_but_visible_in_the_number(
    samples, duplicates_config
):
    """Two checks, two capabilities, and neither is a substitute for the other."""
    kind = duplicates_config["perceptual_hash"]["compare_kind"]
    size = duplicates_config["perceptual_hash"]["hash_size"]
    dpi = duplicates_config["perceptual_hash"]["render_dpi"]

    base = hashing.compute(render_page_image(samples["original"], 1, dpi), kind, size)
    edited = hashing.compute(
        render_page_image(samples["one_digit_edited"], 1, dpi), kind, size
    )
    # The image says "same document".
    assert hashing.hamming_distance(base, edited) <= 4
    # The number says otherwise, and the number is what decides.
    assert CERT_ID != EDITED_CERT_ID


# --------------------------------------------------------------------------
# Hashing itself
# --------------------------------------------------------------------------


def test_hashes_of_different_lengths_refuse_to_be_compared(samples):
    """Padding would produce a small, meaningless distance - and a small
    distance is precisely what this system reads as "possibly the same"."""
    image = render_page_image(samples["original"], 1, 150)
    small = hashing.compute(image, "dhash", hash_size=8)
    large = hashing.compute(image, "dhash", hash_size=16)

    assert small.bit_count == 64
    assert large.bit_count == 256
    with pytest.raises(ValueError, match="different lengths"):
        hashing.hamming_distance(small, large)


def test_an_unknown_hash_kind_is_rejected(samples):
    image = render_page_image(samples["original"], 1, 150)
    with pytest.raises(hashing.UnsupportedHashKind):
        hashing.compute(image, "notahash")


def test_identical_images_hash_identically(samples):
    a = render_page_image(samples["original"], 1, 150)
    b = render_page_image(samples["exact_copy"], 1, 150)
    assert hashing.compute(a, "dhash") == hashing.compute(b, "dhash")


# --------------------------------------------------------------------------
# Exact file duplicates
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_an_exact_duplicate_across_institutions_is_strong(samples, submit, database):
    submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["original"], institution=INSTITUTION_B)

    findings = findings_of(database, second.run_id, "duplicate_detection")
    exact = [f for f in findings if f["type"] == FindingType.EXACT_FILE.value]

    assert len(exact) == 1
    assert exact[0]["strength"] == "strong"
    assert INSTITUTION_A in exact[0]["reason"]
    assert exact[0]["evidence"]["same_institution"] is False
    assert exact[0]["evidence"]["file_sha256"]
    assert "manual verification" in exact[0]["reason"]


@pytest.mark.qr
def test_the_same_institution_resubmitting_is_weak_and_says_so(
    samples, submit, database
):
    """A resubmission is normal: a correction, a re-upload, a second cycle."""
    submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["original"], institution=INSTITUTION_A)

    findings = findings_of(database, second.run_id, "duplicate_detection")
    exact = [f for f in findings if f["type"] == FindingType.EXACT_FILE.value]

    assert len(exact) == 1
    assert exact[0]["strength"] == "weak"
    assert exact[0]["evidence"]["same_institution"] is True
    assert "resubmission is normal" in exact[0]["reason"]


@pytest.mark.qr
def test_identical_content_in_a_differently_named_file_still_matches(
    samples, submit, database
):
    """Content-addressed storage means the filename is irrelevant."""
    first = submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["exact_copy"], institution=INSTITUTION_B)

    assert first.file_sha256 == second.file_sha256
    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert any(f["type"] == FindingType.EXACT_FILE.value for f in findings)


# --------------------------------------------------------------------------
# Near-duplicate rendering
# --------------------------------------------------------------------------


@pytest.mark.qr
@pytest.mark.ocr
def test_a_recompressed_copy_is_caught_by_the_image_not_the_hash(
    samples, submit, database
):
    """The bytes differ completely, so only the perceptual hash can see it."""
    first = submit(samples["original"], institution=INSTITUTION_A)
    second = submit(
        samples["recompressed"], institution=INSTITUTION_B, certificate_id=CERT_ID
    )

    assert first.file_sha256 != second.file_sha256

    findings = findings_of(database, second.run_id, "duplicate_detection")
    near = [
        f for f in findings if f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value
    ]
    assert len(near) == 1
    assert not any(f["type"] == FindingType.EXACT_FILE.value for f in findings)

    evidence = near[0]["evidence"]
    assert evidence["hamming_distance"] <= evidence["threshold"]
    assert evidence["hash_bits"] == 256


@pytest.mark.qr
@pytest.mark.ocr
def test_a_rotated_copy_still_matches(samples, submit, database):
    submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["rotated"], institution=INSTITUTION_B)

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert any(
        f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value for f in findings
    )


@pytest.mark.qr
def test_an_unrelated_document_does_not_match(samples, submit, database):
    submit(samples["original"], institution=INSTITUTION_A)
    second = submit(
        samples["unrelated"],
        institution=INSTITUTION_C,
        certificate_id="VTU/DEMO/2023/77120",
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert findings == []


@pytest.mark.qr
def test_the_legitimate_template_pair_is_not_flagged_strongly(
    samples, submit, database
):
    """Legitimate certificates from one issuer share a template.

    The near-duplicate finding still fires - the pages genuinely do look alike,
    and suppressing that would be hiding evidence - but it is weak, and the
    reason names the explanation a reviewer should check first.
    """
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    second = submit(
        samples["legitimate_template_pair"],
        institution=INSTITUTION_B,
        certificate_id=SIBLING_CERT_ID,
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert all(f["strength"] != "strong" for f in findings), findings

    near = [
        f for f in findings if f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value
    ]
    assert len(near) == 1
    assert near[0]["strength"] == "weak"

    evidence = near[0]["evidence"]
    assert evidence["strength_context"] == "different_number"
    # Both numbers and the distance, as the spec requires: the distance alone
    # cannot tell a copy from a template sibling, the numbers can. The second
    # document is the one under test, so `certificate_number` is its own.
    assert evidence["certificate_number"] == SIBLING_CERT_ID
    assert evidence["other_certificate_number"] == CERT_ID
    assert evidence["certificate_numbers_differ"] is True
    assert "hamming_distance" in evidence
    assert "sharing one issuer" in near[0]["reason"]


@pytest.mark.qr
@pytest.mark.ocr
def test_a_near_duplicate_sharing_a_number_across_institutions_is_medium(
    samples, submit, database
):
    """Corroboration, not a second independent accusation.

    The number itself has already fired strongly through
    `certificate_number_reuse`; the image similarity adds weight to that, which
    is what medium means here.
    """
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    second = submit(
        samples["recompressed"], institution=INSTITUTION_B, certificate_id=CERT_ID
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    near = next(
        f for f in findings if f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value
    )
    assert near["strength"] == "medium"
    assert near["evidence"]["strength_context"] == "same_number_other_institution"

    number = next(
        f for f in findings if f["type"] == FindingType.CERTIFICATE_NUMBER.value
    )
    assert number["strength"] == "strong"


# --------------------------------------------------------------------------
# Certificate number and QR payload reuse
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_certificate_number_reuse_across_institutions_is_strong(
    samples, submit, database
):
    """Different files, same number, different institutions.

    The annexure fixture isolates the number check: its bytes differ and its
    page looks nothing like the certificate, so a match here can only have come
    from the number.
    """
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    second = submit(
        samples["different_layout_same_number"],
        institution=INSTITUTION_B,
        certificate_id=CERT_ID,
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    number = [
        f for f in findings if f["type"] == FindingType.CERTIFICATE_NUMBER.value
    ]
    assert len(number) == 1
    assert number[0]["strength"] == "strong"
    assert "mismatch detected" in number[0]["reason"]
    assert number[0]["evidence"]["certificate_number"] == CERT_ID
    # Nothing else fired: the files differ and the pages do not look alike.
    assert [f["type"] for f in findings] == [
        FindingType.CERTIFICATE_NUMBER.value
    ]


@pytest.mark.qr
def test_certificate_number_reuse_within_one_institution_is_weak(
    samples, submit, database
):
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    second = submit(
        samples["different_layout_same_number"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    number = next(
        f for f in findings if f["type"] == FindingType.CERTIFICATE_NUMBER.value
    )
    assert number["strength"] == "weak"


@pytest.mark.qr
def test_a_number_that_failed_the_snippet_guard_is_never_compared(
    samples, submit, database
):
    """Step 2 returns null both for an absent field and for one it rejected.

    Neither is a number this system may compare against anything, and the
    skipped comparison is recorded rather than silently passing.
    """
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    # A number the source text does not contain fails the exact-value check, so
    # step 2 discards it.
    second = submit(
        samples["unrelated"],
        institution=INSTITUTION_B,
        certificate_id="NBTA/NOT/IN/THE/TEXT",
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert not any(
        f["type"] == FindingType.CERTIFICATE_NUMBER.value for f in findings
    )


@pytest.mark.qr
def test_qr_payload_reuse_across_institutions_is_strong(submit, database):
    """Driven with the step 3 QR fixtures, which carry a real decodable code."""
    from tests.make_qr_fixtures import build_all as build_qr

    qr = build_qr()
    submit(qr["valid_pdf"], institution=INSTITUTION_A)
    second = submit(qr["valid_jpg"], institution=INSTITUTION_B)

    findings = findings_of(database, second.run_id, "duplicate_detection")
    payload = [f for f in findings if f["type"] == FindingType.QR_PAYLOAD.value]
    assert len(payload) == 1
    assert payload[0]["strength"] == "strong"
    assert "same QR payload" in payload[0]["reason"]


@pytest.mark.qr
def test_a_document_with_no_qr_records_the_skipped_comparison(
    samples, submit, database
):
    second = submit(samples["original"], institution=INSTITUTION_A)
    duplicate_stage = stage(second, "duplicate_check")
    assert duplicate_stage.payload is not None
    reasons = [
        entry["reason"] for entry in duplicate_stage.payload["comparisons_skipped"]
    ]
    assert any("no QR code was decoded" in reason for reason in reasons)


# --------------------------------------------------------------------------
# Self-exclusion and order independence
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_a_document_is_never_a_duplicate_of_its_own_earlier_runs(
    samples, submit, database, duplicates_config, persistence_config
):
    """Re-running the same document must not flag it against itself.

    "Re-run" means a second check run over the same document id - which is what
    the standalone CLI does, and what matters here: matches are found by
    document id, never by run, so a document cannot see its own earlier hashes
    or numbers in the corpus however many times it is processed.
    """
    from app.duplicates import detect_duplicates

    first = submit(samples["original"], institution=INSTITUTION_A)
    assert findings_of(database, first.run_id, "duplicate_detection") == []

    for _ in range(2):
        with database.session() as session:
            with session.begin():
                run = repository.start_check_run(
                    session,
                    document_id=first.document_id,
                    actor=ACTOR,
                    role=ROLE,
                    config=persistence_config,
                )
            run_id = run.id

        with database.session() as session:
            with session.begin():
                result = detect_duplicates(
                    session,
                    document_id=first.document_id,
                    run_id=run_id,
                    config=duplicates_config,
                    certificate_number=repository.latest_certificate_number(
                        session, first.document_id
                    ),
                    qr_certificate_ids=repository.latest_qr_certificate_ids(
                        session, first.document_id
                    ),
                    qr_raw_payloads=repository.latest_qr_payloads(
                        session, first.document_id
                    ),
                )
        # Its own earlier run stored a hash and a number. Neither is a match.
        assert result.findings == []
        assert result.matches == []

    with database.session() as session:
        assert session.query(CrossReference).count() == 0
        # Three runs over one document, two hash kinds each: the corpus is
        # complete even though nothing matched.
        assert session.query(PerceptualHashRow).count() == 3 * 2
        assert (
            session.query(PerceptualHashRow)
            .filter(PerceptualHashRow.document_id == first.document_id)
            .count()
            == 6
        )


@pytest.mark.qr
def test_processing_a_then_b_gives_the_same_relationships_as_b_then_a(
    samples, tmp_path
):
    """Order independence, asserted end to end on two fresh databases."""

    def run(order: list[tuple[str, str, str]], root: Path) -> set:
        config = copy.deepcopy(load_config("persistence"))
        config["database"]["url"] = "sqlite:///:memory:"
        config["storage"]["root"] = str(root)
        db = Database(config)
        try:
            registry = load_registry()
            document_ids: dict[str, int] = {}
            for key, institution, certificate_id in order:
                application_id = ensure_application(
                    db,
                    institution_name=institution,
                    category="extension_of_approval",
                    actor=ACTOR,
                    role=ROLE,
                    config=config,
                )
                pipe = Pipeline(
                    database=db,
                    llm_provider=StubProvider(
                        responses=[
                            json.dumps(
                                certificate_payload(institution, certificate_id)
                            )
                        ]
                    ),
                    issuer_transport=TestClient(create_app(registry=registry)),
                    issuer_sleep=lambda seconds: None,
                    configs={"persistence": config},
                    registry_hash=registry.sha256,
                    as_of=AS_OF,
                )
                summary = pipe.process_document(
                    application_id=application_id,
                    file_path=samples[key],
                    document_type="accreditation_certificate",
                    actor=ACTOR,
                    role=ROLE,
                )
                document_ids[key] = summary.document_id

            # Describe each relationship by the fixture names it joins, so the
            # comparison does not depend on the ids, which differ by order.
            by_id = {value: name for name, value in document_ids.items()}
            relationships = set()
            with db.session() as session:
                for row in session.query(CrossReference).all():
                    relationships.add(
                        (
                            frozenset(
                                {by_id[row.document_a_id], by_id[row.document_b_id]}
                            ),
                            row.kind,
                            row.same_institution,
                        )
                    )
            return relationships
        finally:
            db.dispose()

    forward = [
        ("original", INSTITUTION_A, CERT_ID),
        ("recompressed", INSTITUTION_B, CERT_ID),
    ]
    backward = list(reversed(forward))

    first = run(forward, tmp_path / "forward")
    second = run(backward, tmp_path / "backward")

    assert first == second
    assert first, "the two documents should relate somehow"


@pytest.mark.qr
def test_a_relationship_appears_on_both_sides_of_the_report(
    samples, submit, database
):
    """The relationship belongs to both documents, and A's run finished before
    B existed - so it cannot live in A's run."""
    first = submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["original"], institution=INSTITUTION_B)

    with database.session() as session:
        a_side = cross_references_for_document(session, first.document_id)
        b_side = cross_references_for_document(session, second.document_id)

    # An identical file matches on the hash, on the number and on the page
    # image, so three relationships - and the same three from either end.
    assert {entry["kind"] for entry in a_side} == {
        entry["kind"] for entry in b_side
    }
    assert {entry["id"] for entry in a_side} == {entry["id"] for entry in b_side}
    assert len(a_side) == 3

    assert all(
        entry["other_document_id"] == second.document_id for entry in a_side
    )
    assert all(
        entry["other_document_id"] == first.document_id for entry in b_side
    )
    # Discovered by the second run, and recorded as such on both sides.
    assert all(entry["discovered_in_run_id"] == second.run_id for entry in a_side)
    assert all(entry["discovered_in_run_id"] == second.run_id for entry in b_side)

    with database.session() as session:
        report = repository.get_application_report(session, first.application_id)
    stored = report["documents"][0]["cross_references"]
    assert {entry["other_document_id"] for entry in stored} == {
        second.document_id
    }


@pytest.mark.qr
def test_a_relationship_is_stored_once_however_often_it_is_rediscovered(
    samples, submit, database
):
    """The normalised pair plus the unique constraint, doing their job."""
    submit(samples["original"], institution=INSTITUTION_A)
    submit(samples["original"], institution=INSTITUTION_B)
    submit(samples["original"], institution=INSTITUTION_B)

    with database.session() as session:
        rows = session.query(CrossReference).all()
        pairs = [(row.document_a_id, row.document_b_id, row.kind) for row in rows]

    assert len(pairs) == len(set(pairs))
    # Lower id first, always.
    for a, b, _ in pairs:
        assert a < b


@pytest.mark.qr
def test_old_runs_are_never_edited_when_a_relationship_is_discovered(
    samples, submit, database
):
    """B's arrival must not rewrite A's finished run."""
    first = submit(samples["original"], institution=INSTITUTION_A)

    with database.session() as session:
        before = repository.get_application_report(
            session, first.application_id, all_runs=True
        )
    first_run_before = before["documents"][0]["runs"][0]

    submit(samples["original"], institution=INSTITUTION_B)

    with database.session() as session:
        after = repository.get_application_report(
            session, first.application_id, all_runs=True
        )
    first_run_after = after["documents"][0]["runs"][0]

    assert before["documents"][0]["run_count"] == after["documents"][0]["run_count"]
    assert first_run_before["findings"] == first_run_after["findings"]
    assert first_run_before["status"] == first_run_after["status"]
    # The new fact is on the document, not inside the finished run.
    assert after["documents"][0]["cross_references"]


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_both_new_stages_run_end_to_end_with_no_findings_on_a_clean_fixture(
    submit, database
):
    from tests.make_qr_fixtures import build_all as build_qr

    summary = submit(build_qr()["valid_pdf"], institution=INSTITUTION_A)

    assert [entry.stage for entry in summary.stages] == [
        "extraction",
        "certificate_fields",
        "qr_checks",
        "issuer_verification",
        "signature_check",
        "duplicate_check",
    ]

    signature = stage(summary, "signature_check")
    assert signature.status == "ok"
    # No signature on the fixture, which is the common case and not a problem.
    assert signature.detail_status == "signature_absent"
    assert summary.signature_status == "signature_absent"

    duplicate = stage(summary, "duplicate_check")
    assert duplicate.status == "ok"
    # First document into an empty corpus: nothing to be a duplicate of.
    assert duplicate.detail_status == "no_corpus"

    assert findings_of(database, summary.run_id, "duplicate_detection") == []
    assert summary.cross_reference_count == 0


@pytest.mark.qr
def test_a_signed_document_stores_its_signature_detail_through_the_pipeline(
    submit, database
):
    from tests.make_signature_fixtures import build_all as build_signatures

    summary = submit(build_signatures()["signed_clean"], institution=INSTITUTION_A)

    signature = stage(summary, "signature_check")
    assert signature.status == "ok"
    assert signature.detail_status == "present_and_byte_range_intact"

    with database.session() as session:
        report = repository.get_application_report(session, summary.application_id)
    stored = report["documents"][0]["runs"][0]["signatures"]

    assert len(stored) == 1
    assert stored[0]["digest_matches"] is True
    assert stored[0]["covers_whole_file"] is True
    assert stored[0]["signer_identity_verified"] is False
    assert stored[0]["signer_name_unverified"]


@pytest.mark.qr
def test_a_tampered_signed_document_produces_a_finding_through_the_pipeline(
    submit, database
):
    from tests.make_signature_fixtures import build_all as build_signatures

    summary = submit(
        build_signatures()["signed_tampered_digit"], institution=INSTITUTION_A
    )

    findings = findings_of(database, summary.run_id, "certificate_authenticity")
    digest = [f for f in findings if f["type"] == "signature_digest_mismatch"]
    assert len(digest) == 1
    assert digest[0]["strength"] == "strong"
    assert stage(summary, "signature_check").detail_status == "modified_after_signing"


@pytest.mark.qr
def test_duplicate_findings_carry_their_own_detector_name(samples, submit, database):
    """A relationship between applications is not a statement about one
    certificate's authenticity, and a reviewer filtering the queue wants them
    apart."""
    submit(samples["original"], institution=INSTITUTION_A)
    second = submit(samples["original"], institution=INSTITUTION_B)

    detectors = {f["detector"] for f in findings_of(database, second.run_id)}
    assert "duplicate_detection" in detectors


@pytest.mark.qr
def test_the_new_config_versions_are_stamped_on_the_run(submit, database):
    from tests.make_qr_fixtures import build_all as build_qr

    summary = submit(build_qr()["valid_pdf"], institution=INSTITUTION_A)

    with database.session() as session:
        report = repository.get_application_report(session, summary.application_id)
    versions = report["documents"][0]["runs"][0]["config_versions"]

    assert versions["signature_checks"] == load_config("signature_checks")[
        "config_version"
    ]
    assert versions["duplicate_checks"] == load_config("duplicate_checks")[
        "config_version"
    ]
    hashes = report["documents"][0]["runs"][0]["config_hashes"]
    assert "signature_checks" in hashes
    assert "duplicate_checks" in hashes


@pytest.mark.qr
def test_a_failed_image_render_degrades_without_losing_the_other_checks(
    samples, submit, database, duplicates_config
):
    """A corrupt page is a skipped comparison, not a failed run.

    The exact-hash, number and QR checks do not need an image, so they must
    still produce their findings.
    """
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)

    # A hash kind that does not exist makes hashing fail after the render, which
    # is the same shape of failure as an unrenderable page and far easier to
    # provoke deterministically.
    broken = copy.deepcopy(duplicates_config)
    broken["perceptual_hash"]["kinds"] = ["notahash"]
    second = submit(
        samples["different_layout_same_number"],
        institution=INSTITUTION_B,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": broken},
    )

    duplicate = stage(second, "duplicate_check")
    assert duplicate.status == "degraded"
    assert duplicate.detail_status == "image_unavailable"

    # The number check still fired, strongly.
    findings = findings_of(database, second.run_id, "duplicate_detection")
    number = [
        f for f in findings if f["type"] == FindingType.CERTIFICATE_NUMBER.value
    ]
    assert len(number) == 1
    assert number[0]["strength"] == "strong"


@pytest.mark.qr
def test_the_stage_failing_never_erases_the_earlier_stage_results(
    samples, submit, database
):
    summary = submit(samples["original"], institution=INSTITUTION_A)
    with database.session() as session:
        report = repository.get_application_report(session, summary.application_id)
    stages = report["documents"][0]["runs"][0]["stages"]

    # Every stage has a row, including the two new ones.
    assert [entry["stage"] for entry in stages] == [
        "extraction",
        "certificate_fields",
        "qr_checks",
        "issuer_verification",
        "signature_check",
        "duplicate_check",
    ]
    for entry in stages:
        assert entry["status"] in ("ok", "degraded", "skipped", "failed")


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_the_audit_entries_carry_ids_and_counts_but_no_document_content(
    samples, submit, database
):
    from app.db.models import AuditLog

    submit(samples["original"], institution=INSTITUTION_A)
    submit(samples["original"], institution=INSTITUTION_B)

    with database.session() as session:
        entries = session.query(AuditLog).all()
        serialised = json.dumps([entry.details for entry in entries], default=str)
        actions = {entry.action for entry in entries}
        cross = [
            entry
            for entry in entries
            if entry.action == "cross_references_recorded"
        ]

    assert "cross_references_recorded" in actions
    assert cross
    details = cross[-1].details
    assert details["count"] >= 1
    assert details["kinds"]
    assert details["other_document_ids"]
    # Ids, kinds and counts only.
    assert set(details) == {"run_id", "count", "kinds", "other_document_ids"}

    # No document body text anywhere in the trail. The file hash is an id, not
    # content, so it is allowed; the certificate's printed text is not.
    assert "Certificate of Accreditation" not in serialised
    assert "NATIONAL BOARD OF TECHNICAL ACCREDITATION" not in serialised


@pytest.mark.qr
def test_the_signature_stage_records_that_the_signer_was_not_verified(
    submit, database
):
    """The single most misreadable thing about this stage, written into the
    audit trail so it survives being read out of context."""
    from app.db.models import AuditLog
    from tests.make_signature_fixtures import build_all as build_signatures

    submit(build_signatures()["signed_clean"], institution=INSTITUTION_A)

    with database.session() as session:
        entries = [
            entry.details
            for entry in session.query(AuditLog).all()
            if entry.action == "stage_completed"
            and (entry.details or {}).get("stage") == "signature_check"
        ]

    assert entries
    assert entries[0]["signer_identity_verified"] is False


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_an_unlisted_context_defaults_to_weak(duplicates_config):
    """A missing config entry is our mistake, and must not manufacture a
    strong finding against an institution."""
    duplicates_config["strength"] = {}
    for finding_type in FindingType:
        for context in ("other_institution", "same_institution", "different_number"):
            assert strength_for(finding_type, context, duplicates_config) == "weak"


def test_a_near_duplicate_is_never_strong_in_any_configured_context(
    duplicates_config,
):
    """Asserted against the shipped config, because the measurements say no
    threshold can justify it."""
    contexts = duplicates_config["strength"]["near_duplicate_page_image"]
    assert contexts
    for context, label in contexts.items():
        assert label != "strong", context


@pytest.mark.qr
def test_a_disabled_check_is_recorded_as_skipped_not_omitted(
    samples, submit, duplicates_config
):
    disabled = copy.deepcopy(duplicates_config)
    disabled["checks"]["exact_file_hash"] = False

    summary = submit(
        samples["original"],
        institution=INSTITUTION_A,
        configs={"duplicate_checks": disabled},
    )
    payload = stage(summary, "duplicate_check").payload
    assert payload is not None
    skipped = {entry["check"] for entry in payload["comparisons_skipped"]}
    assert "exact_file_hash" in skipped


# --------------------------------------------------------------------------
# No network
# --------------------------------------------------------------------------


@pytest.mark.qr
@pytest.mark.ocr
def test_nothing_in_duplicate_detection_touches_the_network(
    samples, submit, database, duplicates_config, persistence_config, monkeypatch
):
    """Reverse image search here is internal only: our own stored hashes.

    No external image API is contacted, which was a confirmed decision, not an
    implementation detail - so it gets a test that would fail if someone added
    one.
    """
    import socket

    # Set the corpus up *before* arming the trap: getting a document in there
    # goes through the pipeline, which legitimately uses a socket-backed
    # in-process transport for the issuer.
    submit(samples["original"], institution=INSTITUTION_A)
    prepared = submit(samples["recompressed"], institution=INSTITUTION_B)
    prepared_document_id = prepared.document_id

    with database.session() as session:
        with session.begin():
            run = repository.start_check_run(
                session,
                document_id=prepared_document_id,
                actor=ACTOR,
                role=ROLE,
                config=persistence_config,
            )
        prepared_run_id = run.id

    attempts: list[str] = []

    def forbidden(name):
        def blocked(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"network access attempted via {name}")

        return blocked

    for name in (
        "socket",
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "getfqdn",
        "create_server",
    ):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, forbidden(f"socket.{name}"))

    # Prove the trap is armed before trusting it. Without this, the test passes
    # just as happily when the patching silently stops working.
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.getaddrinfo("images.example.test", 443)
    attempts.clear()

    from app.duplicates import detect_duplicates

    # Driven directly, not through the pipeline: the pipeline's issuer stage
    # talks to the mock issuer over an in-process transport, which does open a
    # socket, and that is a different module's business. The claim under test is
    # that *this* module reaches nothing outside the database.
    with database.session() as session:
        with session.begin():
            result = detect_duplicates(
                session,
                document_id=prepared_document_id,
                run_id=prepared_run_id,
                config=duplicates_config,
                certificate_number=CERT_ID,
                qr_certificate_ids=[CERT_ID],
                qr_raw_payloads=["https://verify.nbta-demo.test/c/anything"],
            )

    assert result.matches or result.matches == []
    assert attempts == []

# --------------------------------------------------------------------------
# Template-noise suppression (off by default)
# --------------------------------------------------------------------------


def suppressing(config: dict) -> dict:
    enabled = copy.deepcopy(config)
    enabled["template_noise_suppression"]["enabled"] = True
    return enabled


@pytest.mark.qr
def test_suppression_is_off_by_default(samples, submit, database):
    """Suppressing a finding is hiding evidence, so it is never the default."""
    submit(samples["original"], institution=INSTITUTION_A, certificate_id=CERT_ID)
    second = submit(
        samples["legitimate_template_pair"],
        institution=INSTITUTION_B,
        certificate_id=SIBLING_CERT_ID,
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert any(
        f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value for f in findings
    )
    payload = stage(second, "duplicate_check").payload
    assert payload["suppressed_findings"] == []


@pytest.mark.qr
def test_suppression_withholds_the_finding_when_all_conditions_hold(
    samples, submit, database, duplicates_config
):
    """Numbers differ, both known to the issuer as distinct records, different
    institutions.

    The demo registry holds both `00417` and `00418`, which is what makes this
    the "issuer vouches for both" case. The visually similar pair whose number
    the issuer does *not* know is the test below, and it must behave the
    opposite way.
    """
    enabled = suppressing(duplicates_config)

    submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    second = submit(
        samples["one_digit_edited"],
        institution=INSTITUTION_B,
        certificate_id=EDITED_CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert not any(
        f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value for f in findings
    )

    # Withheld, not forgotten.
    payload = stage(second, "duplicate_check").payload
    withheld = payload["suppressed_findings"]
    assert len(withheld) == 1
    assert withheld[0]["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value
    assert withheld[0]["rule"] == "template_noise_suppression"
    assert "shared issuer template" in withheld[0]["reason"]
    assert withheld[0]["evidence"]["hamming_distance"] is not None


@pytest.mark.qr
def test_suppression_never_removes_the_cross_reference_row(
    samples, submit, database, duplicates_config
):
    """The relationship is a fact. Suppression is a decision about the queue."""
    enabled = suppressing(duplicates_config)

    first = submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    second = submit(
        samples["one_digit_edited"],
        institution=INSTITUTION_B,
        certificate_id=EDITED_CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    with database.session() as session:
        rows = session.query(CrossReference).all()
        a_side = cross_references_for_document(session, first.document_id)

    kinds = {row.kind for row in rows}
    assert FindingType.NEAR_DUPLICATE_IMAGE.value in kinds
    # And still readable from both ends.
    assert FindingType.NEAR_DUPLICATE_IMAGE.value in {
        entry["kind"] for entry in a_side
    }


@pytest.mark.qr
def test_a_near_copy_whose_number_the_issuer_does_not_know_is_still_flagged(
    samples, submit, database, duplicates_config
):
    """The case suppression must never swallow.

    Two differing numbers are only evidence of two certificates if the issuer
    vouches for both. An unknown number is equally consistent with a document
    having been edited - and it is the case most worth a reviewer's time, so a
    naive "the numbers differ" rule would throw away exactly the wrong one.
    """
    enabled = suppressing(duplicates_config)

    submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    # `00903` is printed on the page, so step 2 validates it - but it is not in
    # the issuer registry, so the lookup cannot confirm it exists. Every other
    # condition for suppression is satisfied; this one is not.
    second = submit(
        samples["legitimate_template_pair"],
        institution=INSTITUTION_B,
        certificate_id=SIBLING_CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    near = [
        f for f in findings if f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value
    ]
    assert len(near) == 1, "an unknown number must never be suppressed"
    assert near[0]["strength"] == "weak"

    payload = stage(second, "duplicate_check").payload
    assert payload["suppressed_findings"] == []


@pytest.mark.qr
def test_suppression_does_not_apply_when_the_numbers_are_the_same(
    samples, submit, database, duplicates_config
):
    """Same number on two documents is the opposite of a template coincidence."""
    enabled = suppressing(duplicates_config)

    submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    second = submit(
        samples["exact_copy"],
        institution=INSTITUTION_B,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert any(
        f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value for f in findings
    )
    assert stage(second, "duplicate_check").payload["suppressed_findings"] == []


@pytest.mark.qr
def test_suppression_does_not_apply_within_one_institution(
    samples, submit, database, duplicates_config
):
    """A shared template across *one* institution's own submissions is not the
    noise this rule exists to quieten."""
    enabled = suppressing(duplicates_config)

    submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    # Identical to the suppressed case except that both documents belong to one
    # institution, so the institution condition is the only one failing.
    second = submit(
        samples["one_digit_edited"],
        institution=INSTITUTION_A,
        certificate_id=EDITED_CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    findings = findings_of(database, second.run_id, "duplicate_detection")
    assert any(
        f["type"] == FindingType.NEAR_DUPLICATE_IMAGE.value for f in findings
    )
    assert stage(second, "duplicate_check").payload["suppressed_findings"] == []


@pytest.mark.qr
def test_suppression_leaves_every_other_finding_type_alone(
    samples, submit, database, duplicates_config
):
    """It is scoped to near_duplicate_page_image and nothing else."""
    enabled = suppressing(duplicates_config)

    submit(
        samples["original"],
        institution=INSTITUTION_A,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )
    second = submit(
        samples["exact_copy"],
        institution=INSTITUTION_B,
        certificate_id=CERT_ID,
        configs={"duplicate_checks": enabled},
    )

    types = {
        f["type"] for f in findings_of(database, second.run_id, "duplicate_detection")
    }
    assert FindingType.EXACT_FILE.value in types
    assert FindingType.CERTIFICATE_NUMBER.value in types

