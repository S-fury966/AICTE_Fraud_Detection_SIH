"""Build the evaluation dataset: documents, manifest, labels, issuer registry.

    python -m evaluation.generate [--seed N] [--out DIR] [--only CASE] [--quiet]

**Independence.** Nothing here imports a detector, a validator or the
pipeline. The labels say what this module *did* to each document. That is the
only reason the resulting numbers mean anything: a generator that asked a
checker what it had built would be measuring the checker against itself.

**Determinism.** Every choice comes from the configured seed through named
sub-streams (`"form-7"`, `"noise-7"`, ...) rather than one shared generator.
With a single stream, adding a case type would shift every later draw and the
whole dataset would change; with named streams, case 7's noise level depends on
the seed and the number 7 and nothing else. A test asserts that the same seed
gives byte-identical output.

**Ordering.** Cases built from an earlier document - the duplicates - are
emitted after their sources, because a duplicate only exists relative to a
corpus. `processing_order` in the manifest is authoritative and the runner
follows it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.config import get, load_config

from . import degrade, recipes, signing, templates
from .world import World, build_world, write_world

# Fraud payload shapes read from config, with these as the fallback so a
# missing config block cannot silently produce a *valid* URL for a case whose
# whole purpose is to carry a bad one.
_FALLBACK_HOSTS = {
    "lookalike": "verify.nbta-demo.test.evil-mirror.test",
    "userinfo_host": "evil-mirror.test",
    "punycode": "xn--80ak6aa92e.test",
    "shortener": "bit.ly/3xEvalDemo",
}


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass
class PlannedCase:
    """One application, decided before anything is rendered."""

    index: int
    case_type: str
    processing_order: int
    # Set for cases built from an earlier document.
    source_index: int | None = None


def plan_cases(config: dict[str, Any]) -> list[PlannedCase]:
    """Decide the whole set up front, sources before duplicates.

    Two passes rather than one shuffled list: a duplicate case must be able to
    name an existing clean case as its source, and that is only possible if the
    independent cases are laid out first.
    """
    counts = dict(config.get("cases") or {})
    states = dict(config.get("issuer_states") or {})
    for key, count in states.items():
        counts[f"issuer_state_{key}"] = int(count)

    independent: list[str] = []
    dependent: list[str] = []
    for case_type in sorted(counts):
        count = int(counts[case_type])
        if count <= 0:
            continue
        recipe = recipes.recipe_for(case_type)
        target = dependent if recipe.needs_source else independent
        target.extend([case_type] * count)

    planned: list[PlannedCase] = []
    index = 0
    for case_type in independent:
        planned.append(PlannedCase(index=index, case_type=case_type,
                                   processing_order=index + 1))
        index += 1

    # Sources are drawn from the clean cases only. A duplicate of a hard
    # negative or of another fraud would make the expectation ambiguous: a
    # finding could then come from either the source's own defect or from the
    # duplication, and the label could not say which was being measured.
    clean_indices = [
        case.index for case in planned if case.case_type == "clean"
    ]
    if dependent and not clean_indices:
        raise ValueError(
            "the plan has cases that need a source document but no clean "
            "cases to draw one from; raise `cases.clean` in config"
        )

    rng = random.Random(f"sources-{config.get('seed')}")
    for offset, case_type in enumerate(dependent):
        source = clean_indices[offset % len(clean_indices)]
        # One of the same-institution resubmissions deliberately reuses a
        # source another case already used, so the corpus contains a document
        # with more than one relationship.
        if case_type == "hard_negative_same_institution_resubmission":
            source = clean_indices[rng.randrange(len(clean_indices))]
        planned.append(
            PlannedCase(
                index=index,
                case_type=case_type,
                processing_order=index + 1,
                source_index=source,
            )
        )
        index += 1

    return planned


def assign_splits(
    planned: list[PlannedCase], config: dict[str, Any]
) -> dict[int, str]:
    """Deterministic, stratified dev/holdout split.

    Stratified by case type on purpose. A random 70/30 over sixty
    applications would leave some case types entirely absent from holdout, and
    a holdout number that does not cover a case type says nothing about it.
    """
    ratio = float(get(config, "split.dev_ratio", 0.7))
    seed = config.get("seed")

    by_type: dict[str, list[int]] = {}
    for case in planned:
        by_type.setdefault(case.case_type, []).append(case.index)

    splits: dict[int, str] = {}
    for case_type in sorted(by_type):
        indices = sorted(by_type[case_type])
        rng = random.Random(f"split-{seed}-{case_type}")
        shuffled = list(indices)
        rng.shuffle(shuffled)
        # At least one in dev whenever the type has any case at all, so the
        # dev numbers cover every type.
        dev_count = max(1, round(len(shuffled) * ratio))
        dev_count = min(dev_count, len(shuffled))
        for position, case_index in enumerate(shuffled):
            splits[case_index] = "dev" if position < dev_count else "holdout"
    return splits


# --------------------------------------------------------------------------
# Field construction
# --------------------------------------------------------------------------


def _weighted_choice(rng: random.Random, weights: dict[str, Any]) -> str:
    keys = sorted(weights)
    values = [float(weights[key]) for key in keys]
    total = sum(values) or 1.0
    draw = rng.random() * total
    running = 0.0
    for key, value in zip(keys, values):
        running += value
        if draw <= running:
            return key
    return keys[-1]


def _format_date(value: date) -> str:
    """Day-first with a spelled month, which is what these certificates print."""
    return value.strftime("%d %B %Y").lstrip("0")


@dataclass
class BuiltCase:
    """Everything the generator knows about one finished application."""

    index: int
    case_type: str
    processing_order: int
    split: str
    institution_id: str
    institution_name: str
    template_key: str
    form: str
    noise: degrade.NoiseSettings
    fields: templates.CertificateFields
    qr_payload: str | None
    document_path: str
    document_sha256: str
    source_index: int | None = None
    # The registry record this case implies, if it adds one.
    registry_record: dict[str, Any] | None = None
    pending_certificate_id: str | None = None
    notes: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class Generator:
    """Builds the dataset. Holds the world, the registry and the running plan."""

    def __init__(self, config: dict[str, Any], out_root: Path) -> None:
        self.config = config
        self.seed = int(config.get("seed", 0))
        self.out_root = out_root
        self.documents_dir = out_root / str(
            get(config, "output.documents_dir", "documents")
        )
        self.world: World = build_world(config)
        self.template_list: list[dict[str, Any]] = list(config.get("templates") or [])
        self.deceptive = {
            **_FALLBACK_HOSTS,
            **(config.get("deceptive_hosts") or {}),
        }
        self.devanagari_font = templates.devanagari_font()
        self.devanagari_skipped: list[str] = []
        self.devanagari_notes: list[str] = []
        self._signer: Any | None = None
        self.built: list[BuiltCase] = []
        # certificate ids already used, so a "unique" number really is.
        self._issued_ids: set[str] = set()

    # ------------------------------------------------------------------

    @property
    def signer(self) -> Any:
        """One throwaway key per generation run, created on first use."""
        if self._signer is None:
            self._signer = signing.throwaway_signer()
        return self._signer

    def rng(self, purpose: str, index: int) -> random.Random:
        """A named sub-stream. See the module docstring for why not one stream."""
        return random.Random(f"{purpose}-{self.seed}-{index}")

    def template_for(self, case: PlannedCase, recipe: recipes.Recipe) -> dict[str, Any]:
        if recipe.force_template:
            for entry in self.template_list:
                if entry.get("key") == recipe.force_template:
                    return entry
        rng = self.rng("template", case.index)
        # The no-QR layout is only chosen when a case asks for it; otherwise
        # one issuer would silently account for a fifth of the QR cases.
        candidates = [
            entry
            for entry in self.template_list
            if str(entry.get("qr_position")) != "none"
        ] or self.template_list
        return candidates[rng.randrange(len(candidates))]

    def institution_for(self, case: PlannedCase) -> Any:
        rng = self.rng("institution", case.index)
        return self.world.institutions[rng.randrange(len(self.world.institutions))]

    def certificate_id(self, body: Any, case: PlannedCase, salt: str = "") -> str:
        """A unique, well-formed certificate id for this issuing body."""
        rng = self.rng(f"certid{salt}", case.index)
        for _ in range(500):
            year = rng.choice([2023, 2024, 2025])
            serial = rng.randrange(100, 99999)
            candidate = f"{body.id_prefix}/EVAL/{year}/{serial:05d}"
            if candidate not in self._issued_ids:
                self._issued_ids.add(candidate)
                return candidate
        raise RuntimeError("ran out of unused certificate ids")

    def qr_url(self, body: Any, certificate_id: str) -> str:
        """The agreed demo format: the id percent-encoded into one segment."""
        return f"https://{body.domain}/c/{quote(certificate_id, safe='')}"

    def base_fields(
        self, case: PlannedCase, institution: Any, body: Any, certificate_id: str
    ) -> templates.CertificateFields:
        rng = self.rng("fields", case.index)
        valid_from = date(rng.choice([2022, 2023, 2024]), rng.randint(1, 12), 1)
        # The window must still be open at the fixed evaluation date, or the
        # case is not clean - it is an expiry case, and the detector would be
        # right to say so. Only `issuer_state_expired` sets a lapsed window,
        # and it does so explicitly.
        valid_to = valid_from + timedelta(days=365 * rng.choice([5, 6, 7]))
        issue = valid_from - timedelta(days=rng.randint(10, 90))
        return templates.CertificateFields(
            institution_name=institution.name,
            certificate_number=certificate_id,
            issuing_body=body.name,
            programme=rng.choice(self.world.programmes),
            accreditation_status_or_grade=rng.choice(self.world.grades),
            valid_from=_format_date(valid_from),
            valid_to=_format_date(valid_to),
            issue_date=_format_date(issue),
            signatory_name=rng.choice(
                [
                    "Dr. A. Raghunathan, Member Secretary",
                    "Prof. S. Mahalingam, Chairperson",
                    "Dr. V. Deshpande, Director (Accreditation)",
                ]
            ),
        )

    # ------------------------------------------------------------------
    # Registry records
    # ------------------------------------------------------------------

    def _iso(self, printed: str) -> str:
        """Printed date back to ISO, for the registry record.

        The generator knows the date it printed, so this is a reformat rather
        than a parse of unknown text - no date inference anywhere.
        """
        from datetime import datetime

        return datetime.strptime(printed, "%d %B %Y").date().isoformat()

    def registry_record(
        self,
        fields: templates.CertificateFields,
        issuer_status: str = "active",
        valid_to_override: str | None = None,
        institution_override: str | None = None,
    ) -> dict[str, Any]:
        return {
            "certificate_id": fields.certificate_number,
            "institution_name": institution_override or fields.institution_name,
            "issuing_body": fields.issuing_body,
            "programmes": [fields.programme],
            "grade": fields.accreditation_status_or_grade,
            "valid_from": self._iso(fields.valid_from),
            "valid_to": valid_to_override or self._iso(fields.valid_to),
            "issuer_status": issuer_status,
            "record_updated": "2026-06-01",
        }

    # ------------------------------------------------------------------
    # Rendering a finished document
    # ------------------------------------------------------------------

    def form_for(self, case: PlannedCase, recipe: recipes.Recipe) -> str:
        if recipe.force_form:
            return recipe.force_form
        rng = self.rng("form", case.index)
        return _weighted_choice(rng, get(self.config, "forms.weights", {}) or {})

    def noise_for(self, case: PlannedCase, recipe: recipes.Recipe) -> degrade.NoiseSettings:
        if recipe.force_noise:
            return degrade.settings_for(recipe.force_noise, self.config)
        rng = self.rng("noise", case.index)
        level = _weighted_choice(rng, get(self.config, "noise.weights", {}) or {})
        return degrade.settings_for(level, self.config)

    def materialise(
        self,
        case: PlannedCase,
        pdf_bytes: bytes,
        form: str,
        noise: degrade.NoiseSettings,
    ) -> tuple[bytes, str]:
        """Turn a rendered PDF into the form it arrives in. Returns (bytes, ext).

        A digital PDF skips degradation entirely: degrading a text layer is
        not a thing that happens, and pretending otherwise would make the
        noise-level breakdown meaningless for the form where OCR never runs.
        """
        dpi = int(get(self.config, "noise.render_dpi", 200))

        if form == "digital_pdf":
            return pdf_bytes, ".pdf"

        if form == "signed_pdf":
            return signing.sign(pdf_bytes, self.signer), ".pdf"

        image = templates.render_page_image(pdf_bytes, dpi=dpi)
        image = degrade.apply(image, noise, seed=self.seed + case.index)

        if form == "jpg_scan":
            return degrade.to_jpeg(image, noise.jpeg_quality), ".jpg"
        if form == "scanned_pdf":
            # Round-tripped through JPEG first, so the page inside the PDF
            # carries the same compression artefacts a real scan would.
            import io

            from PIL import Image

            jpeg = degrade.to_jpeg(image, noise.jpeg_quality)
            return templates.image_to_pdf(Image.open(io.BytesIO(jpeg))), ".pdf"

        raise ValueError(f"unknown form {form!r}")

    def write_document(self, case_index: int, data: bytes, extension: str) -> tuple[str, str]:
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        name = f"app-{case_index + 1:04d}{extension}"
        path = self.documents_dir / name
        path.write_bytes(data)
        relative = f"{get(self.config, 'output.documents_dir', 'documents')}/{name}"
        return relative, hashlib.sha256(data).hexdigest()

    def render(
        self,
        fields: templates.CertificateFields,
        template: dict[str, Any],
        qr_payload: str | None,
        body: Any,
        uncompressed: bool = False,
    ) -> bytes:
        if template.get("devanagari_heading") and not self.devanagari_font:
            note = (
                f"template {template['key']!r} asked for a Devanagari heading "
                "but no font supporting the script was found on this machine; "
                "rendered without it"
            )
            if note not in self.devanagari_skipped:
                self.devanagari_skipped.append(note)
        elif template.get("devanagari_heading"):
            # Reported so the coverage is visible either way. Earlier versions
            # of this branch fired for every template *without* a Devanagari
            # heading, which is four of the five, and printed a stale
            # "disabled" note on a run where it was enabled.
            note = (
                f"template {template['key']!r} renders a Devanagari heading "
                f"using {self.devanagari_font}"
            )
            if note not in self.devanagari_notes:
                self.devanagari_notes.append(note)
        return templates.render_pdf(
            fields,
            template,
            qr_payload,
            abbreviation=body.abbreviation,
            devanagari_font_path=self.devanagari_font,
            uncompressed=uncompressed,
        )

    # ------------------------------------------------------------------
    # Building one case
    # ------------------------------------------------------------------

    def build(self, case: PlannedCase) -> BuiltCase:
        """Dispatch to the recipe's own builder.

        Each branch does exactly one deliberate thing to an otherwise clean
        document, and the label records that one thing. A case that did two
        would make a miss impossible to attribute.
        """
        recipe = recipes.recipe_for(case.case_type)
        handler = getattr(self, f"_build_{case.case_type}", None)
        if handler is None:
            handler = self._build_generic_genuine
        return handler(case, recipe)

    # ------------------------------------------------------- genuine baseline

    def _genuine_parts(self, case: PlannedCase, recipe: recipes.Recipe):
        """The common opening move: a clean certificate nobody has touched."""
        institution = self.institution_for(case)
        template = self.template_for(case, recipe)
        body = self.world.body(str(template["issuer"]))
        certificate_id = self.certificate_id(body, case)
        fields = self.base_fields(case, institution, body, certificate_id)
        payload = (
            self.qr_url(body, certificate_id)
            if body.uses_qr and str(template.get("qr_position")) != "none"
            else None
        )
        return institution, template, body, fields, payload

    def _finish(
        self,
        case: PlannedCase,
        recipe: recipes.Recipe,
        institution: Any,
        template: dict[str, Any],
        body: Any,
        fields: templates.CertificateFields,
        payload: str | None,
        *,
        pdf_bytes: bytes | None = None,
        registry_record: dict[str, Any] | None = None,
        pending_certificate_id: str | None = None,
        source_index: int | None = None,
        extras: dict[str, Any] | None = None,
        form: str | None = None,
        document_bytes: bytes | None = None,
        extension: str | None = None,
    ) -> BuiltCase:
        """Render, degrade, write and record. Shared by every branch."""
        chosen_form = form or self.form_for(case, recipe)
        noise = self.noise_for(case, recipe)

        if document_bytes is None:
            if pdf_bytes is None:
                pdf_bytes = self.render(fields, template, payload, body)
            document_bytes, extension = self.materialise(
                case, pdf_bytes, chosen_form, noise
            )

        relative, digest = self.write_document(
            case.index, document_bytes, extension or ".pdf"
        )
        return BuiltCase(
            index=case.index,
            case_type=case.case_type,
            processing_order=case.processing_order,
            split="dev",
            institution_id=institution.id,
            institution_name=institution.name,
            template_key=str(template["key"]),
            form=chosen_form,
            noise=noise,
            fields=fields,
            qr_payload=payload,
            document_path=relative,
            document_sha256=digest,
            source_index=source_index if source_index is not None else case.source_index,
            registry_record=registry_record,
            pending_certificate_id=pending_certificate_id,
            notes=recipe.notes,
            extras=dict(extras or {}),
        )

    def _build_generic_genuine(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=self.registry_record(fields),
        )

    _build_clean = _build_generic_genuine
    _build_hard_negative_noisy_scan = _build_generic_genuine
    _build_hard_negative_blurred_qr = _build_generic_genuine
    _build_hard_negative_signed_untouched = _build_generic_genuine
    _build_hard_negative_issuer_without_qr = _build_generic_genuine

    # -------------------------------------------------------- hard negatives

    def _build_hard_negative_abbreviated_institution(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        # The registry keeps the full name; the page prints the short form.
        record = self.registry_record(fields, institution_override=institution.name)
        fields.institution_name = institution.abbreviated_name
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            extras={
                "registry_institution_name": institution.name,
                "printed_institution_name": institution.abbreviated_name,
            },
        )

    def _build_hard_negative_missing_grade(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        record = self.registry_record(fields)
        # Printed with no grade at all. The registry still has one, so this is
        # the "null is not a mismatch" case.
        fields.accreditation_status_or_grade = None
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            extras={"registry_grade": record["grade"], "printed_grade": None},
        )

    def _build_hard_negative_same_template_pair(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        """A second institution's genuine certificate on the same layout.

        Forced onto one fixed template so the pair really does share a layout:
        letting the template be chosen at random would make this case a
        same-template pair only by luck.
        """
        institution = self.institution_for(case)
        template = self.template_list[0]
        body = self.world.body(str(template["issuer"]))
        certificate_id = self.certificate_id(body, case)
        fields = self.base_fields(case, institution, body, certificate_id)
        payload = self.qr_url(body, certificate_id)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=self.registry_record(fields),
            form="digital_pdf",
            extras={"shared_template": template["key"]},
        )

    def _build_hard_negative_same_institution_resubmission(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        """The same institution's own file, submitted again, byte for byte."""
        source = self.built[case.source_index]
        data = (self.out_root / source.document_path).read_bytes()
        extension = Path(source.document_path).suffix
        institution = self.world.institution(source.institution_id)
        template = self._template_by_key(source.template_key)
        body = self.world.body(str(template["issuer"]))
        return self._finish(
            case, recipe, institution, template, body, source.fields,
            source.qr_payload,
            document_bytes=data,
            extension=extension,
            form=source.form,
            source_index=source.index,
            extras={"source_application_index": source.index},
        )

    # ------------------------------------------------- awkward issuer states

    def _issuer_state(
        self, case: PlannedCase, recipe: recipes.Recipe, state: str
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)

        pending_id = None
        record: dict[str, Any] | None
        if state == "pending":
            # A pending id is listed separately: the service answers 202 for
            # it, which is neither a yes nor a no.
            record = self.registry_record(fields)
            pending_id = fields.certificate_number
        elif state == "expired":
            # Expired per the issuer, against the fixed evaluation date. The
            # page prints the same window, so the document is honest.
            record = self.registry_record(fields, valid_to_override="2025-06-30")
            fields.valid_to = "30 June 2025"
            record["valid_to"] = "2025-06-30"
        else:
            record = self.registry_record(fields, issuer_status=state)

        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            pending_certificate_id=pending_id,
            form="digital_pdf",
            extras={"issuer_state": state},
        )

    def _build_issuer_state_revoked(self, case, recipe):
        return self._issuer_state(case, recipe, "revoked")

    def _build_issuer_state_suspended(self, case, recipe):
        return self._issuer_state(case, recipe, "suspended")

    def _build_issuer_state_expired(self, case, recipe):
        return self._issuer_state(case, recipe, "expired")

    def _build_issuer_state_pending(self, case, recipe):
        return self._issuer_state(case, recipe, "pending")

    # ------------------------------------------------------------- QR frauds

    def _build_number_edited_qr_untouched(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        original = fields.certificate_number
        # The registry holds the ORIGINAL, which the QR still carries.
        record = self.registry_record(fields)
        fields.certificate_number = _edit_last_digit(original)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            form="digital_pdf",
            extras={
                "original_certificate_number": original,
                "printed_certificate_number": fields.certificate_number,
                "qr_certificate_id": original,
            },
        )

    def _build_number_edited_qr_consistent(
        self, case: PlannedCase, recipe: recipes.Recipe
    ) -> BuiltCase:
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        original = fields.certificate_number
        record = self.registry_record(fields)
        edited = _edit_last_digit(original)
        fields.certificate_number = edited
        # Both sources moved together, so the internal cross-check agrees.
        payload = self.qr_url(body, edited)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            form="digital_pdf",
            extras={
                "original_certificate_number": original,
                "edited_certificate_number": edited,
                "note": "the registry knows only the original number",
            },
        )

    def _build_qr_lookalike_domain(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        host = str(self.deceptive["lookalike"])
        payload = (
            f"https://{host}/c/{quote(fields.certificate_number, safe='')}"
        )
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=self.registry_record(fields),
            form="digital_pdf",
            extras={"deceptive_host": host},
        )

    def _build_qr_userinfo_trick(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        host = str(self.deceptive["userinfo_host"])
        payload = (
            f"https://{body.domain}@{host}/c/"
            f"{quote(fields.certificate_number, safe='')}"
        )
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=self.registry_record(fields),
            form="digital_pdf",
            extras={"deceptive_host": host, "trick": "userinfo"},
        )

    def _build_qr_punycode_or_shortener(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        # Alternates, so both shapes appear whatever the count.
        use_punycode = case.index % 2 == 0
        if use_punycode:
            host = str(self.deceptive["punycode"])
            payload = (
                f"https://{host}/c/{quote(fields.certificate_number, safe='')}"
            )
            trick = "punycode_homoglyph"
        else:
            payload = f"https://{self.deceptive['shortener']}"
            host = str(self.deceptive["shortener"]).split("/")[0]
            trick = "shortener"
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=self.registry_record(fields),
            form="digital_pdf",
            extras={"deceptive_host": host, "trick": trick},
        )

    def _build_qr_id_belongs_to_other_institution(self, case, recipe):
        """The QR carries another institution's genuine certificate id."""
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        own_record = self.registry_record(fields)

        # A second, real record held by somebody else. Written to the registry
        # here so the lookup genuinely resolves.
        other = self._other_institution(case, institution.id)
        other_id = self.certificate_id(body, case, salt="-other")
        other_fields = self.base_fields(case, other, body, other_id)
        other_record = self.registry_record(other_fields)

        payload = self.qr_url(body, other_id)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=own_record,
            form="digital_pdf",
            extras={
                "extra_registry_records": [other_record],
                "qr_certificate_id": other_id,
                "qr_id_institution": other.name,
            },
        )

    def _build_unknown_certificate(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        # No registry record at all. Everything about the page is well formed.
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=None,
            form="digital_pdf",
            extras={"note": "deliberately absent from the registry"},
        )

    def _build_institution_name_swapped(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        # The registry record belongs to the real holder.
        record = self.registry_record(fields)
        other = self._other_institution(case, institution.id)
        fields.institution_name = other.name
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            form="digital_pdf",
            extras={
                "registry_institution_name": record["institution_name"],
                "printed_institution_name": other.name,
            },
        )

    def _build_dates_extended(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        record = self.registry_record(fields)
        from datetime import datetime

        real_to = datetime.strptime(fields.valid_to, "%d %B %Y").date()
        extended = real_to.replace(year=real_to.year + 3)
        fields.valid_to = _format_date(extended)
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            registry_record=record,
            form="digital_pdf",
            extras={
                "registry_valid_to": record["valid_to"],
                "printed_valid_to": fields.valid_to,
            },
        )

    # ------------------------------------------------------ signature frauds

    def _build_signed_then_tampered(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        original = fields.certificate_number
        record = self.registry_record(fields)

        # Uncompressed, so the printed digit is literally there to edit. This
        # is the only case that needs it, and it is signed anyway - so the
        # reproducibility cost is already paid.
        pdf_bytes = self.render(fields, template, payload, body, uncompressed=True)
        signed = signing.sign(pdf_bytes, self.signer)
        # One printed digit, inside the signed range, same length.
        tail = original[-5:]
        tampered = signing.tamper_inside_signed_range(
            signed, tail, _edit_last_digit(tail)
        )
        printed_number = _edit_last_digit(original)
        fields.certificate_number = printed_number
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            document_bytes=tampered,
            extension=".pdf",
            form="signed_pdf",
            registry_record=record,
            extras={
                "original_certificate_number": original,
                "printed_certificate_number": printed_number,
                "byte_range": signing.byte_range_of(tampered),
            },
        )

    def _build_signed_then_appended(self, case, recipe):
        institution, template, body, fields, payload = self._genuine_parts(case, recipe)
        pdf_bytes = self.render(fields, template, payload, body)
        signed = signing.sign(pdf_bytes, self.signer)
        appended = signing.append_incremental_revision(
            signed, "appended by the evaluation generator after signing"
        )
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            document_bytes=appended,
            extension=".pdf",
            form="signed_pdf",
            registry_record=self.registry_record(fields),
            extras={
                "appended_bytes": len(appended) - len(signed),
                "byte_range": signing.byte_range_of(appended),
            },
        )

    # ------------------------------------------------------ duplicate frauds

    def _build_duplicate_file_other_institution(self, case, recipe):
        """An exact copy of an earlier clean document, different institution."""
        source = self.built[case.source_index]
        data = (self.out_root / source.document_path).read_bytes()
        institution = self._other_institution(case, source.institution_id)
        template = self._template_by_key(source.template_key)
        body = self.world.body(str(template["issuer"]))
        return self._finish(
            case, recipe, institution, template, body, source.fields,
            source.qr_payload,
            document_bytes=data,
            extension=Path(source.document_path).suffix,
            form=source.form,
            source_index=source.index,
            extras={
                "source_application_index": source.index,
                "submitting_institution": institution.name,
                "certificate_holder_per_page": source.fields.institution_name,
            },
        )

    def _build_number_reuse_other_institution(self, case, recipe):
        """The same number, re-rendered on a DIFFERENT layout.

        Different layout on purpose: it isolates the number check, because the
        file hash differs and the two pages do not look alike.
        """
        source = self.built[case.source_index]
        institution = self._other_institution(case, source.institution_id)
        source_template = self._template_by_key(source.template_key)
        template = self._different_template(source.template_key)
        body = self.world.body(str(template["issuer"]))

        fields = templates.CertificateFields(**vars(source.fields))
        fields.institution_name = institution.name
        payload = (
            self.qr_url(body, fields.certificate_number)
            if str(template.get("qr_position")) != "none"
            else None
        )
        return self._finish(
            case, recipe, institution, template, body, fields, payload,
            form="digital_pdf",
            source_index=source.index,
            extras={
                "source_application_index": source.index,
                "reused_certificate_number": fields.certificate_number,
                "source_template": source_template["key"],
                "this_template": template["key"],
            },
        )

    def _build_resaved_copy_with_edit(self, case, recipe):
        """An earlier document, recompressed, with one field edited.

        The certificate NUMBER is kept and the grade is edited, so the number
        check should fire strongly and the image check should corroborate it.
        Editing the number instead would make this indistinguishable from
        `number_edited_*` and measure the same thing twice.
        """
        source = self.built[case.source_index]
        institution = self._other_institution(case, source.institution_id)
        template = self._template_by_key(source.template_key)
        body = self.world.body(str(template["issuer"]))

        fields = templates.CertificateFields(**vars(source.fields))
        original_grade = fields.accreditation_status_or_grade
        fields.accreditation_status_or_grade = _bump_grade(original_grade)

        noise = degrade.settings_for("light", self.config)
        pdf_bytes = self.render(fields, template, source.qr_payload, body)
        image = templates.render_page_image(
            pdf_bytes, dpi=int(get(self.config, "noise.render_dpi", 200))
        )
        image = degrade.apply(image, noise, seed=self.seed + case.index)
        data = degrade.to_jpeg(image, 70)

        return self._finish(
            case, recipe, institution, template, body, fields, source.qr_payload,
            document_bytes=data,
            extension=".jpg",
            form="jpg_scan",
            source_index=source.index,
            extras={
                "source_application_index": source.index,
                "original_grade": original_grade,
                "edited_grade": fields.accreditation_status_or_grade,
                "kept_certificate_number": fields.certificate_number,
            },
        )

    # ------------------------------------------------------------- helpers

    def _template_by_key(self, key: str) -> dict[str, Any]:
        for entry in self.template_list:
            if entry.get("key") == key:
                return entry
        return self.template_list[0]

    def _different_template(self, key: str) -> dict[str, Any]:
        for entry in self.template_list:
            if entry.get("key") != key and str(entry.get("qr_position")) != "none":
                return entry
        return self.template_list[0]

    def _other_institution(self, case: PlannedCase, avoid_id: str) -> Any:
        rng = self.rng("other-institution", case.index)
        candidates = [i for i in self.world.institutions if i.id != avoid_id]
        return candidates[rng.randrange(len(candidates))]


def _edit_last_digit(value: str) -> str:
    """Change the final digit, keeping the length identical.

    Length matters for the signed case: a replacement of a different length
    would move every later offset in the file, so the edit would no longer be
    the thing the test claims it is.
    """
    for position in range(len(value) - 1, -1, -1):
        if value[position].isdigit():
            digit = int(value[position])
            replacement = str((digit + 1) % 10)
            return value[:position] + replacement + value[position + 1 :]
    raise ValueError(f"no digit to edit in {value!r}")


def _bump_grade(grade: str | None) -> str:
    ladder = ["B", "B+", "A", "A+", "A++"]
    if grade in ladder:
        return ladder[min(ladder.index(grade) + 1, len(ladder) - 1)]
    return "A++"

# --------------------------------------------------------------------------
# Labels, manifest and the evaluation registry
# --------------------------------------------------------------------------


def _label_for(
    built: BuiltCase,
    recipe: recipes.Recipe,
    pair_index: int | None,
) -> dict[str, Any]:
    """One label entry. Written from the recipe, never from a detector."""
    return {
        # 1-based, and equal to the application id the runner creates, so the
        # two can be joined without a lookup table.
        "application_id": built.index + 1,
        "institution": built.institution_name,
        "institution_id": built.institution_id,
        "source_recipe": built.case_type,
        "case_type": built.case_type,
        "is_fraud": recipe.is_fraud,
        "fraud_types": list(recipe.fraud_types),
        "split": built.split,
        # What is actually printed on the page. This is the ground truth for
        # extraction: whatever is here is what step 2 should return.
        "printed_fields": built.fields.printed(),
        "qr_payload": built.qr_payload,
        "expected": [group.to_dict() for group in recipe.expected],
        "expected_issuer_status": recipe.expected_issuer_status,
        "must_not": list(recipe.must_not),
        "processing_order": built.processing_order,
        "noise": built.noise.to_dict(),
        "form": built.form,
        "template": built.template_key,
        "document": built.document_path,
        # Null for a signed document, for the same reason as in the manifest:
        # a signature embeds a signing time and a freshly generated throwaway
        # key, so its bytes differ between runs. The label's *content* - what
        # was printed, what was expected - is unchanged, and that is what the
        # measurement uses.
        "document_sha256": (
            None if built.form == "signed_pdf" else built.document_sha256
        ),
        "reproducible_bytes": built.form != "signed_pdf",
        # The other half of a duplicate pair, as an application id, so a group
        # marked `either_side` can be checked against it.
        "pair_application_id": pair_index,
        "notes": recipe.notes,
        "recipe_details": _reproducible_extras(built),
    }


def _reproducible_extras(built: BuiltCase) -> dict[str, Any]:
    """Recipe details, minus anything that varies between runs.

    `byte_range` and `appended_bytes` depend on the size of the signature
    container, which depends on the throwaway key, so they move from run to
    run. They are dropped rather than kept: both are readable from the
    document itself, and keeping them would make the labels non-deterministic
    for information that is already there.
    """
    varies = {"byte_range", "appended_bytes"}
    return {
        key: value
        for key, value in (built.extras or {}).items()
        if key not in varies
    }


def build_registry_data(
    built: list[BuiltCase], world: World, config: dict[str, Any]
) -> dict[str, Any]:
    """The evaluation's own issuer registry.

    Separate from the demo registry on purpose. The demo one backs step 4's
    tests, and a generator that mutated it would silently change what those
    tests mean.
    """
    records: list[dict[str, Any]] = []
    pending: list[str] = []
    seen: set[str] = set()

    for case in built:
        for record in [case.registry_record] + list(
            case.extras.get("extra_registry_records") or []
        ):
            if not record:
                continue
            certificate_id = str(record["certificate_id"])
            if certificate_id in seen:
                continue
            seen.add(certificate_id)
            records.append(record)
        if case.pending_certificate_id:
            pending.append(str(case.pending_certificate_id))

    records.sort(key=lambda entry: str(entry["certificate_id"]))
    return {
        "registry_version": f"eval-registry-{config.get('config_version')}",
        "seed": config.get("seed"),
        "note": (
            "Synthetic registry for the step 7 evaluation. Every institution, "
            "issuing body, certificate id and domain is fictional; all domains "
            "use the reserved .test TLD. Separate from data/demo/ so the demo "
            "registry and step 4's tests are never affected."
        ),
        "issuer_name": "Evaluation Issuer Aggregate (Demo)",
        "issuer_domain": "verify.eval-demo.test",
        "pending_certificate_ids": sorted(pending),
        "record_count": len(records),
        "records": records,
    }


def _write_json(path: Path, payload: Any) -> Path:
    """Canonical JSON: sorted keys, fixed indent, trailing newline.

    All three so that "the same seed gives byte-identical output" is a claim a
    test can actually check.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def generate(
    config: dict[str, Any] | None = None,
    out_root: Path | None = None,
    seed: int | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Build the whole dataset. Returns the manifest."""
    cfg = dict(config if config is not None else load_config("eval_generation"))
    if seed is not None:
        cfg["seed"] = int(seed)
    if only:
        wanted = {name.strip() for name in only.split(",") if name.strip()}
        cfg["cases"] = {
            key: value
            for key, value in (cfg.get("cases") or {}).items()
            if key in wanted
        }
        cfg["issuer_states"] = {
            key: value
            for key, value in (cfg.get("issuer_states") or {}).items()
            if f"issuer_state_{key}" in wanted
        }

    root = Path(
        out_root
        if out_root is not None
        else _repo_root() / str(get(cfg, "output.root", "data/eval"))
    )

    generator = Generator(cfg, root)
    planned = plan_cases(cfg)
    splits = assign_splits(planned, cfg)

    for case in planned:
        built = generator.build(case)
        built.split = splits.get(case.index, "dev")
        generator.built.append(built)

    # Pair links, both ways, so `either_side` can be checked from either end.
    pairs: dict[int, int] = {}
    for built in generator.built:
        if built.source_index is not None:
            pairs[built.index] = built.source_index
            pairs.setdefault(built.source_index, built.index)

    labels = [
        _label_for(
            built,
            recipes.recipe_for(built.case_type),
            (pairs[built.index] + 1) if built.index in pairs else None,
        )
        for built in sorted(generator.built, key=lambda b: b.processing_order)
    ]

    registry_data = build_registry_data(generator.built, generator.world, cfg)

    write_world(generator.world, root / str(get(cfg, "output.world", "world.json")))
    _write_json(
        root / str(get(cfg, "output.issuer_registry", "issuer_registry.json")),
        registry_data,
    )
    _write_json(root / str(get(cfg, "output.labels", "labels.json")), labels)

    counts: dict[str, int] = {}
    for built in generator.built:
        counts[built.case_type] = counts.get(built.case_type, 0) + 1

    manifest = {
        "config_version": str(cfg.get("config_version", "unknown")),
        "seed": int(cfg.get("seed", 0)),
        "generated_case_count": len(generator.built),
        "case_counts": counts,
        "fraud_count": sum(
            1
            for b in generator.built
            if recipes.recipe_for(b.case_type).is_fraud
        ),
        "genuine_count": sum(
            1
            for b in generator.built
            if not recipes.recipe_for(b.case_type).is_fraud
        ),
        "split_counts": {
            "dev": sum(1 for b in generator.built if b.split == "dev"),
            "holdout": sum(1 for b in generator.built if b.split == "holdout"),
        },
        "form_counts": _tally(b.form for b in generator.built),
        "noise_counts": _tally(b.noise.level for b in generator.built),
        "template_counts": _tally(b.template_key for b in generator.built),
        "devanagari_font": generator.devanagari_font,
        # Reported, never silently swallowed: a missing script is a gap in
        # coverage and the reader should be told.
        "devanagari_skipped": list(generator.devanagari_skipped),
        "devanagari_notes": list(generator.devanagari_notes),
        # Named, with the reason. Everything else in the dataset is byte-for-byte
        # reproducible from the seed; these are not, and a reader should be told
        # which ones and why rather than finding out from a failing diff.
        "non_reproducible_documents": sorted(
            b.document_path for b in generator.built if b.form == "signed_pdf"
        ),
        "non_reproducible_reason": (
            "a PDF signature embeds a signing time and a freshly generated "
            "throwaway key, so signed documents differ byte for byte between "
            "runs. Their content, labels and expectations are unchanged."
        ),
        "registry_record_count": registry_data["record_count"],
        "pending_certificate_ids": registry_data["pending_certificate_ids"],
        "applications": [
            {
                "application_id": b.index + 1,
                "processing_order": b.processing_order,
                "case_type": b.case_type,
                "split": b.split,
                "institution": b.institution_name,
                "institution_id": b.institution_id,
                "template": b.template_key,
                "form": b.form,
                "noise_level": b.noise.level,
                "document": b.document_path,
                # Null for signed documents. A PDF signature embeds a signing
                # time and a freshly generated key, so signed bytes cannot be
                # reproduced across runs - and a hash that changed every run
                # would make the manifest non-deterministic for no benefit.
                # `non_reproducible_documents` below names them, so the gap is
                # stated rather than discovered.
                "document_sha256": (
                    None if b.form == "signed_pdf" else b.document_sha256
                ),
                "reproducible_bytes": b.form != "signed_pdf",
                "source_application_id": (
                    b.source_index + 1 if b.source_index is not None else None
                ),
            }
            for b in sorted(generator.built, key=lambda b: b.processing_order)
        ],
    }
    _write_json(root / str(get(cfg, "output.manifest", "manifest.json")), manifest)
    return manifest


def _tally(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


def _repo_root() -> Path:
    # backend/evaluation/generate.py -> repository root
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.generate",
        description=(
            "Build the synthetic evaluation dataset. Deterministic from the "
            "configured seed. Imports no detector: the labels come from the "
            "recipe that built each document."
        ),
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", default=None, help="output root (default data/eval)")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated case types, for a quick partial build",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    manifest = generate(
        out_root=Path(args.out) if args.out else None,
        seed=args.seed,
        only=args.only,
    )

    if not args.quiet:
        print(
            f"generated {manifest['generated_case_count']} applications "
            f"({manifest['fraud_count']} fraud, {manifest['genuine_count']} genuine) "
            f"seed {manifest['seed']}"
        )
        print(
            f"  split: dev {manifest['split_counts']['dev']}, "
            f"holdout {manifest['split_counts']['holdout']}"
        )
        print(f"  forms: {manifest['form_counts']}")
        print(f"  noise: {manifest['noise_counts']}")
        print(f"  templates: {manifest['template_counts']}")
        print(f"  registry records: {manifest['registry_record_count']}")
        for note in manifest["devanagari_skipped"] + manifest["devanagari_notes"]:
            print(f"  NOTE: {note}")
        if not manifest["devanagari_skipped"]:
            print(f"  devanagari font found: {manifest['devanagari_font']}")
        print("\n  case counts:")
        for case_type, count in sorted(manifest["case_counts"].items()):
            print(f"    {count:>3}  {case_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
