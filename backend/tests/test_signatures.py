"""Tests for step 6 part A: PDF signature integrity.

Every fixture is generated in `tests/make_signature_fixtures.py` from a
throwaway RSA key created in memory. Nothing here touches the network, and no
private key is ever written to disk.

These tests are fast - signing is a couple of RSA operations - so they stay in
the fast suite unmarked.
"""

from __future__ import annotations

import copy
import hashlib
import io

import pytest

from app.core.config import load_config
from app.signatures import FindingType, SignatureStatus, check_signatures
from app.signatures.checker import strength_for
from tests.make_signature_fixtures import (
    ALTERED_CERT_ID,
    CERT_ID,
    SIGNER_COMMON_NAME,
    build_all,
)


@pytest.fixture(scope="module")
def samples() -> dict:
    return build_all()


@pytest.fixture
def config() -> dict:
    return copy.deepcopy(load_config("signature_checks"))


def types(result) -> list[str]:
    return [finding.type.value for finding in result.findings]


# --------------------------------------------------------------------------
# The six cases
# --------------------------------------------------------------------------


def test_a_clean_signed_pdf_produces_no_findings_but_stores_the_detail(samples):
    """No findings, and still a full record.

    The record matters as much as the silence: a findings table alone cannot
    distinguish "one intact signature covering the whole file" from "no
    signature at all", and those are different facts about a document.
    """
    result = check_signatures(samples["signed_clean"])

    assert result.findings == []
    assert result.status is SignatureStatus.PRESENT_INTACT

    assert len(result.signatures) == 1
    detail = result.signatures[0]
    assert detail.present is True
    assert detail.parsed is True
    assert detail.covers_whole_file is True
    assert detail.digest_matches is True
    assert detail.digest_algorithm == "sha256"
    assert detail.computed_digest_hex == detail.signed_digest_hex
    assert detail.byte_range is not None and len(detail.byte_range) == 4
    assert detail.field_name == "Signature1"


def test_one_byte_changed_inside_the_signed_range_is_a_digest_mismatch(samples):
    """The case the whole check exists for.

    The signature still parses and its byte range still covers the file; only
    the content changed. Nothing but recomputing the digest catches this.
    """
    result = check_signatures(samples["signed_tampered_digit"])

    assert types(result) == [FindingType.DIGEST_MISMATCH.value]
    assert result.status is SignatureStatus.MODIFIED_AFTER_SIGNING

    detail = result.signatures[0]
    assert detail.digest_matches is False
    assert detail.computed_digest_hex != detail.signed_digest_hex
    # Coverage is unaffected: the edit was inside the covered range, not after
    # it. Reporting this as a coverage problem would point a reviewer at the
    # wrong thing entirely.
    assert detail.covers_whole_file is True

    finding = result.findings[0]
    assert finding.strength == "strong"
    assert "no longer matches what was signed" in finding.reason
    # A reviewer sees the two values that were compared, not just a verdict.
    assert finding.evidence["computed_digest_hex"]
    assert finding.evidence["signed_digest_hex"]
    assert (
        finding.evidence["computed_digest_hex"]
        != finding.evidence["signed_digest_hex"]
    )


def test_an_incremental_update_after_signing_is_flagged_with_the_caveat(samples):
    """An append is reported, and the reason says it may be legitimate.

    This is the finding most likely to be misread. The signature is *intact*;
    bytes were added after the revision it covers. Tools that add long-term
    validation data do exactly this, so the wording has to carry the caveat or
    the finding becomes an accusation.
    """
    result = check_signatures(samples["signed_incremental_update"])

    assert types(result) == [FindingType.BYTES_AFTER_SIGNED_REVISION.value]
    detail = result.signatures[0]
    # Intact over what it covers - which is the whole point.
    assert detail.digest_matches is True
    assert detail.covers_whole_file is False
    assert detail.coverage.value == "entire_revision"

    finding = result.findings[0]
    assert finding.strength == "medium"
    assert "legitimate" in finding.reason
    assert "manual verification" in finding.reason

    # Evidence is a measurement, never a content dump.
    appended = finding.evidence["appended"]
    assert appended["byte_length"] > 0
    assert appended["object_count"] >= 1
    assert appended["revision_count_after"] == 1
    assert finding.evidence["digest_over_covered_bytes_matches"] is True


def test_a_signature_that_does_not_cover_the_whole_file_is_flagged(samples):
    """A forged `/ByteRange` that stops short of a revision boundary.

    Both findings fire, and that is correct rather than redundant: the range
    was altered, so the digest over the altered range cannot match either. A
    reviewer needs to see both, because "the signature lies about what it
    covers" and "the covered bytes do not hash right" are different claims.
    """
    result = check_signatures(samples["signed_short_byte_range"])

    assert FindingType.DOES_NOT_COVER_DOCUMENT.value in types(result)
    assert result.status is SignatureStatus.MODIFIED_AFTER_SIGNING

    detail = result.signatures[0]
    assert detail.coverage.value == "contiguous_block_from_start"
    assert detail.covers_whole_file is False

    coverage_finding = next(
        f
        for f in result.findings
        if f.type is FindingType.DOES_NOT_COVER_DOCUMENT
    )
    assert coverage_finding.strength == "strong"
    assert "does not cover the whole document" in coverage_finding.reason
    assert "legitimate" in coverage_finding.reason


def test_a_corrupt_signature_routes_to_manual_review_not_an_accusation(samples):
    """A signature we cannot read is one we cannot judge.

    The fallback path matters here: a malformed CMS blob makes pyHanko's
    enumeration raise, which would otherwise take out *every* signature in the
    file and report it as unsigned - the opposite of what is true.
    """
    result = check_signatures(samples["signed_corrupt_cms"])

    assert types(result) == [FindingType.UNPARSEABLE.value]
    assert result.status is SignatureStatus.UNVERIFIABLE

    detail = result.signatures[0]
    assert detail.present is True
    assert detail.parsed is False
    assert detail.digest_matches is None
    assert detail.parse_error

    finding = result.findings[0]
    assert finding.strength == "weak"
    assert "could not be read" in finding.reason
    assert "manual verification" in finding.reason
    # Nothing in the wording asserts wrongdoing.
    for forbidden in ("fake", "forged", "fraudulent", "tampered", "deepfake"):
        assert forbidden not in finding.reason.lower()


def test_an_unsigned_pdf_is_quiet_by_default(samples):
    """Most EoA attachments are scans that were never signed."""
    result = check_signatures(samples["unsigned"])

    assert result.findings == []
    assert result.status is SignatureStatus.ABSENT
    assert result.signatures == []


def test_an_unsigned_pdf_is_flagged_only_when_config_expects_a_signature(
    samples, config
):
    config["expectations"]["signature_expected_document_types"] = [
        "accreditation_certificate"
    ]

    quiet = check_signatures(
        samples["unsigned"], config=config, document_type="land_deed"
    )
    assert quiet.findings == []

    flagged = check_signatures(
        samples["unsigned"],
        config=config,
        document_type="accreditation_certificate",
    )
    assert types(flagged) == [FindingType.UNSIGNED_WHERE_EXPECTED.value]
    assert flagged.findings[0].strength == "weak"


def test_an_expected_signature_can_be_keyed_on_the_issuing_body(samples, config):
    config["expectations"]["signature_expected_issuers"] = [
        "National Board of Technical Accreditation (Demo)"
    ]

    # Matched case-insensitively after whitespace collapse, because this value
    # comes out of OCR and an LLM, not out of a dropdown.
    flagged = check_signatures(
        samples["unsigned"],
        config=config,
        issuing_body="  national board of   technical accreditation (demo) ",
    )
    assert types(flagged) == [FindingType.UNSIGNED_WHERE_EXPECTED.value]


# --------------------------------------------------------------------------
# Signer identity is never verified
# --------------------------------------------------------------------------


def test_the_signer_is_reported_and_explicitly_not_verified(samples):
    """The name is recorded because a reviewer wants it; labelled because it
    proves nothing."""
    result = check_signatures(samples["signed_clean"])
    detail = result.signatures[0]

    assert SIGNER_COMMON_NAME in (detail.signer_name_unverified or "")
    assert detail.claimed_signing_time_unverified is not None
    assert detail.signer_identity_verified is False

    payload = result.to_dict()
    assert payload["signatures"][0]["signer_identity_verified"] is False
    assert "Signer identity is NOT verified" in payload["scope_note"]


def test_every_result_carries_the_scope_note(samples):
    """No consumer should have to remember the scope. Each result restates it."""
    for key in (
        "unsigned",
        "signed_clean",
        "signed_tampered_digit",
        "signed_corrupt_cms",
    ):
        payload = check_signatures(samples[key]).to_dict()
        assert "Integrity only" in payload["scope_note"]
        assert "trust store" in payload["scope_note"]


def test_no_signature_detail_can_claim_a_verified_signer(samples):
    """There is no code path that sets it True, and this asserts that."""
    for key, path in build_all().items():
        result = check_signatures(path)
        for detail in result.signatures:
            assert detail.signer_identity_verified is False, key


# --------------------------------------------------------------------------
# The digest comparison itself
# --------------------------------------------------------------------------


def test_our_recomputation_agrees_with_pyhankos_own(samples):
    """Guards the one place this module could have been vacuous.

    pyHanko exposes `external_digests`, which looks like the signed digest and
    is not - it is a cache of *recomputed* digests, so comparing it against our
    recomputation would always agree and the check would pass on a tampered
    file. We therefore compare our hashlib recomputation against the CMS
    `messageDigest`. This test checks the other half: that our recomputation
    matches pyHanko's independent `compute_digest()`, on both a clean and a
    tampered file.
    """
    from pyhanko.pdf_utils.reader import PdfFileReader

    for key in ("signed_clean", "signed_tampered_digit"):
        data = samples[key].read_bytes()
        reader = PdfFileReader(io.BytesIO(data))
        signature = list(reader.embedded_signatures)[0]
        signature.compute_integrity_info()

        byte_range = [int(value) for value in signature.byte_range]
        mine = hashlib.sha256()
        for offset, length in zip(byte_range[0::2], byte_range[1::2]):
            mine.update(data[offset : offset + length])

        assert mine.digest() == signature.compute_digest(), key


def test_the_signed_digest_does_not_change_when_the_file_is_edited(samples):
    """The signed digest is the fixed point of the comparison.

    If it moved with the file, as a recomputed digest does, nothing could be
    detected. Clean and tampered must report the *same* signed digest and
    different computed ones.
    """
    clean = check_signatures(samples["signed_clean"]).signatures[0]
    tampered = check_signatures(samples["signed_tampered_digit"]).signatures[0]

    assert clean.signed_digest_hex == tampered.signed_digest_hex
    assert clean.computed_digest_hex != tampered.computed_digest_hex


def test_a_weak_digest_algorithm_is_reported_when_configured(samples, config):
    """sha256 is not weak; the check only fires on what config lists."""
    assert check_signatures(samples["signed_clean"], config=config).findings == []

    config["integrity"]["weak_digest_algorithms"] = ["sha256"]
    result = check_signatures(samples["signed_clean"], config=config)
    assert types(result) == [FindingType.WEAK_DIGEST_ALGORITHM.value]
    assert "sha256" in result.findings[0].reason


# --------------------------------------------------------------------------
# Errors as data
# --------------------------------------------------------------------------


def test_a_missing_file_is_reported_not_raised(tmp_path):
    result = check_signatures(tmp_path / "nope.pdf")
    assert result.error.value == "file_not_found"
    assert result.status is SignatureStatus.UNVERIFIABLE


def test_a_non_pdf_has_nothing_to_check_and_says_so(tmp_path):
    """An image attachment cannot carry a PDF signature. Not a failure."""
    path = tmp_path / "scan.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")

    result = check_signatures(path)
    assert result.error.value == "not_a_pdf"
    assert result.status is SignatureStatus.ABSENT
    assert result.findings == []


def test_a_corrupt_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nthis is not a pdf body at all\n")

    result = check_signatures(path)
    # Either unreadable or simply signature-free; what matters is that it
    # returned a result instead of raising.
    assert result.status in (
        SignatureStatus.ABSENT,
        SignatureStatus.UNVERIFIABLE,
    )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_an_unlisted_finding_type_defaults_to_weak(config):
    """A missing config entry is our mistake, and must not manufacture a
    strong finding against an institution."""
    config["strength"] = {}
    for finding_type in FindingType:
        assert strength_for(finding_type, config) == "weak"


def test_strength_labels_come_from_config(samples, config):
    config["strength"]["signature_digest_mismatch"] = "weak"
    result = check_signatures(samples["signed_tampered_digit"], config=config)
    assert result.findings[0].strength == "weak"


def test_the_config_version_is_stamped_on_every_result(samples, config):
    result = check_signatures(samples["signed_clean"], config=config)
    assert result.audit is not None
    assert result.audit.config_version == config["config_version"]
    assert result.audit.input_sha256
    assert result.audit.library == "pyhanko"


def test_the_signature_cap_is_honoured(samples, config):
    config["detection"]["max_signatures"] = 0
    result = check_signatures(samples["signed_clean"], config=config)
    assert result.signatures == []


# --------------------------------------------------------------------------
# No network
# --------------------------------------------------------------------------


def test_nothing_in_the_signature_check_touches_the_network(samples, monkeypatch):
    """A signature names a certificate authority, and a URL for its revocation
    list. We read those names; we never contact them.

    That is the scope decision made concrete: certificate-chain validation is
    exactly the part of signature verification that goes online, and it is the
    part we do not do.
    """
    import socket

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
        socket.getaddrinfo("ocsp.example.test", 443)
    attempts.clear()

    for key in (
        "unsigned",
        "signed_clean",
        "signed_tampered_digit",
        "signed_incremental_update",
        "signed_short_byte_range",
        "signed_corrupt_cms",
    ):
        check_signatures(samples[key])

    assert attempts == []


# --------------------------------------------------------------------------
# Language rules
# --------------------------------------------------------------------------


def test_no_finding_reason_accuses_anyone(samples, config):
    """The project's language rules, asserted rather than trusted."""
    config["expectations"]["signature_expected_by_default"] = True
    config["integrity"]["weak_digest_algorithms"] = ["sha256"]

    forbidden = ("fake", "forged", "fraudulent", "deepfake", "counterfeit")
    for path in build_all().values():
        for finding in check_signatures(path, config=config).findings:
            lowered = finding.reason.lower()
            for word in forbidden:
                assert word not in lowered, (path.name, finding.reason)


def test_every_finding_carries_evidence_and_a_strength(samples, config):
    config["expectations"]["signature_expected_by_default"] = True
    for path in build_all().values():
        for finding in check_signatures(path, config=config).findings:
            assert finding.strength in ("strong", "medium", "weak")
            assert finding.reason.strip()
            assert finding.evidence


def test_no_evidence_contains_document_text(samples):
    """Evidence is digests, offsets and counts - never the document's content.

    The appended region in particular is measured, not quoted: a byte-length
    and an object count tell a reviewer whether one dictionary or two hundred
    objects were added, which is what the decision turns on.
    """
    for key in ("signed_tampered_digit", "signed_incremental_update"):
        for finding in check_signatures(samples[key]).findings:
            serialised = str(finding.evidence)
            assert CERT_ID not in serialised
            assert ALTERED_CERT_ID not in serialised
            assert "Shantiniketan" not in serialised


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_emits_json_for_a_clean_file(samples, capsys):
    import json

    from app.signatures.cli import main

    exit_code = main([str(samples["signed_clean"])])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "present_and_byte_range_intact"
    assert payload["findings"] == []
    assert payload["signatures"][0]["signer_identity_verified"] is False


def test_cli_reports_a_tampered_file_without_failing(samples, capsys):
    import json

    from app.signatures.cli import main

    # A finding is a result, not an error: the check ran and reported.
    exit_code = main([str(samples["signed_tampered_digit"]), "--compact"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "modified_after_signing"
    assert payload["findings"][0]["type"] == "signature_digest_mismatch"
