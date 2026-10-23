"""Tests for step 4: the mock issuer service and the verification client.

Fast by design: every test drives the FastAPI app in-process through
TestClient, so nothing opens a port or touches the network. Sleeps are
captured rather than performed, which lets the retry schedule be asserted
exactly instead of inferred from elapsed time.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.core.verification import VerificationStatus, parse_status
from app.issuer import verify_certificate
from app.issuer.client import IssuerClient, IssuerUnreachableError
from app.issuer.models import FieldOutcome, FindingType, IdOrigin, LookupOutcome
from app.issuer.normalise import normalise_name, programme_set
from app.issuer.status import PRECEDENCE
from app.issuer.verifier import qr_id_from_scan, submitted_from_extraction
from app.mock_issuer.registry import load_registry
from app.mock_issuer.seed import (
    ANCHOR_CERTIFICATE_ID,
    ANCHOR_INSTITUTION,
    ANCHOR_PROGRAMME,
    ANCHOR_VALID_FROM,
    ANCHOR_VALID_TO,
    EXPIRED_ID,
    ISSUING_BODY,
    NAME_VARIANT_ID,
    OTHER_INSTITUTION_ID,
    PENDING_ID,
    REVOKED_ID,
    SUSPENDED_ID,
    build_registry,
    registry_hash,
)
from app.mock_issuer.service import FaultInjection, create_app

AS_OF = "2026-01-15"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def registry():
    return load_registry()


@pytest.fixture
def issuer_config() -> dict:
    return load_config("issuer_verification")


@pytest.fixture
def client(registry):
    return TestClient(create_app(registry=registry))


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def sleeper(sleeps):
    return sleeps.append


def submitted(**overrides):
    """The anchor document's step 2 values, as extracted."""
    base = {
        "certificate_number": ANCHOR_CERTIFICATE_ID,
        "institution_name": ANCHOR_INSTITUTION,
        "issuing_body": ISSUING_BODY,
        # As step 2's model actually reads the comma-joined programme line.
        "programmes": ["Bachelor of Technology", "Computer Science and Engineering"],
        "grade": "A+",
        "valid_from": ANCHOR_VALID_FROM,
        "valid_to": ANCHOR_VALID_TO,
    }
    base.update(overrides)
    return base


def run(client, sleeper, **kwargs):
    kwargs.setdefault("as_of", AS_OF)
    document = kwargs.pop("document", None)
    return verify_certificate(
        document if document is not None else submitted(),
        transport=client,
        sleep=sleeper,
        **kwargs,
    )


def comparison(result, name, lookup_index=0):
    for entry in result.lookups[lookup_index].comparisons:
        if entry.name == name:
            return entry
    raise AssertionError(f"no comparison for {name}")


def finding_types(result) -> list[str]:
    return [f.type.value for f in result.findings]


# --------------------------------------------------------------------------
# The registry and the service
# --------------------------------------------------------------------------


def test_the_seed_is_deterministic():
    """Two builds must be byte-identical, or the eval set is not comparable."""
    first = build_registry()
    second = build_registry()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert registry_hash(first) == registry_hash(second)


def test_the_registry_holds_twelve_institutions(registry):
    institutions = {record["institution_name"] for record in registry.records}

    assert len(institutions) == 12
    assert len(registry.records) >= 12


def test_every_registry_domain_is_fictional(registry):
    """All demo domains use the reserved .test TLD, which cannot resolve."""
    text = json.dumps(registry.data)

    for suffix in (".com", ".org", ".in", ".net", ".edu", ".gov"):
        assert suffix not in text, f"{suffix} appears in the registry"


def test_the_anchor_certificate_is_an_exact_match(registry):
    record = registry.find(ANCHOR_CERTIFICATE_ID)

    assert record is not None
    assert record["institution_name"] == ANCHOR_INSTITUTION
    assert record["programmes"] == [ANCHOR_PROGRAMME]
    assert record["valid_from"] == ANCHOR_VALID_FROM
    assert record["valid_to"] == ANCHOR_VALID_TO
    assert record["issuer_status"] == "active"


def test_the_registry_lookup_is_exact_not_fuzzy(registry):
    """A fuzzy registry would hand back a record for an altered number."""
    assert registry.find("NBTA/DEMO/2024/0041") is None
    assert registry.find("NBTA/DEMO/2024/00418") is not None


def test_the_service_serves_an_id_with_slashes_as_a_query_parameter(client):
    response = client.get(
        "/v1/certificates", params={"certificate_id": ANCHOR_CERTIFICATE_ID}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["record"]["certificate_id"] == ANCHOR_CERTIFICATE_ID


def test_the_service_serves_a_percent_encoded_path(client):
    """The round trip that breaks if the slashes are mishandled anywhere."""
    response = client.get(f"/v1/certificates/{quote(ANCHOR_CERTIFICATE_ID, safe='')}")

    assert response.status_code == 200
    assert response.json()["record"]["certificate_id"] == ANCHOR_CERTIFICATE_ID


def test_the_service_answers_404_for_an_unknown_id(client):
    response = client.get(
        "/v1/certificates", params={"certificate_id": "NBTA/DEMO/2024/99999"}
    )

    assert response.status_code == 404
    assert response.json()["lookup_status"] == "not_found"


def test_the_service_answers_202_for_the_pending_id(client):
    response = client.get("/v1/certificates", params={"certificate_id": PENDING_ID})

    assert response.status_code == 202
    assert response.json()["lookup_status"] == "pending"


def test_fault_injection_is_inert_by_default():
    """A mock that failed by default would make a passing test meaningless."""
    faults = FaultInjection()

    assert faults.active() is False
    assert faults.should_fail_now() is False

    client = TestClient(create_app())
    response = client.get(
        "/v1/certificates", params={"certificate_id": ANCHOR_CERTIFICATE_ID}
    )
    assert response.status_code == 200


def test_the_path_route_handles_a_doubly_encoded_id(client):
    response = client.get(
        f"/v1/certificates/{quote(quote(ANCHOR_CERTIFICATE_ID, safe=''), safe='')}"
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_the_anchor_certificate_verifies(client, sleeper):
    result = run(client, sleeper)

    assert result.status is VerificationStatus.VERIFIED
    assert result.status_reasons == ["all_fields_match"]
    assert result.findings == []

    lookup = result.lookups[0]
    assert lookup.outcome is LookupOutcome.FOUND
    assert lookup.id_origin is IdOrigin.DOCUMENT_NUMBER
    assert lookup.trust == "normal"
    assert all(c.outcome is FieldOutcome.MATCH for c in lookup.comparisons)


def test_the_comma_split_programme_still_matches(client, sleeper):
    """Step 2 reads one comma-joined programme line as two items.

    The registry holds it as one string. Both must canonicalise to the same
    set, or every real extraction would report a programme mismatch.
    """
    result = run(client, sleeper)

    assert comparison(result, "programmes").outcome is FieldOutcome.MATCH
    assert programme_set([ANCHOR_PROGRAMME], load_config("issuer_verification")) == (
        programme_set(
            ["Bachelor of Technology", "Computer Science and Engineering"],
            load_config("issuer_verification"),
        )
    )


def test_the_existing_step_2_fixture_chain_verifies(client, sleeper):
    """A real step 2 output shape, straight into step 4."""
    extraction = {
        "source_path": "tests/fixtures/certificate_text_layer.pdf",
        "status": "ok",
        "fields": {
            "certificate_number": {"value": ANCHOR_CERTIFICATE_ID, "validated": True},
            "institution_name": {"value": ANCHOR_INSTITUTION, "validated": True},
            "issuing_body": {"value": ISSUING_BODY, "validated": True},
            "programme": {
                "value": [
                    "Bachelor of Technology",
                    "Computer Science and Engineering",
                ],
                "validated": True,
            },
            # The fixture certificate prints no grade, so step 2 returns null.
            "accreditation_status_or_grade": {"value": None, "validated": False},
            "valid_from": {
                "value": "01 July 2024",
                "value_iso": ANCHOR_VALID_FROM,
                "validated": True,
            },
            "valid_to": {
                "value": "30 June 2027",
                "value_iso": ANCHOR_VALID_TO,
                "validated": True,
            },
        },
    }

    result = run(client, sleeper, document=submitted_from_extraction(extraction))

    assert result.status is VerificationStatus.VERIFIED
    assert comparison(result, "grade").outcome is FieldOutcome.NOT_COMPARED


def test_an_unvalidated_field_is_not_used(client, sleeper):
    """Step 2 already rejected it; comparing it would invent a mismatch."""
    extraction = {
        "fields": {
            "certificate_number": {"value": ANCHOR_CERTIFICATE_ID, "validated": True},
            "institution_name": {"value": "Some Other College", "validated": False},
        }
    }

    document = submitted_from_extraction(extraction)

    assert document["institution_name"] is None
    result = run(client, sleeper, document=document)
    assert comparison(result, "institution_name").outcome is FieldOutcome.NOT_COMPARED


# --------------------------------------------------------------------------
# Not found
# --------------------------------------------------------------------------


def test_a_one_digit_different_number_is_not_found(client, sleeper):
    result = run(
        client, sleeper, document=submitted(certificate_number="NBTA/DEMO/2024/00419")
    )

    assert result.status is VerificationStatus.MANUAL_REVIEW_REQUIRED
    assert result.lookups[0].outcome is LookupOutcome.NOT_FOUND

    finding = next(
        f for f in result.findings if f.type is FindingType.RECORD_NOT_FOUND
    )
    assert finding.strength == "strong"
    assert finding.evidence["certificate_id"] == "NBTA/DEMO/2024/00419"
    # Not an accusation: a not-found can also be a gap in the issuer's records.
    assert "manual" in finding.reason.lower()


# --------------------------------------------------------------------------
# Field comparison
# --------------------------------------------------------------------------


def test_a_legitimate_name_variant_still_matches(client, sleeper):
    """The issuer holds "Konkan Coast Inst. of Engg."; the document spells it out."""
    result = run(
        client,
        sleeper,
        document=submitted(
            certificate_number=NAME_VARIANT_ID,
            institution_name="Konkan Coast Institute of Engineering",
            programmes=[
                "Bachelor of Technology, Electronics and Communication Engineering"
            ],
            grade="A",
            valid_from="2024-07-01",
            valid_to="2029-06-30",
        ),
    )

    assert comparison(result, "institution_name").outcome is FieldOutcome.MATCH
    assert result.status is VerificationStatus.VERIFIED


def test_a_genuinely_different_institution_is_a_mismatch(client, sleeper):
    result = run(
        client, sleeper, document=submitted(institution_name="Gomati Valley College of Engineering")
    )

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    entry = comparison(result, "institution_name")
    assert entry.outcome is FieldOutcome.MISMATCH

    finding = next(f for f in result.findings if f.type is FindingType.FIELD_MISMATCH)
    assert finding.strength == "strong"
    assert "mismatch detected" in finding.reason.lower()
    assert finding.evidence["field_compared"] == "institution_name"
    assert finding.evidence["issuer_value"] == ANCHOR_INSTITUTION


def test_a_different_programme_is_a_mismatch(client, sleeper):
    result = run(
        client, sleeper, document=submitted(programmes=["Bachelor of Arts, History"])
    )

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    entry = comparison(result, "programmes")
    assert entry.outcome is FieldOutcome.MISMATCH
    assert entry.method == "programme_not_in_issuer_record"


def test_a_different_date_is_a_mismatch(client, sleeper):
    result = run(client, sleeper, document=submitted(valid_to="2028-06-30"))

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    entry = comparison(result, "valid_to")
    assert entry.outcome is FieldOutcome.MISMATCH
    assert entry.submitted_value == "2028-06-30"
    assert entry.issuer_value == ANCHOR_VALID_TO


def test_a_different_grade_is_a_mismatch(client, sleeper):
    """A++ and A+ are different outcomes, so the grade check is exact."""
    result = run(client, sleeper, document=submitted(grade="A++"))

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    assert comparison(result, "grade").outcome is FieldOutcome.MISMATCH


def test_dates_in_a_different_format_are_not_a_mismatch(client, sleeper):
    result = run(client, sleeper, document=submitted(valid_to="2027-06-30T00:00:00"))

    assert comparison(result, "valid_to").outcome is FieldOutcome.MATCH


def test_null_submitted_fields_are_not_compared_not_mismatched(client, sleeper):
    result = run(
        client,
        sleeper,
        document=submitted(
            institution_name=None, grade=None, programmes=None, valid_to=None
        ),
    )

    for name in ("institution_name", "grade", "programmes", "valid_to"):
        assert comparison(result, name).outcome is FieldOutcome.NOT_COMPARED

    # Nothing conflicted, so nothing is a mismatch.
    assert result.status is VerificationStatus.VERIFIED
    assert FindingType.FIELD_MISMATCH.value not in finding_types(result)


def test_an_empty_programme_list_is_not_compared(client, sleeper):
    result = run(client, sleeper, document=submitted(programmes=[]))

    assert comparison(result, "programmes").outcome is FieldOutcome.NOT_COMPARED


def test_the_name_similarity_threshold_is_configurable(client, sleeper, issuer_config):
    config = copy.deepcopy(issuer_config)
    config["comparison"]["names"]["min_similarity"] = 0.999
    config["comparison"]["names"]["abbreviations"] = {}

    result = verify_certificate(
        submitted(institution_name="Shantiniketan Institute of Technolgy"),
        transport=client,
        sleep=sleeper,
        config=config,
        as_of=AS_OF,
    )

    assert comparison(result, "institution_name").outcome is FieldOutcome.MISMATCH


def test_abbreviation_expansion_is_configurable(issuer_config):
    config = copy.deepcopy(issuer_config)

    assert normalise_name("Konkan Coast Inst. of Engg.", config) == normalise_name(
        "Konkan Coast Institute of Engineering", config
    )

    config["comparison"]["names"]["abbreviations"] = {}
    assert normalise_name("Konkan Coast Inst. of Engg.", config) != normalise_name(
        "Konkan Coast Institute of Engineering", config
    )


def test_a_subset_of_issuer_programmes_matches_when_configured(
    client, sleeper, issuer_config
):
    document = submitted(
        certificate_number=OTHER_INSTITUTION_ID,
        institution_name="Gomati Valley College of Engineering",
        grade="A",
        programmes=["Bachelor of Technology"],
    )

    result = verify_certificate(
        document, transport=client, sleep=sleeper, config=issuer_config, as_of=AS_OF
    )
    assert comparison(result, "programmes").outcome is FieldOutcome.MATCH

    strict = copy.deepcopy(issuer_config)
    strict["comparison"]["programmes"]["allow_subset"] = False
    result = verify_certificate(
        document, transport=client, sleep=sleeper, config=strict, as_of=AS_OF
    )
    assert comparison(result, "programmes").outcome is FieldOutcome.MISMATCH


# --------------------------------------------------------------------------
# Issuer-side conditions
# --------------------------------------------------------------------------


def test_an_expired_record_is_expired(client, sleeper):
    result = run(
        client,
        sleeper,
        document=submitted(
            certificate_number=EXPIRED_ID,
            institution_name="Harita Institute of Science and Technology",
            programmes=["Bachelor of Technology, Civil Engineering"],
            grade="B++",
            valid_from="2016-07-01",
            valid_to="2019-06-30",
        ),
    )

    assert result.status is VerificationStatus.EXPIRED
    finding = next(f for f in result.findings if f.type is FindingType.EXPIRED)
    assert finding.evidence["as_of"] == AS_OF


def test_a_revoked_record_is_a_mismatch_with_a_strong_finding(client, sleeper):
    result = run(
        client,
        sleeper,
        document=submitted(
            certificate_number=REVOKED_ID,
            institution_name="Nilgiri College of Engineering and Technology",
            programmes=["Bachelor of Technology, Mechanical Engineering"],
            grade="B+",
            valid_from="2023-07-01",
            valid_to="2028-06-30",
        ),
    )

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    finding = next(
        f for f in result.findings if f.type is FindingType.STATUS_NOT_ACTIVE
    )
    assert finding.strength == "strong"
    assert "revoked" in finding.reason
    assert finding.evidence["issuer_value"] == "revoked"


def test_a_suspended_record_is_a_mismatch_with_a_strong_finding(client, sleeper):
    result = run(
        client,
        sleeper,
        document=submitted(
            certificate_number=SUSPENDED_ID,
            institution_name="Nilgiri College of Engineering and Technology",
            programmes=["Bachelor of Technology, Civil Engineering"],
            grade="B++",
            valid_from="2023-07-01",
            valid_to="2028-06-30",
        ),
    )

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    finding = next(
        f for f in result.findings if f.type is FindingType.STATUS_NOT_ACTIVE
    )
    assert "suspended" in finding.reason


def test_a_pending_answer_is_verification_pending(client, sleeper):
    result = run(client, sleeper, document=submitted(certificate_number=PENDING_ID))

    assert result.status is VerificationStatus.VERIFICATION_PENDING
    assert result.lookups[0].outcome is LookupOutcome.PENDING
    # Nothing was compared, so nothing is claimed.
    assert result.lookups[0].comparisons == []


def test_a_mismatch_outranks_expiry(client, sleeper):
    """Expiry is a fact about dates; a mismatch is a question about the document."""
    result = run(
        client,
        sleeper,
        document=submitted(
            certificate_number=EXPIRED_ID,
            institution_name="A Completely Different College",
            programmes=["Bachelor of Technology, Civil Engineering"],
            grade="B++",
            valid_from="2016-07-01",
            valid_to="2019-06-30",
        ),
    )

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    assert "field_mismatch" in result.status_reasons
    assert "expired_per_issuer" not in result.status_reasons


# --------------------------------------------------------------------------
# Transport behaviour
# --------------------------------------------------------------------------


def test_a_429_then_success_retries_on_the_exact_schedule(registry, sleeps, sleeper):
    """Honours Retry-After, and the wait is asserted exactly, not timed."""
    faults = FaultInjection(
        force_status=429, fail_first_n=1, retry_after="3"
    )
    client = TestClient(create_app(registry=registry, faults=faults))

    result = run(client, sleeper)

    assert result.status is VerificationStatus.VERIFIED
    assert sleeps == [3.0]

    attempts = result.audit.requests[0]["attempts"]
    assert [a["http_status"] for a in attempts] == [429, 200]
    assert attempts[0]["waited_before_next"] == 3.0


def test_an_absurd_retry_after_is_capped(registry, sleeps, sleeper):
    faults = FaultInjection(force_status=429, fail_first_n=1, retry_after="9000")
    client = TestClient(create_app(registry=registry, faults=faults))

    run(client, sleeper)

    assert sleeps == [10.0]


def test_a_429_without_retry_after_uses_the_configured_backoff(
    registry, sleeps, sleeper
):
    faults = FaultInjection(force_status=429, fail_first_n=2)
    client = TestClient(create_app(registry=registry, faults=faults))

    result = run(client, sleeper)

    assert result.status is VerificationStatus.VERIFIED
    # backoff_seconds 0.5, multiplier 2.0
    assert sleeps == [0.5, 1.0]


def test_exhausted_429_retries_are_source_unavailable(registry, sleeps, sleeper):
    faults = FaultInjection(force_status=429)
    client = TestClient(create_app(registry=registry, faults=faults))

    result = run(client, sleeper)

    assert result.status is VerificationStatus.SOURCE_UNAVAILABLE
    # 1 initial attempt + 2 retries
    assert len(sleeps) == 2
    finding = next(
        f for f in result.findings if f.type is FindingType.SOURCE_UNAVAILABLE
    )
    assert finding.strength == "weak"


def test_a_500_is_retried_then_reported_unavailable(registry, sleeps, sleeper):
    faults = FaultInjection(force_status=500)
    client = TestClient(create_app(registry=registry, faults=faults))

    result = run(client, sleeper)

    assert result.status is VerificationStatus.SOURCE_UNAVAILABLE
    assert len(result.audit.requests[0]["attempts"]) == 3


def test_a_404_is_not_retried(registry, sleeps, sleeper):
    """A not-found is an answer; repeating the request cannot change it."""
    client = TestClient(create_app(registry=registry))

    result = run(
        client, sleeper, document=submitted(certificate_number="NBTA/DEMO/2024/99999")
    )

    assert sleeps == []
    assert len(result.audit.requests[0]["attempts"]) == 1


def test_a_malformed_body_is_not_retried_and_routes_to_manual_review(
    registry, sleeps, sleeper
):
    faults = FaultInjection(malformed_body=True)
    client = TestClient(create_app(registry=registry, faults=faults))

    result = run(client, sleeper)

    assert result.status is VerificationStatus.MANUAL_REVIEW_REQUIRED
    assert result.lookups[0].outcome is LookupOutcome.MALFORMED
    assert sleeps == []
    assert FindingType.MALFORMED_RESPONSE.value in finding_types(result)


def test_a_timeout_is_source_unavailable(registry, sleeps, sleeper, issuer_config):
    """Raised from the transport, which is what a real client does on timeout."""

    class TimingOutTransport:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            raise TimeoutError("request timed out after 5.0s")

    transport = TimingOutTransport()
    result = verify_certificate(
        submitted(),
        transport=transport,
        sleep=sleeper,
        config=issuer_config,
        as_of=AS_OF,
    )

    assert result.status is VerificationStatus.SOURCE_UNAVAILABLE
    assert transport.calls == 3
    assert sleeps == [0.5, 1.0]
    attempts = result.audit.requests[0]["attempts"]
    assert all(a["http_status"] is None for a in attempts)
    assert "TimeoutError" in attempts[0]["error"]


def test_retry_count_is_configurable(registry, sleeps, sleeper, issuer_config):
    config = copy.deepcopy(issuer_config)
    config["request"]["max_retries"] = 0
    faults = FaultInjection(force_status=500)
    client = TestClient(create_app(registry=registry, faults=faults))

    result = verify_certificate(
        submitted(), transport=client, sleep=sleeper, config=config, as_of=AS_OF
    )

    assert result.status is VerificationStatus.SOURCE_UNAVAILABLE
    assert sleeps == []
    assert len(result.audit.requests[0]["attempts"]) == 1


# --------------------------------------------------------------------------
# Two ids: the document number and a differing QR id
# --------------------------------------------------------------------------


def test_a_null_document_number_falls_back_to_the_qr_id_at_lower_trust(
    client, sleeper
):
    result = run(
        client,
        sleeper,
        document=submitted(certificate_number=None),
        qr_certificate_id=ANCHOR_CERTIFICATE_ID,
    )

    assert len(result.lookups) == 1
    lookup = result.lookups[0]
    assert lookup.id_origin is IdOrigin.QR_CODE
    assert lookup.trust == "lower"
    assert FindingType.LOWER_TRUST_LOOKUP.value in finding_types(result)


def test_a_differing_qr_id_is_looked_up_separately(client, sleeper):
    result = run(client, sleeper, qr_certificate_id="NBTA/DEMO/2024/99999")

    assert len(result.lookups) == 2
    assert result.lookups[0].id_origin is IdOrigin.DOCUMENT_NUMBER
    assert result.lookups[0].status is VerificationStatus.VERIFIED
    assert result.lookups[1].id_origin is IdOrigin.QR_CODE
    assert result.lookups[1].outcome is LookupOutcome.NOT_FOUND

    assert FindingType.QR_ID_DIFFERS.value in finding_types(result)
    # The document-level status is the most severe of the two.
    assert result.status is VerificationStatus.MANUAL_REVIEW_REQUIRED


def test_a_qr_id_belonging_to_another_institution_is_a_strong_finding(
    client, sleeper
):
    """A real record, internally consistent, just not this document's."""
    result = run(client, sleeper, qr_certificate_id=OTHER_INSTITUTION_ID)

    assert len(result.lookups) == 2
    finding = next(
        f for f in result.findings if f.type is FindingType.QR_ID_OTHER_INSTITUTION
    )
    assert finding.strength == "strong"
    assert finding.evidence["document_value"] == ANCHOR_INSTITUTION
    assert finding.evidence["issuer_value"] == "Gomati Valley College of Engineering"
    assert result.status is VerificationStatus.MISMATCH_DETECTED


def test_an_identical_qr_id_is_only_looked_up_once(client, sleeper):
    result = run(client, sleeper, qr_certificate_id=ANCHOR_CERTIFICATE_ID)

    assert len(result.lookups) == 1
    assert FindingType.QR_ID_DIFFERS.value not in finding_types(result)


def test_no_usable_id_at_all_routes_to_manual_review(client, sleeper):
    result = run(client, sleeper, document=submitted(certificate_number=None))

    assert result.status is VerificationStatus.MANUAL_REVIEW_REQUIRED
    assert result.lookups == []
    assert FindingType.NO_CERTIFICATE_ID.value in finding_types(result)
    assert result.audit.requests == []


def test_qr_id_is_not_taken_when_two_distinct_payloads_exist():
    """Two QRs is already a step 3 finding; choosing one here would hide it."""
    scan = {
        "detections": [
            {"certificate_id": "NBTA/DEMO/2024/00417"},
            {"certificate_id": "NBTA/DEMO/2024/00418"},
        ]
    }

    assert qr_id_from_scan(scan) is None
    assert qr_id_from_scan({"detections": [{"certificate_id": "X"}]}) == "X"
    assert qr_id_from_scan({"detections": []}) is None


# --------------------------------------------------------------------------
# The destination rule
# --------------------------------------------------------------------------


def test_the_qr_host_is_never_contacted(registry, sleeper, issuer_config):
    """The request destination comes from config and from nowhere else.

    Honouring a URL off a submitted document would let whoever prepared it
    choose which "issuer" vouches for it. The client takes its base URL at
    construction and exposes no per-request override, so this asserts the
    property that makes that structural.
    """
    requested: list[str] = []

    class RecordingTransport:
        def get(self, url, **kwargs):
            requested.append(url)
            raise TimeoutError("not a real endpoint")

    hostile = "https://verify.nbta-demo.test.evil.test"
    result = verify_certificate(
        submitted(),
        qr_certificate_id=ANCHOR_CERTIFICATE_ID,
        transport=RecordingTransport(),
        sleep=sleeper,
        config=issuer_config,
        as_of=AS_OF,
    )

    assert requested, "no request was attempted, so the test proves nothing"
    configured = str(issuer_config["issuer"]["base_url"])
    for url in requested:
        assert url.startswith(configured), url
        assert "evil.test" not in url
        assert hostile not in url
    assert result.audit.issuer_base_url == configured


def test_the_client_offers_no_per_request_destination_override(issuer_config):
    """There is no argument by which a caller could redirect a lookup."""
    import inspect

    client = IssuerClient(issuer_config, transport=object())
    parameters = set(inspect.signature(client.lookup).parameters)

    assert parameters == {"certificate_id"}


def test_a_missing_base_url_is_refused(issuer_config):
    config = copy.deepcopy(issuer_config)
    config["issuer"]["base_url"] = ""

    with pytest.raises(ValueError, match="base_url"):
        IssuerClient(config)


# --------------------------------------------------------------------------
# Status mapping, audit, serialisation
# --------------------------------------------------------------------------


def test_the_status_mapping_covers_every_precedence_key(issuer_config):
    table = issuer_config["status_mapping"]

    for key in PRECEDENCE:
        assert key in table, f"status_mapping has no entry for {key}"
        # And every configured value is in the fixed taxonomy.
        parse_status(table[key])


def test_the_status_mapping_is_configurable(client, sleeper, issuer_config):
    """An organisation can route a suspended certificate to manual review."""
    config = copy.deepcopy(issuer_config)
    config["status_mapping"]["issuer_status_suspended"] = "manual_review_required"

    result = verify_certificate(
        submitted(
            certificate_number=SUSPENDED_ID,
            institution_name="Nilgiri College of Engineering and Technology",
            programmes=["Bachelor of Technology, Civil Engineering"],
            grade="B++",
            valid_from="2023-07-01",
            valid_to="2028-06-30",
        ),
        transport=client,
        sleep=sleeper,
        config=config,
        as_of=AS_OF,
    )

    assert result.status is VerificationStatus.MANUAL_REVIEW_REQUIRED


def test_a_status_outside_the_taxonomy_is_rejected(client, sleeper, issuer_config):
    config = copy.deepcopy(issuer_config)
    config["status_mapping"]["all_fields_match"] = "probably_fine"

    with pytest.raises(ValueError, match="not a verification status"):
        verify_certificate(
            submitted(), transport=client, sleep=sleeper, config=config, as_of=AS_OF
        )


def test_the_audit_block_records_the_request_and_response(
    client, sleeper, registry
):
    result = run(
        client, sleeper, registry_version=registry.version, registry_sha256=registry.sha256
    )
    audit = result.audit

    assert audit.config_version == load_config("issuer_verification")["config_version"]
    assert audit.issuer_base_url
    assert audit.registry_version == "issuer-registry-v1"
    assert audit.registry_sha256 and len(audit.registry_sha256) == 64
    assert audit.as_of == AS_OF
    assert audit.timestamp.endswith("+00:00")

    request = audit.requests[0]
    assert request["http_status"] == 200
    assert request["latency_ms"] >= 0
    assert ANCHOR_CERTIFICATE_ID in request["raw_response"]


def test_the_raw_response_can_be_withheld(client, sleeper, issuer_config):
    config = copy.deepcopy(issuer_config)
    config["output"]["include_raw_response"] = False

    result = verify_certificate(
        submitted(), transport=client, sleep=sleeper, config=config, as_of=AS_OF
    )

    assert "raw_response" not in result.audit.requests[0]


def test_the_result_serialises_to_json(client, sleeper):
    result = run(client, sleeper, qr_certificate_id=OTHER_INSTITUTION_ID)

    round_tripped = json.loads(json.dumps(result.to_dict()))

    assert round_tripped["status"] == "mismatch_detected"
    assert len(round_tripped["lookups"]) == 2
    assert round_tripped["audit"]["config_version"]
    assert round_tripped["findings"]


def test_no_finding_mentions_forgery(client, sleeper):
    """CLAUDE.md language rule, asserted rather than trusted."""
    banned = ("fake", "forged", "forgery", "fraudulent", "deepfake", "counterfeit")
    cases = [
        {},
        {"document": submitted(certificate_number="NBTA/DEMO/2024/99999")},
        {"document": submitted(institution_name="Gomati Valley College of Engineering")},
        {"document": submitted(certificate_number=REVOKED_ID)},
        {"document": submitted(certificate_number=None)},
        {"qr_certificate_id": OTHER_INSTITUTION_ID},
    ]

    for case in cases:
        result = run(client, sleeper, **case)
        for finding in result.findings:
            lowered = finding.reason.lower()
            for word in banned:
                assert word not in lowered, finding.reason


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_extraction(tmp_path: Path, **overrides) -> Path:
    fields = {
        "certificate_number": {"value": ANCHOR_CERTIFICATE_ID, "validated": True},
        "institution_name": {"value": ANCHOR_INSTITUTION, "validated": True},
        "issuing_body": {"value": ISSUING_BODY, "validated": True},
        "programme": {"value": [ANCHOR_PROGRAMME], "validated": True},
        "accreditation_status_or_grade": {"value": "A+", "validated": True},
        "valid_from": {"value_iso": ANCHOR_VALID_FROM, "validated": True},
        "valid_to": {"value_iso": ANCHOR_VALID_TO, "validated": True},
    }
    fields.update(overrides)
    path = tmp_path / "step2.json"
    path.write_text(json.dumps({"fields": fields}), encoding="utf-8")
    return path


def test_cli_verifies_in_process(tmp_path, capsys):
    from app.issuer.cli import main

    path = _write_extraction(tmp_path)
    exit_code = main([str(path), "--in-process", "--as-of", AS_OF])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "verified"
    assert payload["audit"]["registry_version"] == "issuer-registry-v1"


def test_cli_reads_a_qr_id_from_a_step_3_result(tmp_path, capsys):
    from app.issuer.cli import main

    extraction = _write_extraction(tmp_path)
    scan = tmp_path / "step3.json"
    scan.write_text(
        json.dumps({"detections": [{"certificate_id": OTHER_INSTITUTION_ID}]}),
        encoding="utf-8",
    )

    main([str(extraction), "--qr", str(scan), "--in-process", "--as-of", AS_OF])
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "mismatch_detected"
    assert len(payload["lookups"]) == 2


def test_cli_reports_a_missing_file(capsys):
    from app.issuer.cli import main

    exit_code = main(["nope.json", "--in-process"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error"] == "file_not_found"


def test_document_reasons_lead_with_the_deciding_one(client, sleeper):
    """In lookup order a clean primary lookup would read first and mislead."""
    result = run(client, sleeper, qr_certificate_id=OTHER_INSTITUTION_ID)

    assert result.status is VerificationStatus.MISMATCH_DETECTED
    assert result.status_reasons[0] == "field_mismatch"
    assert "all_fields_match" in result.status_reasons
