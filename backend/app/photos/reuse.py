"""Has this photo been submitted before? The most reliable photo signal.

Internal reverse image search: the corpus is the photos already stored in our
own database. No external image API is called (a confirmed decision), and
nothing here touches the network.

Each photo's perceptual hashes go into `perceptual_hashes` with page 0 - a
photograph has no pages, and page 0 keeps photos out of the certificate
near-duplicate comparison, which reads page 1. The comparison also tries the
mirrored image, because flipping is the cheapest disguise.

Relationships are written to `cross_references` as normalised pairs, exactly
like the document duplicates, so the reuse shows on both submissions without
either finished run being rewritten.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get, load_config
from ..db.models import Application, CrossReference, Document, Institution, PerceptualHashRow
from ..similarity import hashing
from .checker import CONFIG_NAME, LoadedPhoto, _finding, upright

REUSE_KIND = "photo_reuse"


def check_reuse(
    session: Session,
    document_id: int,
    run_id: int,
    photo: LoadedPhoto,
    cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from PIL import ImageOps

    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    findings: list[dict[str, Any]] = []
    if not bool(get(cfg, "reuse.enabled", True)):
        return {"disabled": True}, findings

    kinds = [str(k) for k in (get(cfg, "reuse.kinds", ["phash"]) or [])]
    compare_kind = str(get(cfg, "reuse.compare_kind", "phash"))
    size = int(get(cfg, "reuse.hash_size", 16))
    threshold = int(get(cfg, "reuse.near_duplicate_max_distance", 24))
    page = int(get(cfg, "reuse.page_marker", 0))
    photo_types = [str(t) for t in (get(cfg, "document_types", []) or [])]

    image = upright(photo.image)
    computed = hashing.compute_all(image, kinds, hash_size=size)
    for entry in computed:
        session.add(
            PerceptualHashRow(
                document_id=document_id,
                run_id=run_id,
                page=page,
                hash_kind=entry.kind,
                hash_value=entry.hex_value,
                hash_size=entry.hash_size,
            )
        )
    session.flush()

    mine = next((h for h in computed if h.kind == compare_kind), None)
    mirrored = (
        hashing.compute(ImageOps.mirror(image), compare_kind, hash_size=size)
        if bool(get(cfg, "reuse.check_mirrored", True)) and mine is not None
        else None
    )

    me = session.get(Document, document_id)
    my_app = session.get(Application, me.application_id) if me else None

    rows = session.execute(
        select(PerceptualHashRow, Document, Application, Institution)
        .join(Document, PerceptualHashRow.document_id == Document.id)
        .join(Application, Document.application_id == Application.id)
        .join(Institution, Application.institution_id == Institution.id)
        .where(
            PerceptualHashRow.hash_kind == compare_kind,
            PerceptualHashRow.page == page,
            PerceptualHashRow.hash_size == size,
            PerceptualHashRow.document_id != document_id,
            Document.document_type.in_(photo_types),
        )
        .order_by(PerceptualHashRow.document_id, PerceptualHashRow.id.desc())
    ).all()

    detail: dict[str, Any] = {
        "hashes": [h.to_dict() for h in computed],
        "compare_kind": compare_kind,
        "threshold": threshold,
        "corpus_photos_compared": 0,
        "matches": [],
    }
    if mine is None:
        detail["skipped"] = f"compare kind {compare_kind!r} not among computed kinds"
        return detail, findings

    seen: set[int] = set()
    for hash_row, other_doc, other_app, other_inst in rows:
        if other_doc.id in seen:
            continue
        seen.add(other_doc.id)
        distance = hashing.hamming_distance(mine.hex_value, hash_row.hash_value)
        mirror_distance = (
            hashing.hamming_distance(mirrored.hex_value, hash_row.hash_value) if mirrored else None
        )
        best = min(distance, mirror_distance) if mirror_distance is not None else distance
        identical = me is not None and other_doc.file_sha256 == me.file_sha256
        if identical:
            best = 0
        if best > threshold:
            continue
        same_institution = my_app is not None and other_app.institution_id == my_app.institution_id
        detail["matches"].append(
            {
                "other_document_id": other_doc.id,
                "other_application_id": other_app.id,
                "other_institution": other_inst.name,
                "same_institution": same_institution,
                "same_application": my_app is not None and other_app.id == my_app.id,
                "hamming_distance": best,
                "direct_distance": distance,
                "mirrored_distance": mirror_distance,
                "mirrored": mirror_distance is not None and mirror_distance < distance,
                "identical_file": identical,
                "other_filename": other_doc.original_filename,
                "other_claim": other_doc.claim,
            }
        )
    detail["corpus_photos_compared"] = len(seen)

    strength_cfg = get(cfg, "reuse.strength", {}) or {}
    for match in detail["matches"][:10]:
        context = "same_institution" if match["same_institution"] else "other_institution"
        strength = str(strength_cfg.get(context, "medium"))
        how = (
            "an identical file"
            if match["identical_file"]
            else ("a mirrored copy" if match["mirrored"] else "a near-identical copy")
        )
        where = (
            "earlier in a submission from the same institution"
            if match["same_institution"]
            else f"in a submission from another institution ({match['other_institution']})"
        )
        findings.append(
            _finding(
                "photo_reused_across_submissions",
                strength,
                f"This photo matches {how} of a photo already submitted {where} "
                f"(perceptual-hash distance {match['hamming_distance']} of "
                f"{mine.bit_count} bits; threshold {threshold}). The same image "
                f"presented as two facilities requires manual verification.",
                {
                    **match,
                    "threshold": threshold,
                    "hash_kind": compare_kind,
                    "hash_bits": mine.bit_count,
                    "this_claim": me.claim if me else None,
                },
                confidence="high" if match["hamming_distance"] <= threshold // 3 else "medium",
            )
        )
        _record_pair(session, document_id, match, run_id)
    session.flush()
    return detail, findings


def _record_pair(session: Session, document_id: int, match: dict[str, Any], run_id: int) -> None:
    low, high = sorted((document_id, int(match["other_document_id"])))
    existing = session.scalars(
        select(CrossReference).where(
            CrossReference.document_a_id == low,
            CrossReference.document_b_id == high,
            CrossReference.kind == REUSE_KIND,
        )
    ).first()
    if existing is not None:
        return
    session.add(
        CrossReference(
            document_a_id=low,
            document_b_id=high,
            kind=REUSE_KIND,
            same_institution=bool(match["same_institution"]),
            evidence={
                "hamming_distance": match["hamming_distance"],
                "mirrored": match["mirrored"],
                "identical_file": match["identical_file"],
            },
            discovered_in_run_id=run_id,
        )
    )
