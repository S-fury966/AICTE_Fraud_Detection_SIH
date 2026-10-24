"""SQLAlchemy models.

Portability rule: **no SQLite-only types.** Every column here is `String`,
`Text`, `Integer`, `Float`, `Boolean`, `DateTime` or the generic `JSON`, all of
which SQLAlchemy renders natively on both SQLite and Postgres. That keeps
Postgres a drop-in rather than a migration project.

Two shapes worth noting before reading further:

- **A re-run never overwrites.** Every run of a document creates a new
  `check_runs` row and its own children. History is the point: a reviewer must
  be able to see that a document was clean under last month's rules and is
  flagged under this month's, which is impossible if a run is mutated in place.
- **Findings are one table for every detector.** The faculty and photo
  detectors are not built yet, but a reviewer works through one queue, and a
  table per detector would mean a UNION for the most common query in the
  system. `detector` says where a finding came from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    """What created this database. Stands in for a migration tool.

    No Alembic yet, so this is how a database made by an older build is
    recognised instead of failing obscurely at query time.
    """

    __tablename__ = "schema_version"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    # Which synthetic registry record this maps to, where one exists. Null is
    # normal: an applicant institution need not be in the issuer registry.
    registry_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    applications: Mapped[list["Application"]] = relationship(
        back_populates="institution", cascade="all, delete-orphan"
    )


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False
    )
    # EoA category as submitted, e.g. "extension_of_approval".
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    institution: Mapped[Institution] = relationship(back_populates="applications")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # The content address. Duplicate detection (a later step) will key off
    # this, and it is already how the file store deduplicates.
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(256), nullable=False)
    uploaded_role: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="documents")
    check_runs: Mapped[list["CheckRun"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class CheckRun(Base):
    """One pass of the pipeline over one document.

    The config versions and hashes live here rather than being looked up at
    read time, because they are the answer to "under which rules was this
    judged?" — and the rules on disk will have moved on.
    """

    __tablename__ = "check_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # completed | completed_with_failures | failed | running
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="running")
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)

    # Explicit columns, because "show every run made under qr-checks-v1" is a
    # question we expect to ask.
    extraction_config_version: Mapped[str | None] = mapped_column(String(64))
    certificate_config_version: Mapped[str | None] = mapped_column(String(64))
    qr_config_version: Mapped[str | None] = mapped_column(String(64))
    issuer_config_version: Mapped[str | None] = mapped_column(String(64))
    persistence_config_version: Mapped[str | None] = mapped_column(String(64))
    # Added in step 6. Nullable, because runs made before step 6 existed are
    # not retrospectively annotated with rules that were not applied to them.
    signature_config_version: Mapped[str | None] = mapped_column(String(64))
    duplicate_config_version: Mapped[str | None] = mapped_column(String(64))

    # Hashes of the config files as loaded, keyed by name. A dict rather than
    # four more columns: these exist to detect that a file changed, not to be
    # grouped or filtered on.
    config_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    prompt_version: Mapped[str | None] = mapped_column(String(128))
    model_name: Mapped[str | None] = mapped_column(String(256))
    provider: Mapped[str | None] = mapped_column(String(64))
    # Of the synthetic issuer registry the lookup was answered from.
    registry_hash: Mapped[str | None] = mapped_column(String(64))

    document: Mapped[Document] = relationship(back_populates="check_runs")
    stage_results: Mapped[list["StageResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    page_extractions: Mapped[list["PageExtraction"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    extracted_fields: Mapped[list["ExtractedField"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    raw_llm_responses: Mapped[list["RawLLMResponse"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    qr_detections: Mapped[list["QRDetectionRow"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    verification_lookups: Mapped[list["VerificationLookup"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    findings: Mapped[list["FindingRow"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    signature_details: Mapped[list["SignatureRow"]] = relationship(
        cascade="all, delete-orphan"
    )
    perceptual_hashes: Mapped[list["PerceptualHashRow"]] = relationship(
        cascade="all, delete-orphan",
        foreign_keys="PerceptualHashRow.run_id",
    )


class StageResult(Base):
    """What each pipeline stage did, including nothing.

    A stage that was skipped gets a row saying so, with the reason. Omitting it
    would make "skipped because OCR failed" indistinguishable from "ran and
    found nothing", which are opposite facts for a reviewer.
    """

    __tablename__ = "stage_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    # ok | degraded | skipped | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # The stage's own status string, e.g. step 1's "partial" or step 4's
    # "source_unavailable". Kept verbatim rather than flattened.
    detail_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[CheckRun] = relationship(back_populates="stage_results")

    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_stage_per_run"),
    )


class PageExtraction(Base):
    __tablename__ = "page_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    text_layer_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Kept separately when OCR ran on a page that also had a text layer, so
    # the two readings can be compared later.
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    low_confidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[CheckRun] = relationship(back_populates="page_extractions")


class ExtractedField(Base):
    __tablename__ = "extracted_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # JSON because `programme` is a list while the rest are strings. Storing
    # the native shape avoids a parse-on-read that would differ per field.
    value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    source_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    value_iso: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Why a value was discarded: snippet_not_in_source, value_not_in_source, …
    invalidation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # What the model claimed, kept even when the value was rejected, so a
    # reviewer can see what was discarded and why.
    model_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)

    run: Mapped[CheckRun] = relationship(back_populates="extracted_fields")

    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_field_per_run"),
    )


class RawLLMResponse(Base):
    """Raw model output, in its own table.

    Separate from `extracted_fields` on purpose: these are unvalidated text,
    and keeping them in the same table as checked values would invite reading
    them as data. Written only when runs.store_raw_llm_responses is true.
    """

    __tablename__ = "raw_llm_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[CheckRun] = relationship(back_populates="raw_llm_responses")


class QRDetectionRow(Base):
    __tablename__ = "qr_detections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [x, y, w, h] in pixels of the image it was decoded from.
    bounding_box: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    decoder: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preprocessing: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parsed_host: Mapped[str | None] = mapped_column(String(512), nullable=True)
    certificate_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    run: Mapped[CheckRun] = relationship(back_populates="qr_detections")


class VerificationLookup(Base):
    __tablename__ = "verification_lookups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    certificate_id: Mapped[str] = mapped_column(String(256), nullable=False)
    id_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    trust: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # One of the six fixed verification statuses.
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_reasons: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # The per-field comparison table, as JSON: it is read as a whole for one
    # lookup and never queried field by field.
    comparisons: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    issuer_record: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[CheckRun] = relationship(back_populates="verification_lookups")


class FindingRow(Base):
    """One finding, from any detector.

    `review_status` lives here rather than in review_actions because the
    current state is what every queue query filters on; the actions table is
    the history of how it got there.
    """

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    detector: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # "strong" or "weak", as the detector's config labelled it. Never a score.
    strength: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # Only where a detector reports one. The certificate detectors do not.
    confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[CheckRun] = relationship(back_populates="findings")
    review_actions: Mapped[list["ReviewAction"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class ReviewAction(Base):
    """A human acting on a finding. Append-only by convention, not by trigger.

    Unlike audit_log this is not hash-chained: it is working state a reviewer
    may legitimately add to, and every action is mirrored into audit_log, which
    is the tamper-evident record.
    """

    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    finding: Mapped[FindingRow] = relationship(back_populates="review_actions")


class AuditLog(Base):
    """Append-only, hash-chained record of everything that happened.

    `entry_hash` covers the entry's own content *and* `previous_hash`, so
    altering or removing any entry breaks every entry after it. Deliberately
    holds no document text and no raw model output — only ids pointing at the
    rows that do, so the audit trail cannot become a second, unguarded copy of
    sensitive content.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )


class PerceptualHashRow(Base):
    """A perceptual hash of one rendered page, kept for later comparison.

    Its own table rather than a column on `documents` because a document has
    several hashes - one per kind, and later one per page - and because the
    photo detector will store hashes of submitted photographs here too. The
    `run_id` records which run computed it; the hash belongs to the document.
    """

    __tablename__ = "perceptual_hashes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    # "dhash", "phash", "ahash". Stored as text so adding a kind is additive.
    hash_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Lowercase hex. Width depends on hash_size, which is why hash_size is
    # stored alongside: comparing two different widths is meaningless and the
    # comparison code refuses it.
    hash_value: Mapped[str] = mapped_column(String(256), nullable=False)
    hash_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "page", "hash_kind", name="uq_hash_per_run_page_kind"
        ),
        Index("ix_hash_lookup", "hash_kind", "hash_size"),
    )


class CrossReference(Base):
    """A relationship between two documents, stored once and read from both ends.

    The two document ids are **normalised so the lower id is always `document_a`**.
    That is what makes the result order-independent: processing A then B and
    processing B then A produce the same row, and a unique constraint keeps a
    re-run from adding a second copy of a relationship already known.

    This is also why duplicate detection does not edit old `check_runs`. When B
    arrives and matches A, the fact belongs to both documents, but A's run
    finished before B existed. Writing the relationship here, and reading it
    from both sides, lets A's report show it without anything in A's run being
    rewritten - runs are never overwritten.
    """

    __tablename__ = "cross_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Always the lower of the two ids.
    document_a_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_b_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # duplicate_file_exact | near_duplicate_page_image |
    # certificate_number_reuse | qr_payload_reuse
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Whether the two documents belong to the same institution. Stored because
    # it is what decides the finding's strength, and because an institution can
    # be renamed later - the judgement should stay traceable to what was true.
    same_institution: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # What was compared and what matched: hashes, numbers, distances. No
    # document text.
    evidence: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # The run that discovered it, which is not necessarily either document's
    # latest run.
    discovered_in_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("check_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "document_a_id", "document_b_id", "kind", name="uq_cross_reference_pair"
        ),
    )


class SignatureRow(Base):
    """One PDF signature's integrity detail, stored findings or not.

    Written even for a clean signed file. "This document carries one intact
    signature covering the whole file" is a fact a reviewer wants, and it is
    only visible if the detail is recorded when nothing is wrong - a findings
    table alone cannot distinguish an intact signature from no signature.

    The two `_unverified` columns are named that way on purpose. Nothing in
    this system checks a signing certificate against a chain or a trust store,
    so the signer name and signing time are claims printed in the file, and the
    column name is the last place that is still true at the point of reading.
    """

    __tablename__ = "signature_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), nullable=False
    )
    # Position within the file, so two unnamed signatures stay distinguishable.
    signature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The /ByteRange array as offset/length pairs.
    byte_range: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    coverage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    covers_whole_file: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Nullable three-state: True, False, or None for "no comparison possible".
    digest_matches: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    digest_algorithm: Mapped[str | None] = mapped_column(String(32), nullable=True)
    computed_digest_hex: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signed_digest_hex: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Size and shape of anything appended after the signed revision. Never the
    # appended content: that would put document material in the database under
    # a name nobody would think to redact.
    appended_byte_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appended_object_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signer_name_unverified: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    claimed_signing_time_unverified: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "signature_index", name="uq_signature_per_run"
        ),
    )
