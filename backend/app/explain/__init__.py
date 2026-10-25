"""Plain-language explanations of an application's findings.

The explanation describes; it never concludes. A deterministic template always
produces one from the risk breakdown and the findings' own reasons. An LLM may
rewrite it for readability when config enables that, and its output is then
held to two checks before it is shown:

1. **No banned term** - "fake", "forged", "fraudulent", "deepfake" and the rest
   of the list in `config/explanations.yaml`.
2. **No new numbers** - every number in the rewrite must already appear in the
   template. A rewrite that invents a count or a distance is discarded.

Either failure, or any provider error, falls back to the template. The model
explains; it never adds a fact.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.config import get, load_config

CONFIG_NAME = "explanations"

_DETECTOR_NOUN = {
    "certificate_authenticity": "certificate",
    "duplicate_detection": "cross-application duplicate",
    "faculty_consistency": "faculty",
    "photo_authenticity": "photo",
    "application_validation": "application",
    "regulatory_compliance": "compliance",
}


def contains_banned_term(text: str, cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    terms = [str(t).lower() for t in (get(cfg, "language.banned_terms", []) or [])]
    lowered = text.lower()
    return [t for t in terms if re.search(rf"\b{re.escape(t)}", lowered)]


def template_explanation(
    risk: dict[str, Any],
    findings: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    level = str(risk.get("level", "low"))
    counted = [c for c in risk.get("contributions", []) if c.get("points", 0) > 0]
    by_id = {f.get("id"): f for f in findings}

    strong = [c for c in counted if c["strength"] == "strong"]
    medium = [c for c in counted if c["strength"] == "medium"]
    weak = [c for c in counted if c["strength"] == "weak"]

    if not counted:
        headline = (
            "No open indicators were raised by the automated checks. This does "
            "not establish that the documents are sound; it means nothing was "
            "flagged for priority review."
        )
    else:
        parts = []
        if strong:
            parts.append(f"{len(strong)} strong")
        if medium:
            parts.append(f"{len(medium)} medium")
        if weak:
            parts.append(f"{len(weak)} weak")
        headline = (
            f"Placed at {level.upper()} review priority on {', '.join(parts)} "
            f"indicator(s) (score {round(risk.get('score', 0))} of 100). Each "
            f"requires manual verification against its evidence."
        )

    # A weak page-similarity match fires against most certificates sharing an
    # issuer's template (measured in step 6); listed individually it buries the
    # decisive points, so it is summarised in one note instead.
    template_matches = [
        c for c in counted if c["type"] == "near_duplicate_page_image" and c["strength"] == "weak"
    ]
    listed = [c for c in counted if c not in template_matches]

    points: list[dict[str, Any]] = []
    for contribution in listed[:8]:
        finding = by_id.get(contribution.get("finding_id"), {})
        noun = _DETECTOR_NOUN.get(str(contribution.get("detector")), str(contribution.get("detector")))
        points.append(
            {
                "finding_id": contribution.get("finding_id"),
                "detector": contribution.get("detector"),
                "strength": contribution["strength"],
                "text": f"[{contribution['strength']}, {noun}] {finding.get('reason') or contribution['type']}",
            }
        )

    floors = [
        f"Level raised by rule '{f['rule']}' from {f['from']} to {f['to']}."
        for f in risk.get("floors_applied", []) or []
    ]
    excluded = risk.get("excluded_findings") or []
    if template_matches:
        floors.append(
            f"{len(template_matches)} weak page-similarity match(es) with other "
            f"certificates on the same issuer template are not listed above; "
            f"the certificate-number checks are what decide."
        )
    notes = floors + (
        [f"{len(excluded)} finding(s) dismissed by a reviewer are not counted."] if excluded else []
    )

    return {
        "headline": headline,
        "points": points,
        "notes": notes,
        "disclaimer": str(get(cfg, "language.disclaimer", "")).strip(),
        "generated_by": "template",
        "config_version": cfg.get("config_version"),
    }


def explain(
    risk: dict[str, Any],
    findings: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """The template, optionally rewritten by an LLM that passes both guards."""
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    base = template_explanation(risk, findings, cfg)
    if not bool(get(cfg, "llm.enabled", False)) and provider is None:
        return base

    template_text = base["headline"] + "\n" + "\n".join(p["text"] for p in base["points"])
    prompt = (
        "Rewrite the following review summary in plain, neutral English for a "
        "government official. Keep every fact, add none, keep every number "
        "exactly, and never say or imply that anything is fake, forged or "
        "fraudulent. Return only the rewritten text.\n\n" + template_text
    )
    try:
        if provider is None:
            from ..llm import get_provider

            ext = load_config("certificate_extraction")
            name = str(get(cfg, "llm.provider", "groq"))
            provider = get_provider(name, dict(get(ext, f"llm.providers.{name}", {}) or {}))
        text = provider.complete(prompt).text.strip()
    except Exception as exc:
        base["llm_rejected"] = f"provider error: {type(exc).__name__}"
        return base

    banned = contains_banned_term(text, cfg)
    allowed_numbers = set(re.findall(r"\d+(?:\.\d+)?", template_text))
    new_numbers = sorted(set(re.findall(r"\d+(?:\.\d+)?", text)) - allowed_numbers)
    if banned or new_numbers or not text:
        base["llm_rejected"] = (
            f"banned terms: {banned}" if banned else f"numbers not in the source: {new_numbers}" if new_numbers else "empty"
        )
        return base

    rewritten = dict(base)
    rewritten["rewrite"] = text
    rewritten["generated_by"] = f"llm:{getattr(provider, 'name', 'unknown')}"
    return rewritten
