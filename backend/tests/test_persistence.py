"""Tests for step 5: persistence, the audit chain and the pipeline.

In-memory SQLite, the stub LLM provider, the mock issuer in-process, and no
sleeps. Nothing here touches the network or calls a live model.

Tests that run the pipeline carry the `qr` marker, because the pipeline
decodes real images with OpenCV and zxing-cpp and that is what makes them
slow. The database, audit-chain, storage and role tests have no such cost and
stay in the fast suite.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.core.config import load_config
from app.db import audit_log, repository
from app.db.engine import (
    Database,
    audit_triggers_installed,
    foreign_keys_enforced,
)
from app.db.models import (
    Application,
    AuditLog,
    CheckRun,
    Document,
    FindingRow,
    RawLLMResponse,
)
from app.db.roles import UnknownRoleError
from app.db.storage import store_file, verify_stored_file
from app.llm.stub_provider import StubProvider
from app.mock_issuer.registry import load_registry
from app.mock_issuer.seed import (
    ANCHOR_CERTIFICATE_ID,
    ANCHOR_INSTITUTION,
    ANCHOR_PROGRAMME,
    ANCHOR_VALID_FROM,
    ANCHOR_VALID_TO,
    ISSUING_BODY,
)
from app.mock_issuer.service import FaultInjection, create_app
from app.pipeline.runner import Pipeline, ensure_application
from tests.make_qr_fixtures import build_all as build_qr_fixtures
from tests.make_qr_fixtures import verify_url

AS_OF = "2026-01-15"
ACTOR = "officer.sharma"
ROLE = "officer"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def persistence_config(tmp_path) -> dict:
    """Real config, redirected at a temporary store and an in-memory database."""
    config = copy.deepcopy(load_config("persistence"))
    config["database"]["url"] = "sqlite:///:memory:"
    config["storage"]["root"] = str(tmp_path / "store")
    return config


@pytest.fixture
def database(persistence_config) -> Database:
    db = Database(persistence_config)
    yield db
    db.dispose()


@pytest.fixture(scope="session")
def qr_samples() -> dict[str, Path]:
    return build_qr_fixtures()


def _certificate_payload(**overrides) -> dict:
    """What the model returns for the anchor certificate."""
    payload = {
        "institution_name": {
            "value": ANCHOR_INSTITUTION,
            "source_snippet": f"Institution: {ANCHOR_INSTITUTION}",
            "page": 1,
            "confidence": "high",
        },
        "certificate_number": {
            "value": ANCHOR_CERTIFICATE_ID,
            "source_snippet": f"Certificate Number: {ANCHOR_CERTIFICATE_ID}",
            "page": 1,
            "confidence": "high",
        },
        "issuing_body": {
            "value": ISSUING_BODY,
            "source_snippet": f"Issuing Body: {ISSUING_BODY}",
            "page": 1,
            "confidence": "high",
        },
        "programme": {
            "value": ["Bachelor of Technology", "Computer Science and Engineering"],
            "source_snippet": f"Programme: {ANCHOR_PROGRAMME}",
            "page": 1,
            "confidence": "high",
        },
        "accreditation_status_or_grade": {
            "value": None,
            "source_snippet": None,
            "page": None,
            "confidence": None,
        },
        "issue_date": {
            "value": None,
            "source_snippet": None,
            "page": None,
            "confidence": None,
        },
        "valid_from": {
            "value": "01 July 2024",
            "source_snippet": "Valid From: 01 July 2024",
            "page": 1,
            "confidence": "high",
        },
        "valid_to": {
            "value": "30 June 2027",
            "source_snippet": "Valid Until: 30 June 2027",
            "page": 1,
            "confidence": "high",
        },
        "signatory_name": {
            "value": None,
            "source_snippet": None,
            "page": None,
            "confidence": None,
        },
    }
    payload.update(overrides)
    return payload


def stub_provider(**overrides) -> StubProvider:
    return StubProvider(responses=[json.dumps(_certificate_payload(**overrides))])


@pytest.fixture
def pipeline(database, persistence_config):
    """A pipeline with every external collaborator replaced in-process."""

    def build(
        provider=None,
        faults: FaultInjection | None = None,
        configs: dict | None = None,
        transport=None,
    ):
        registry = load_registry()
        if transport is None:
            transport = TestClient(
                create_app(registry=registry, faults=faults)
            )
        merged = {"persistence": persistence_config}
        merged.update(configs or {})
        return Pipeline(
            database=database,
            llm_provider=provider if provider is not None else stub_provider(),
            issuer_transport=transport,
            # Captured, never performed, so a retry test costs no wall time.
            issuer_sleep=lambda seconds: None,
            configs=merged,
            registry_hash=registry.sha256,
            as_of=AS_OF,
        )

    return build


@pytest.fixture
def application(database, persistence_config) -> int:
    return ensure_application(
        database,
        institution_name=ANCHOR_INSTITUTION,
        category="extension_of_approval",
        actor=ACTOR,
        role=ROLE,
        config=persistence_config,
    )


def run_document(pipeline_factory, application_id, path, **kwargs):
    pipe = pipeline_factory(**kwargs)
    return pipe, pipe.process_document(
        application_id=application_id,
        file_path=path,
        document_type="accreditation_certificate",
        actor=ACTOR,
        role=ROLE,
    )


def stage(summary, name):
    for entry in summary.stages:
        if entry.stage == name:
            return entry
    raise AssertionError(f"no stage {name}; got {[s.stage for s in summary.stages]}")


# --------------------------------------------------------------------------
# Schema and database guarantees
# --------------------------------------------------------------------------


def test_the_schema_is_created_with_a_version(database):
    assert database.schema_version() == "schema-v3"
    tables = database.table_names()

    for expected in (
        "institutions",
        "applications",
        "documents",
        "check_runs",
        "stage_results",
        "page_extractions",
        "extracted_fields",
        "raw_llm_responses",
        "qr_detections",
        "verification_lookups",
        "findings",
        "review_actions",
        "audit_log",
        "schema_version",
    ):
        assert expected in tables


def test_foreign_keys_are_enforced(database):
    """SQLite ignores them unless asked, per connection."""
    assert foreign_keys_enforced(database.engine) is True

    with database.session() as session:
        session.add(Application(institution_id=9999, category="eoa"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_unknown_role_is_rejected(database, persistence_config):
    with pytest.raises(UnknownRoleError, match="not a known role"):
        ensure_application(
            database,
            institution_name="Somewhere",
            category="eoa",
            actor=ACTOR,
            role="supreme_overlord",
            config=persistence_config,
        )


def test_no_sqlite_only_column_types_are_used():
    """Keeps Postgres a drop-in rather than a migration project."""
    from app.db.models import Base

    # SQLAlchemy renders every one of these natively on both backends.
    allowed = {"Integer", "String", "Text", "Float", "Boolean", "DateTime", "JSON"}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            name = type(column.type).__name__
            assert name in allowed, f"{table.name}.{column.name} is {name}"


# --------------------------------------------------------------------------
# File storage
# --------------------------------------------------------------------------


def test_identical_files_are_stored_once(persistence_config, tmp_path, qr_samples):
    source = Path(qr_samples["valid_pdf"])
    copy_path = tmp_path / "resubmitted.pdf"
    copy_path.write_bytes(source.read_bytes())

    first = store_file(source, persistence_config)
    second = store_file(copy_path, persistence_config)

    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.newly_stored is True
    assert second.newly_stored is False

    root = Path(persistence_config["storage"]["root"])
    stored = [p for p in root.rglob("*") if p.is_file()]
    assert len(stored) == 1


def test_a_stored_file_verifies_against_its_hash(persistence_config, qr_samples):
    stored = store_file(Path(qr_samples["valid_pdf"]), persistence_config)

    assert verify_stored_file(stored.path, stored.sha256) is True
    assert verify_stored_file(stored.path, "0" * 64) is False


@pytest.mark.qr
def test_the_same_file_submitted_twice_is_visible_as_a_duplicate(
    pipeline, application, qr_samples, database
):
    _, first = run_document(pipeline, application, qr_samples["valid_pdf"])
    _, second = run_document(pipeline, application, qr_samples["valid_pdf"])

    assert first.duplicate_of_document_ids == []
    assert second.duplicate_of_document_ids == [first.document_id]
    assert first.file_sha256 == second.file_sha256


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_a_clean_document_round_trips(pipeline, application, qr_samples, database):
    _, summary = run_document(pipeline, application, qr_samples["valid_pdf"])

    assert summary.status == "completed"
    assert summary.verification_status == "verified"
    assert summary.finding_count == 0

    for name in ("extraction", "certificate_fields", "qr_checks", "issuer_verification"):
        assert stage(summary, name).status == "ok"

    with database.session() as session:
        report = repository.get_application_report(session, application)

    run = report["documents"][0]["runs"][0]
    assert run["status"] == "completed"
    assert len(run["pages"]) == 1

    fields = {entry["name"]: entry for entry in run["extracted_fields"]}
    assert fields["certificate_number"]["value"] == ANCHOR_CERTIFICATE_ID
    assert fields["certificate_number"]["validated"] is True
    assert fields["valid_to"]["value_iso"] == ANCHOR_VALID_TO
    # A field absent from the certificate is stored as null, not omitted.
    assert fields["accreditation_status_or_grade"]["value"] is None

    assert len(run["qr_detections"]) == 1
    assert run["qr_detections"][0]["certificate_id"] == ANCHOR_CERTIFICATE_ID
    assert run["qr_detections"][0]["parsed_host"] == "verify.nbta-demo.test"

    assert len(run["verification_lookups"]) == 1
    lookup = run["verification_lookups"][0]
    assert lookup["status"] == "verified"
    assert lookup["http_status"] == 200
    assert lookup["issuer_record"]["certificate_id"] == ANCHOR_CERTIFICATE_ID

    assert run["findings"] == []


@pytest.mark.qr
def test_the_run_records_the_config_versions_it_was_judged_under(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)

        assert run.extraction_config_version
        assert run.certificate_config_version
        assert run.qr_config_version
        assert run.issuer_config_version
        assert run.persistence_config_version
        assert set(run.config_hashes) == {
            "extraction",
            "certificate_extraction",
            "qr_checks",
            "issuer_verification",
            # Added in step 6.
            "signature_checks",
            "duplicate_checks",
            "persistence",
            # Added with the faculty and photo detectors.
            "faculty_checks",
            "photo_checks",
        }
        assert run.signature_config_version
        assert run.duplicate_config_version
        assert run.provider == "stub"
        assert run.prompt_version == "certificate_extraction.v1"
        assert run.registry_hash and len(run.registry_hash) == 64


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_a_flagged_document_stores_findings_with_evidence(
    pipeline, application, qr_samples, database
):
    """The QR carries a certificate id one digit from the printed number."""
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    assert summary.finding_count >= 1
    assert "qr_certificate_id_mismatch" in summary.finding_types

    with database.session() as session:
        findings = repository.findings_for_run(session, summary.run_id)
        mismatch = next(
            f for f in findings if f.type == "qr_certificate_id_mismatch"
        )

        assert mismatch.detector == "certificate_authenticity"
        assert mismatch.strength == "strong"
        assert mismatch.review_status == "open"
        assert mismatch.evidence["qr_value"] == "NBTA/DEMO/2024/00418"
        assert mismatch.evidence["document_value"] == ANCHOR_CERTIFICATE_ID
        assert "mismatch detected" in mismatch.reason.lower()


@pytest.mark.qr
def test_findings_from_both_detectors_land_in_one_table(
    pipeline, application, qr_samples, database
):
    """A reviewer works one queue, so step 3 and step 4 share a table."""
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        findings = repository.findings_for_run(session, summary.run_id)

    types = {finding.type for finding in findings}
    assert any(t.startswith("qr_") for t in types)
    assert any(t.startswith("issuer_") for t in types)
    assert {finding.detector for finding in findings} == {"certificate_authenticity"}


# --------------------------------------------------------------------------
# Re-runs never overwrite
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_re_running_creates_a_second_run_and_leaves_the_first_untouched(
    pipeline, application, qr_samples, database
):
    pipe, first = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        before = repository._run_dict(session, session.get(CheckRun, first.run_id))

    second = pipe.process_document(
        application_id=application,
        file_path=qr_samples["valid_pdf"],
        document_type="accreditation_certificate",
        actor="senior.iyer",
        role="senior_reviewer",
    )

    assert second.run_id != first.run_id

    with database.session() as session:
        after = repository._run_dict(session, session.get(CheckRun, first.run_id))
        runs = repository.runs_for_document(session, first.document_id)

    assert after == before, "the earlier run was modified"
    # A second submission of the same bytes is a second document row sharing
    # the stored file, so each has its own run.
    assert len(runs) == 1
    with database.session() as session:
        assert len(repository.runs_for_document(session, second.document_id)) == 1


@pytest.mark.qr
def test_a_second_run_on_the_same_document_row_keeps_both(
    pipeline, application, qr_samples, database
):
    pipe, first = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        with session.begin():
            run = repository.start_check_run(
                session,
                document_id=first.document_id,
                actor="admin.rao",
                role="admin",
                config_versions={},
                config=pipe.persistence,
            )
        second_id = run.id

    with database.session() as session:
        runs = repository.runs_for_document(session, first.document_id)

    assert [r.id for r in runs] == [first.run_id, second_id]
    assert runs[0].actor == ACTOR
    assert runs[1].actor == "admin.rao"


# --------------------------------------------------------------------------
# Degradation: recorded, never fatal
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_a_corrupt_pdf_is_recorded_and_does_not_crash_the_run(
    pipeline, application, tmp_path, database
):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nthis is not a pdf body")

    _, summary = run_document(pipeline, application, broken)

    assert summary.status == "completed_with_failures"
    assert stage(summary, "extraction").status == "failed"
    # With no text there is nothing for field extraction to read.
    assert stage(summary, "certificate_fields").status == "skipped"
    assert "nothing for field extraction to read" in (
        stage(summary, "certificate_fields").reason or ""
    )

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)
        stages = {s.stage: s for s in run.stage_results}

    # Every stage has a row, including the ones that could not run: a missing
    # row would be indistinguishable from a stage that passed.
    assert set(stages) == {
        "extraction",
        "certificate_fields",
        "qr_checks",
        "issuer_verification",
        "signature_check",
        "duplicate_check",
    }
    assert stages["issuer_verification"].status == "skipped"
    # Step 6's stages work on the file and the corpus, so a corrupt PDF leaves
    # them something to say rather than nothing: the signature check reports
    # that it found no signature structures, and the duplicate check still has
    # the file hash to compare even though the page will not render.
    assert stages["signature_check"].status in ("ok", "degraded")
    assert stages["duplicate_check"].status in ("ok", "degraded")


@pytest.mark.qr
def test_extraction_failure_still_lets_qr_run_and_feeds_the_issuer_lookup(
    pipeline, application, qr_samples, database
):
    """A QR-only image has no text layer, so stage 2 has nothing to read.

    Stage 3 works on the file itself, so it still decodes, and stage 4 uses
    the QR id at lower trust. Losing the earlier stages would discard exactly
    the evidence that explains the rest.
    """
    _, summary = run_document(pipeline, application, qr_samples["valid_jpg"])

    assert stage(summary, "qr_checks").status == "ok"
    assert stage(summary, "issuer_verification").status == "ok"

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)
        lookups = run.verification_lookups

    assert len(lookups) == 1
    assert lookups[0].id_origin == "qr_certificate_id"
    assert lookups[0].trust == "lower"
    assert lookups[0].status == "verified"


@pytest.mark.qr
def test_an_unreachable_issuer_is_stored_as_source_unavailable(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(
        pipeline,
        application,
        qr_samples["valid_pdf"],
        faults=FaultInjection(force_status=500),
    )

    assert summary.verification_status == "source_unavailable"
    assert stage(summary, "issuer_verification").status == "degraded"
    assert summary.status == "completed_with_failures"

    # The earlier stages are intact.
    assert stage(summary, "extraction").status == "ok"
    assert stage(summary, "certificate_fields").status == "ok"

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)

        assert len(run.extracted_fields) == 9
        assert len(run.qr_detections) == 1
        lookup = run.verification_lookups[0]
        assert lookup.status == "source_unavailable"
        # The retry history survives the failure, which is when it matters.
        assert lookup.attempt_count == 3
        assert lookup.error


@pytest.mark.qr
def test_a_provider_failure_is_degraded_not_fatal(
    pipeline, application, qr_samples, database
):
    """An exhausted stub provider stands in for a dead API key."""
    _, summary = run_document(
        pipeline, application, qr_samples["valid_pdf"], provider=StubProvider(responses=[])
    )

    assert stage(summary, "certificate_fields").status in ("degraded", "failed")
    # The QR still decodes, and its id still drives the lookup.
    assert stage(summary, "qr_checks").status == "ok"
    assert stage(summary, "issuer_verification").status == "ok"
    assert summary.verification_status == "verified"


@pytest.mark.qr
def test_a_failing_stage_does_not_roll_back_earlier_stages(
    pipeline, application, qr_samples, database
):
    """Each stage commits in its own transaction."""
    _, summary = run_document(
        pipeline,
        application,
        qr_samples["valid_pdf"],
        faults=FaultInjection(force_status=500),
    )

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)

        assert run.page_extractions, "stage 1 was rolled back"
        assert run.extracted_fields, "stage 2 was rolled back"
        assert run.qr_detections, "stage 3 was rolled back"


# --------------------------------------------------------------------------
# Raw LLM responses
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_raw_llm_responses_are_stored_separately_from_validated_fields(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        run = session.get(CheckRun, summary.run_id)

        assert len(run.raw_llm_responses) == 1
        assert ANCHOR_CERTIFICATE_ID in run.raw_llm_responses[0].response_text
        assert run.raw_llm_responses[0].attempt_number == 1


@pytest.mark.qr
def test_raw_responses_can_be_switched_off(
    database, persistence_config, pipeline, application, qr_samples
):
    persistence_config["runs"]["store_raw_llm_responses"] = False

    _, summary = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        rows = session.query(RawLLMResponse).all()
        run = session.get(CheckRun, summary.run_id)
        field_count = len(run.extracted_fields)

    assert rows == []
    # The validated fields are unaffected.
    assert field_count == 9


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_the_audit_log_records_each_stage_and_verifies(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        entries = audit_log.all_entries(session)
        verification = audit_log.verify_chain(session)

    actions = [entry.action for entry in entries]
    assert "institution_created" in actions
    assert "application_created" in actions
    assert "document_stored" in actions
    assert "check_run_started" in actions
    # Six stages since step 6 added the signature and duplicate checks.
    assert actions.count("stage_completed") == 6
    assert "check_run_finished" in actions

    assert verification.ok is True
    assert verification.entries_checked == len(entries)
    assert verification.first_broken_id is None


@pytest.mark.qr
def test_the_chain_links_every_entry_to_the_previous_one(
    pipeline, application, qr_samples, database
):
    run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        entries = audit_log.all_entries(session)

    assert entries[0].previous_hash is None
    for earlier, later in zip(entries, entries[1:]):
        assert later.previous_hash == earlier.entry_hash


@pytest.mark.qr
def test_audit_details_carry_no_document_text_or_raw_model_output(
    pipeline, application, qr_samples, database
):
    """An audit trail that quoted the documents would be a second copy of them.

    Identifiers are expected and wanted: the institution name an operator
    typed, filenames, hashes, ids, statuses. What must never appear is content
    read *out of* the document — page text, the model's source snippets, or a
    raw model response — because the audit log is the one table designed to be
    kept and exported.
    """
    run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        entries = audit_log.all_entries(session)

    blob = json.dumps([entry.details for entry in entries])

    # Body text from the certificate itself.
    assert "Certificate of Accreditation" not in blob
    assert "NATIONAL BOARD OF TECHNICAL ACCREDITATION" not in blob
    assert "Valid Until" not in blob
    # The model's own output and its quoted evidence.
    assert "source_snippet" not in blob
    assert "model_value" not in blob
    assert "Issuing Body:" not in blob


def test_update_on_the_audit_log_is_rejected(database):
    assert "audit_log_no_update" in audit_triggers_installed(database.engine)

    with database.session() as session:
        with session.begin():
            audit_log.append(
                session,
                actor=ACTOR,
                role=ROLE,
                action="test_entry",
                entity_type="test",
                entity_id=1,
            )

    # SQLite's RAISE(ABORT) surfaces as an IntegrityError.
    with pytest.raises(DatabaseError, match="append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE audit_log SET actor='someone_else'"))


def test_delete_on_the_audit_log_is_rejected(database):
    assert "audit_log_no_delete" in audit_triggers_installed(database.engine)

    with database.session() as session:
        with session.begin():
            audit_log.append(
                session,
                actor=ACTOR,
                role=ROLE,
                action="test_entry",
                entity_type="test",
                entity_id=1,
            )

    with pytest.raises(DatabaseError, match="append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_log"))


def test_the_audit_module_exposes_no_update_or_delete(database):
    """The second lock: no code path to call, even if the triggers were gone."""
    names = {name for name in dir(audit_log) if not name.startswith("_")}

    for forbidden in ("update", "delete", "remove", "edit", "modify"):
        assert not any(forbidden in name for name in names), names


@pytest.mark.qr
def test_tampering_with_a_row_directly_is_detected(
    pipeline, application, qr_samples, database
):
    """Someone with file access can drop the triggers; they cannot fix the chain."""
    run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        before = audit_log.verify_chain(session)
        assert before.ok is True
        entries = audit_log.all_entries(session)
        target_id = entries[2].id

    # Drop the triggers, exactly as an attacker with direct access would.
    with database.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER audit_log_no_update"))
        connection.execute(text("DROP TRIGGER audit_log_no_delete"))
        connection.execute(
            text("UPDATE audit_log SET actor='ghost' WHERE id = :id"),
            {"id": target_id},
        )

    with database.session() as session:
        after = audit_log.verify_chain(session)

    assert after.ok is False
    assert after.first_broken_id == target_id
    assert "altered after it was written" in (after.problem or "")


@pytest.mark.qr
def test_removing_an_entry_is_detected_as_a_break_in_the_links(
    pipeline, application, qr_samples, database
):
    run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        entries = audit_log.all_entries(session)
        victim_id = entries[2].id
        next_id = entries[3].id

    with database.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER audit_log_no_update"))
        connection.execute(text("DROP TRIGGER audit_log_no_delete"))
        connection.execute(
            text("DELETE FROM audit_log WHERE id = :id"), {"id": victim_id}
        )

    with database.session() as session:
        after = audit_log.verify_chain(session)

    assert after.ok is False
    assert after.first_broken_id == next_id
    assert "removed, reordered or inserted" in (after.problem or "")


@pytest.mark.qr
def test_a_stage_failure_is_written_to_the_audit_log(
    pipeline, application, tmp_path, database
):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot a pdf")

    run_document(pipeline, application, broken)

    with database.session() as session:
        entries = audit_log.all_entries(session)

    stage_entries = [e for e in entries if e.action == "stage_completed"]
    statuses = {e.details["stage"]: e.details["status"] for e in stage_entries}

    assert statuses["extraction"] == "failed"
    assert statuses["certificate_fields"] == "skipped"
    assert any(e.details.get("reason") for e in stage_entries)


# --------------------------------------------------------------------------
# Review actions
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_a_finding_starts_open_and_an_action_moves_it(
    pipeline, application, qr_samples, database, persistence_config
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        finding = repository.findings_for_run(session, summary.run_id)[0]
        assert finding.review_status == "open"
        finding_id = finding.id

    with database.session() as session:
        with session.begin():
            repository.record_review_action(
                session,
                finding_id=finding_id,
                action="confirm",
                actor="senior.iyer",
                role="senior_reviewer",
                note="checked against the physical certificate",
                config=persistence_config,
            )

    with database.session() as session:
        finding = session.get(FindingRow, finding_id)

        assert finding.review_status == "confirmed"
        assert len(finding.review_actions) == 1
        assert finding.review_actions[0].actor == "senior.iyer"
        assert finding.review_actions[0].role == "senior_reviewer"

        entries = [
            entry
            for entry in audit_log.all_entries(session)
            if entry.action == "review_action"
        ]
        assert len(entries) == 1
        assert entries[0].details["new_status"] == "confirmed"
        assert entries[0].details["previous_status"] == "open"
        # The note's existence is recorded; its text stays in the actions table.
        assert entries[0].details["has_note"] is True
        assert "checked against" not in json.dumps(entries[0].details)

        assert audit_log.verify_chain(session).ok is True


@pytest.mark.qr
def test_a_comment_action_records_without_changing_the_status(
    pipeline, application, qr_samples, database, persistence_config
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        finding_id = repository.findings_for_run(session, summary.run_id)[0].id

    with database.session() as session:
        with session.begin():
            repository.record_review_action(
                session,
                finding_id=finding_id,
                action="comment",
                actor=ACTOR,
                role=ROLE,
                note="awaiting the registrar's reply",
                config=persistence_config,
            )

    with database.session() as session:
        finding = session.get(FindingRow, finding_id)
        review_status = finding.review_status
        action_count = len(finding.review_actions)

    assert review_status == "open"
    assert action_count == 1


@pytest.mark.qr
def test_an_unknown_review_action_is_rejected(
    pipeline, application, qr_samples, database, persistence_config
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        finding_id = repository.findings_for_run(session, summary.run_id)[0].id
        with pytest.raises(ValueError, match="not a known review action"):
            repository.record_review_action(
                session,
                finding_id=finding_id,
                action="approve_application",
                actor=ACTOR,
                role=ROLE,
                config=persistence_config,
            )


@pytest.mark.qr
def test_a_review_action_requires_a_known_role(
    pipeline, application, qr_samples, database, persistence_config
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        finding_id = repository.findings_for_run(session, summary.run_id)[0].id
        with pytest.raises(UnknownRoleError):
            repository.record_review_action(
                session,
                finding_id=finding_id,
                action="confirm",
                actor=ACTOR,
                role="intern",
                config=persistence_config,
            )


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_the_report_returns_everything_for_an_application(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        report = repository.get_application_report(session, application)

    assert report["application"]["id"] == application
    assert report["institution"]["name"] == ANCHOR_INSTITUTION
    assert len(report["documents"]) == 1

    document = report["documents"][0]
    assert document["file_sha256"]
    assert document["uploaded_by"] == ACTOR
    assert document["run_count"] == 1

    run = document["runs"][0]
    for key in (
        "stages",
        "pages",
        "extracted_fields",
        "qr_detections",
        "verification_lookups",
        "findings",
        "config_versions",
    ):
        assert key in run

    assert run["findings"][0]["review_status"] == "open"
    # JSON-serialisable end to end, since this will feed the dashboard.
    json.dumps(report)


@pytest.mark.qr
def test_the_report_shows_only_the_latest_run_unless_asked(
    pipeline, application, qr_samples, database, persistence_config
):
    pipe, first = run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        with session.begin():
            repository.start_check_run(
                session,
                document_id=first.document_id,
                actor="admin.rao",
                role="admin",
                config_versions={},
                config=persistence_config,
            )

    with database.session() as session:
        latest = repository.get_application_report(session, application)
        everything = repository.get_application_report(
            session, application, all_runs=True
        )

    assert len(latest["documents"][0]["runs"]) == 1
    assert latest["documents"][0]["runs"][0]["actor"] == "admin.rao"
    assert len(everything["documents"][0]["runs"]) == 2


@pytest.mark.qr
def test_reading_a_report_with_an_actor_is_audited(
    pipeline, application, qr_samples, database
):
    run_document(pipeline, application, qr_samples["valid_pdf"])

    with database.session() as session:
        repository.get_application_report(
            session, application, actor="senior.iyer", role="senior_reviewer"
        )

    with database.session() as session:
        entries = [
            entry
            for entry in audit_log.all_entries(session)
            if entry.action == "report_read"
        ]
        assert len(entries) == 1
        assert entries[0].actor == "senior.iyer"
        assert audit_log.verify_chain(session).ok is True


def test_an_unknown_application_is_an_error_not_an_empty_report(database):
    with database.session() as session:
        with pytest.raises(ValueError, match="no application with id"):
            repository.get_application_report(session, 9999)


# --------------------------------------------------------------------------
# Deleting an application cascades (and leaves no orphans)
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_deleting_an_application_leaves_no_orphaned_runs_or_findings(
    pipeline, application, qr_samples, database
):
    _, summary = run_document(pipeline, application, qr_samples["altered_id_pdf"])

    with database.session() as session:
        with session.begin():
            session.delete(session.get(Application, application))

    with database.session() as session:
        assert session.get(CheckRun, summary.run_id) is None
        assert session.get(Document, summary.document_id) is None
        assert session.query(FindingRow).count() == 0
        # The audit trail is untouched: it outlives the rows it describes.
        assert session.query(AuditLog).count() > 0
        assert audit_log.verify_chain(session).ok is True
