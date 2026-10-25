"""The scrutiny note: one application's indicators, as a PDF for the file.

A record of what the automated checks surfaced and what reviewers have done
about it so far. It states no conclusion: the header and footer say so on every
page, and the wording is the findings' own (already held to the language rules).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from html import escape
from typing import Any

_STRENGTH_COLOUR = {"strong": "#b42318", "medium": "#c4561f", "weak": "#64748b"}
_LEVEL_COLOUR = {"high": "#b42318", "medium": "#c4561f", "low": "#067647"}

_CSS = """
* { font-family: sans-serif; }
body { font-size: 9.5pt; color: #0f172a; }
h1 { font-size: 16pt; margin: 0 0 2pt 0; color: #13294b; }
h2 { font-size: 11.5pt; margin: 14pt 0 5pt 0; color: #13294b; border-bottom: 1px solid #cbd5e1; padding-bottom: 2pt; }
p { margin: 0 0 4pt 0; }
.muted { color: #64748b; font-size: 8.5pt; }
.box { border: 1px solid #cbd5e1; background-color: #f8fafc; padding: 6pt; margin: 6pt 0; }
.warn { border: 1px solid #f5c26b; background-color: #fff8eb; padding: 6pt; margin: 6pt 0; font-size: 8.5pt; }
table { border-collapse: collapse; width: 100%; margin-top: 3pt; }
th { text-align: left; font-size: 8pt; color: #475569; border-bottom: 1px solid #94a3b8; padding: 3pt; }
td { font-size: 8.5pt; border-bottom: 1px solid #e2e8f0; padding: 3pt; vertical-align: top; }
.tag { font-weight: bold; font-size: 8pt; }
"""


def _h(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return escape(", ".join(str(v) for v in value))
    return escape(str(value))


def _human(value: str) -> str:
    return escape(str(value).replace("_", " "))


def build_scrutiny_note(
    report: dict[str, Any],
    findings: list[dict[str, Any]],
    risk: dict[str, Any],
    explanation: dict[str, Any],
    identity: Any,
    chain_ok: bool,
) -> bytes:
    import fitz

    application = report["application"]
    institution = report["institution"]["name"]
    level = risk["level"]
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    parts: list[str] = []
    parts.append(f"<h1>Scrutiny note &#8212; APP-{application['id']:04d}</h1>")
    parts.append(f"<p><b>{escape(institution)}</b> &#183; {_human(application['category'])}</p>")
    parts.append(f"<p class='muted'>Generated {generated} for {escape(identity.actor)} ({_human(identity.role)}) &#183; risk rules {escape(str(risk.get('config_version')))} &#183; audit chain {'intact' if chain_ok else 'NOT intact'}</p>")
    parts.append(
        "<div class='warn'><b>Decision support only.</b> These are preliminary indicators "
        "with evidence for an official to verify. They are not findings of fact, and the "
        "review priority below only orders the review queue. All data is synthetic.</div>"
    )

    parts.append("<h2>Review priority</h2>")
    parts.append(
        f"<p><span class='tag' style='color:{_LEVEL_COLOUR[level]}'>{level.upper()} PRIORITY</span> &#160; "
        f"score {risk['score']:g} of 100</p>"
    )
    rows = "".join(
        f"<tr><td>{escape(sub['label'])}</td><td>{sub['score']:g}</td><td>{sub['strong']}</td><td>{sub['medium']}</td><td>{sub['weak']}</td></tr>"
        for sub in risk["subscores"].values()
    )
    parts.append(f"<table><tr><th>Detector</th><th>Sub-score</th><th>Strong</th><th>Medium</th><th>Weak</th></tr>{rows}</table>")
    parts.append(f"<div class='box'>{escape(explanation['headline'])}")
    for note in explanation.get("notes", []):
        parts.append(f"<p class='muted'>{escape(note)}</p>")
    parts.append("</div>")

    parts.append("<h2>Documents</h2>")
    doc_rows = []
    for document in report["documents"]:
        run = (document.get("runs") or [{}])[-1]
        status = None
        for stage in run.get("stages", []) or []:
            if stage["stage"] in ("issuer_verification", "faculty_consistency"):
                status = stage.get("detail_status")
        if status is None:
            status = ((run.get("details") or {}).get("photo_analysis") or {}).get("verification_status")
        doc_rows.append(
            f"<tr><td>#{document['id']}</td><td>{_human(document['document_type'])}</td><td>{escape(document['original_filename'])}</td>"
            f"<td>{_human(status) if status else 'no status claimed'}</td><td>{len(run.get('findings', []))}</td>"
            f"<td style='font-family:monospace;font-size:7pt'>{document['file_sha256'][:16]}&#8230;</td></tr>"
        )
    parts.append(f"<table><tr><th>ID</th><th>Type</th><th>File</th><th>Verification status</th><th>Indicators</th><th>SHA-256</th></tr>{''.join(doc_rows)}</table>")

    parts.append("<h2>Indicators</h2>")
    order = {"strong": 0, "medium": 1, "weak": 2}
    finding_rows = []
    for finding in sorted(findings, key=lambda f: (order.get(f["strength"], 3), f["id"])):
        colour = _STRENGTH_COLOUR.get(finding["strength"], "#64748b")
        finding_rows.append(
            f"<tr><td><span class='tag' style='color:{colour}'>{finding['strength'].upper()}</span></td>"
            f"<td><b>{_human(finding['type'])}</b><br/>{escape(finding['reason'])}</td>"
            f"<td>{_human(finding['detector'])}</td><td>{_h(finding.get('confidence'))}</td>"
            f"<td>{_human(finding['review_status'])}</td></tr>"
        )
    if finding_rows:
        parts.append(f"<table><tr><th>Strength</th><th>Indicator and reason</th><th>Detector</th><th>Confidence</th><th>Review</th></tr>{''.join(finding_rows)}</table>")
    else:
        parts.append("<p>No indicators were raised. That is not a finding that the documents are sound.</p>")

    all_findings = [f for d in report["documents"] for f in ((d.get("runs") or [{}])[-1].get("findings") or [])]
    all_findings += (report.get("application_check") or {}).get("findings") or []
    actions = [(f, a) for f in all_findings for a in f.get("review_actions", [])]
    parts.append("<h2>Reviewer actions</h2>")
    if actions:
        rows = "".join(
            f"<tr><td>{escape(a['created_at'][:16].replace('T', ' '))}</td><td>{escape(a['actor'])} ({_human(a['role'])})</td>"
            f"<td>{_human(a['action'])}</td><td>{_human(f['type'])}</td><td>{_h(a.get('note'))}</td></tr>"
            for f, a in actions
        )
        parts.append(f"<table><tr><th>When</th><th>Who</th><th>Action</th><th>Indicator</th><th>Note</th></tr>{rows}</table>")
    else:
        parts.append("<p class='muted'>No reviewer has acted on these indicators yet.</p>")

    parts.append(
        f"<p class='muted' style='margin-top:14pt'>{escape(explanation.get('disclaimer', ''))} "
        "Signer identity of any digital signature is not verified. URLs in QR codes are never contacted.</p>"
    )

    html = "<body>" + "".join(parts) + "</body>"
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html=html, user_css=_CSS)
    mediabox = fitz.paper_rect("a4")
    content = mediabox + (48, 56, -48, -56)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(content)
        story.draw(device)
        writer.end_page()
    writer.close()

    # Running header and footer on every page.
    document = fitz.open("pdf", buffer.getvalue())
    for number, page in enumerate(document, start=1):
        page.insert_text((48, 34), f"Authentiq scrutiny note · APP-{application['id']:04d} · {institution}", fontsize=7.5, color=(0.39, 0.45, 0.55))
        page.insert_text(
            (48, page.rect.height - 28),
            f"Preliminary indicators for review, not findings of fact · synthetic demo data · page {number} of {document.page_count}",
            fontsize=7.5, color=(0.39, 0.45, 0.55),
        )
    document.set_metadata({"title": f"Scrutiny note APP-{application['id']:04d}", "creator": "Authentiq (SIH 2026 prototype)"})
    data = document.tobytes(deflate=True)
    document.close()
    return data
