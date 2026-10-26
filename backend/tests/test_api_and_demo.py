"""The reviewer API, end to end over the demo set, plus the demo evaluation.

Builds the demo dataset into a temporary directory and a temporary database,
so nothing here touches data/ or the dashboard's database.
"""

from __future__ import annotations

import copy
import json
import socket

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.explain import contains_banned_term

pytestmark = [pytest.mark.qr, pytest.mark.slow]


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    from demo.generate import generate

    out = tmp_path_factory.mktemp("demo")
    generate(out)
    # The faculty detector reads records and websites from config paths.
    return out


@pytest.fixture(scope="module")
def client(demo_dir, tmp_path_factory):
    from app.api.main import create_app
    from app.api.service import Service
    from demo.load import make_pipeline

    db = tmp_path_factory.mktemp("db") / "api.db"
    pipeline = make_pipeline(f"sqlite:///{db}")
    pipeline.configs["faculty_checks"] = copy.deepcopy(pipeline.configs["faculty_checks"])
    pipeline.configs["faculty_checks"]["sources"]["aicte_records_path"] = str(demo_dir / "aicte_records.json")
    pipeline.configs["faculty_checks"]["sources"]["website_snapshot_dir"] = str(demo_dir / "websites")
    api_cfg = copy.deepcopy(load_config("api"))
    api_cfg["demo"]["data_dir"] = str(demo_dir)
    service = Service(pipeline=pipeline, api_config=api_cfg)
    with TestClient(create_app(service)) as test_client:
        for key in ("shantiniketan", "malabar", "vindhya", "chenab"):
            response = test_client.post(f"/api/demo/samples/{key}/submit", headers={"X-Actor": "officer.test", "X-Role": "officer"})
            assert response.status_code == 200, response.text
        yield test_client


PASSCODES = {"senior_reviewer": "senior@2026", "admin": "admin@2026"}


def signed_in(client, role, actor="reviewer.test"):
    response = client.post("/api/session", json={"actor": actor, "role": role, "passcode": PASSCODES[role]})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_queue_orders_by_priority_and_clean_controls_are_low(client):
    rows = client.get("/api/applications").json()
    levels = [r["risk"]["level"] for r in rows]
    assert levels == sorted(levels, key={"high": 0, "medium": 1, "low": 2}.get)
    by_name = {r["institution"]: r["risk"]["level"] for r in rows}
    assert by_name["Shantiniketan Institute of Technology"] == "low"
    assert by_name["Vindhya Institute of Technology"] == "high"


def test_report_carries_risk_explanation_and_evidence(client):
    vindhya = next(r for r in client.get("/api/applications").json() if r["institution"].startswith("Vindhya"))
    report = client.get(f"/api/applications/{vindhya['id']}/report").json()
    types = {f["type"] for d in report["documents"] for f in d["runs"][-1]["findings"]}
    assert {"qr_domain_not_allowlisted", "photo_reused_across_submissions"} <= types
    assert report["explanation"]["disclaimer"]
    assert report["risk"]["contributions"]


def test_no_user_facing_text_uses_a_banned_term(client):
    for row in client.get("/api/applications").json():
        report = client.get(f"/api/applications/{row['id']}/report").json()
        texts = [f["reason"] for d in report["documents"] for f in d["runs"][-1]["findings"]]
        texts.append(report["explanation"]["headline"])
        texts += [p["text"] for p in report["explanation"]["points"]]
        for text in texts:
            assert not contains_banned_term(text), text


def test_review_action_is_recorded_audited_and_rescored(client):
    finding = client.get("/api/findings", params={"status": "open", "strength": "strong"}).json()[0]
    before = len(client.get(f"/api/applications/{finding['application_id']}/report").json()["risk_history"])
    response = client.post(f"/api/findings/{finding['id']}/review", json={"action": "dismiss", "note": "checked"}, headers=signed_in(client, "senior_reviewer", "senior.test"))
    assert response.status_code == 200
    assert response.json()["finding"]["review_status"] == "dismissed"
    report = client.get(f"/api/applications/{finding['application_id']}/report").json()
    assert len(report["risk_history"]) == before + 1
    assert any(e["id"] == finding["id"] for e in report["risk"]["excluded_findings"])
    audit = client.get(f"/api/applications/{finding['application_id']}/audit", headers=signed_in(client, "admin")).json()
    assert any(e["action"] == "review_action" and e["actor"] == "senior.test" for e in audit)


def test_roles_are_enforced(client):
    assert client.get("/api/audit", headers={"X-Role": "officer"}).status_code == 403
    assert client.get("/api/audit", headers=signed_in(client, "admin")).status_code == 200
    assert client.get("/api/meta", headers={"X-Role": "system"}).status_code == 403
    assert client.get("/api/meta", headers={"X-Role": "superuser"}).status_code == 400
    first = client.get("/api/applications").json()[0]["id"]
    assert client.post(f"/api/applications/{first}/rerun", headers={"X-Role": "officer"}).status_code == 403


def test_a_senior_role_cannot_be_claimed_in_a_header(client):
    for role in ("senior_reviewer", "admin"):
        response = client.get("/api/audit", headers={"X-Actor": "someone", "X-Role": role})
        assert response.status_code == 401
        assert "sign-in" in response.json()["detail"]
    # An officer still names themselves; no passcode is involved.
    assert client.get("/api/applications", headers={"X-Role": "officer"}).status_code == 200
    assert client.get("/api/meta").json()["sign_in_required"] == ["admin", "senior_reviewer"]


def test_sign_in_issues_a_token_that_fixes_actor_and_role(client):
    headers = signed_in(client, "senior_reviewer", "senior.verma")
    # A header cannot change who the token says this is.
    meta = client.get("/api/meta", headers={**headers, "X-Actor": "other", "X-Role": "officer"}).json()
    assert meta["identity"] == {"actor": "senior.verma", "role": "senior_reviewer"}
    app_id = client.get("/api/applications").json()[0]["id"]
    assert client.get(f"/api/applications/{app_id}/audit", headers={"X-Role": "officer"}).status_code == 403
    assert client.get(f"/api/applications/{app_id}/audit", headers=headers).status_code == 200


def test_wrong_passcode_and_tampered_token_are_refused_and_audited(client):
    refused = client.post("/api/session", json={"actor": "guess.test", "role": "admin", "passcode": "admin"})
    assert refused.status_code == 401
    assert client.post("/api/session", json={"actor": "x", "role": "officer", "passcode": ""}).status_code == 400
    token = signed_in(client, "senior_reviewer")["Authorization"]
    body, signature = token.removeprefix("Bearer ").split(".")
    import base64

    forged = base64.urlsafe_b64encode(b"x|admin|9999999999").rstrip(b"=").decode()
    assert client.get("/api/audit", headers={"Authorization": f"Bearer {forged}.{signature}"}).status_code == 401
    entries = client.get("/api/audit", params={"action": "sign_in_refused"}, headers=signed_in(client, "admin")).json()["entries"]
    assert any(e["actor"] == "guess.test" and e["details"] == {"reason": "passcode_not_accepted"} for e in entries)
    # The passcode never reaches the audit trail.
    assert all("admin" not in json.dumps(e["details"]) for e in entries)


def test_repeated_refusals_lock_the_role_and_tokens_expire():
    import time

    from app.api.sessions import SessionError, Sessions

    sessions = Sessions(load_config("api"))
    for _ in range(sessions.max_failures):
        assert not sessions.check_passcode("admin", "wrong")
    assert sessions.locked_out("admin")
    assert not sessions.locked_out("senior_reviewer")
    expired = sessions.issue("a", "admin")
    expired.expires_at = int(time.time()) - 1
    with pytest.raises(SessionError):
        sessions.decode(sessions.encode(expired))


def test_report_reads_are_logged_with_the_reader(client):
    app_id = client.get("/api/applications").json()[0]["id"]
    client.get(f"/api/applications/{app_id}/report", headers={"X-Actor": "reader.test", "X-Role": "officer"})
    audit = client.get(f"/api/applications/{app_id}/audit", headers=signed_in(client, "admin")).json()
    assert any(e["action"] == "report_read" and e["actor"] == "reader.test" for e in audit)
    assert client.get("/api/audit/verify", headers=signed_in(client, "admin")).json()["ok"] is True


def test_rerun_adds_runs_and_keeps_the_old_ones(client):
    app_id = client.get("/api/applications").json()[-1]["id"]
    before = client.get(f"/api/applications/{app_id}/report", params={"all_runs": True}).json()
    client.post(f"/api/applications/{app_id}/rerun", headers=signed_in(client, "admin"))
    after = client.get(f"/api/applications/{app_id}/report", params={"all_runs": True}).json()
    for old, new in zip(before["documents"], after["documents"]):
        assert new["run_count"] == old["run_count"] + 1
        assert new["runs"][0]["id"] == old["runs"][0]["id"]
    # Dashboard counts read current findings only, so the old run is not counted twice.
    stats = client.get("/api/stats").json()
    counted = sum(sum(v.values()) for v in stats["open_findings_by_detector"].values())
    assert counted == sum(r["open_findings"] for r in client.get("/api/applications").json())


def test_a_rerun_keeps_the_review_of_an_unchanged_indicator(client):
    vindhya = next(r for r in client.get("/api/applications").json() if r["institution"].startswith("Vindhya"))
    open_findings = client.get("/api/findings", params={"application_id": vindhya["id"], "status": "open"}).json()
    document_finding = next(f for f in open_findings if f["document_id"] is not None)
    officer = {"X-Actor": "officer.carry", "X-Role": "officer"}
    client.post(f"/api/findings/{document_finding['id']}/review", json={"action": "dismiss", "note": "checked"}, headers=officer)
    level_before = client.get(f"/api/applications/{vindhya['id']}/report").json()["risk"]["score"]

    client.post(f"/api/applications/{vindhya['id']}/rerun", headers=signed_in(client, "senior_reviewer"))

    current = client.get("/api/findings", params={"application_id": vindhya["id"]}).json()
    successor = next(f for f in current if f["type"] == document_finding["type"] and f["reason"] == document_finding["reason"])
    assert successor["id"] != document_finding["id"]
    assert successor["review_status"] == "dismissed"
    assert successor["review_actions"][-1]["action"] == "carried_forward"
    assert f"#{document_finding['id']}" in successor["review_actions"][-1]["note"]
    # What was open before is still open. (The re-run may also find new links
    # to applications submitted after this one; those start open.)
    status = {(f["type"], f["reason"]): f["review_status"] for f in current}
    for f in open_findings:
        if f["id"] != document_finding["id"] and (f["type"], f["reason"]) in status:
            assert status[(f["type"], f["reason"])] == "open"
    assert client.get(f"/api/applications/{vindhya['id']}/report").json()["risk"]["score"] >= level_before


def test_upload_rejects_unknown_types_and_extensions(client):
    files = [("files", ("x.exe", b"MZ", "application/octet-stream"))]
    response = client.post("/api/applications", data={"institution_name": "X", "document_types": "lab_photo"}, files=files)
    assert response.status_code == 400
    files = [("files", ("x.jpg", b"0", "image/jpeg"))]
    response = client.post("/api/applications", data={"institution_name": "X", "document_types": "land_deed"}, files=files)
    assert response.status_code == 400


def _clear_provider_keys(monkeypatch):
    """Remove every configured provider's API key from the environment.

    The result does not depend on whether this machine happens to have a key
    in .env: with none present, the field stage reads offline through the same
    snippet guard. conftest loads .env for the live tests, so an offline test
    must clear the keys for itself.
    """
    llm = load_config("certificate_extraction").get("llm", {}) or {}
    for settings in (llm.get("providers", {}) or {}).values():
        env = (settings or {}).get("api_key_env")
        if env:
            monkeypatch.delenv(env, raising=False)


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _block_external_network(monkeypatch):
    """Block every outbound connection except loopback.

    The demo evaluation runs the whole pipeline, and the mock issuer runs
    in-process through Starlette's TestClient - which, on Windows with httpx2,
    opens a real loopback socket. Blocking loopback too would break the
    in-process issuer and prove nothing about external calls. So loopback is
    allowed and everything else raises; the field-extraction fallback path, the
    thing under test, makes no outbound call at all and is unaffected either
    way.
    """
    import socket as socket_module

    real_connect = socket_module.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        if host not in _LOOPBACK:
            raise AssertionError(f"external network access attempted to {address}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket_module.socket, "connect", guarded_connect)


def _demo_pipeline_factory(loader, demo_dir):
    original = loader.make_pipeline

    # Mirror make_pipeline's real signature (url + database), so evaluate() may
    # hand it the temporary-database Database it now owns.
    def pipeline_with_demo_sources(url=None, database=None):
        pipeline = original(url, database=database)
        pipeline.configs["faculty_checks"] = copy.deepcopy(pipeline.configs["faculty_checks"])
        pipeline.configs["faculty_checks"]["sources"]["aicte_records_path"] = str(demo_dir / "aicte_records.json")
        pipeline.configs["faculty_checks"]["sources"]["website_snapshot_dir"] = str(demo_dir / "websites")
        return pipeline

    return pipeline_with_demo_sources


def test_demo_evaluation_detects_every_injected_type_offline_with_no_key(demo_dir, monkeypatch):
    """Offline, no provider key: the field stage reads offline and every
    injected type is still detected.

    The environment is controlled explicitly - all provider keys are cleared -
    so the result is the same whether or not this machine has a key in .env.
    """
    from demo import evaluate as evaluation
    from demo import load as loader

    _clear_provider_keys(monkeypatch)
    _block_external_network(monkeypatch)
    monkeypatch.setattr(evaluation, "make_pipeline", _demo_pipeline_factory(loader, demo_dir))
    report = evaluation.evaluate(demo_dir)
    assert report["overall_detection_rate"] == 1.0, json.dumps(report["per_fraud_type"], indent=1)
    assert report["false_positives"]["clean_with_medium_or_strong_finding"] == 0
    assert report["false_positives"]["clean_not_low_priority"] == 0
    assert report["honest_flags_expected"]["raised"] == report["honest_flags_expected"]["count"]
    # Offline by construction: no key present, so every certificate was read by
    # the offline reader.
    assert report["field_extraction"]["documents_via_offline_reader"] >= 1


def test_demo_evaluation_with_a_fake_key_and_no_network_uses_the_fallback(demo_dir, monkeypatch):
    """A key is present but the network is down - the Windows demo scenario.

    This is the case that used to drop to 8/11: with a key set, the primary was
    attempted, the socket trap turned the call into a transport failure, and
    before the fix that hard-failed the field stage. Now the offline reader
    stands in and detection is back to 11/11 with no false positives.
    """
    from app.llm.breaker import default_breaker
    from demo import evaluate as evaluation
    from demo import load as loader

    # A made-up key, never a real one. It should never appear in any stored
    # output; the point is only that a key being *present* no longer breaks the
    # offline path.
    fake = "sk-fake-eval-key-DO-NOT-USE-0000000000"
    monkeypatch.setenv("GROQ_API_KEY", fake)
    # A fresh breaker so state from another test cannot pre-open or mask it.
    monkeypatch.setattr("app.certificates.extractor.default_breaker", lambda: _FreshBreaker())
    _block_external_network(monkeypatch)
    monkeypatch.setattr(evaluation, "make_pipeline", _demo_pipeline_factory(loader, demo_dir))
    report = evaluation.evaluate(demo_dir)

    assert report["overall_detection_rate"] == 1.0, json.dumps(report["per_fraud_type"], indent=1)
    assert report["false_positives"]["clean_with_medium_or_strong_finding"] == 0
    assert report["false_positives"]["clean_not_low_priority"] == 0
    # The key was present but the provider was unreachable, so the offline
    # reader produced every certificate's fields.
    assert report["field_extraction"]["documents_via_offline_reader"] >= 1
    # The fake key must not have leaked anywhere into the report.
    assert fake not in json.dumps(report)


def _FreshBreaker():
    from app.llm.breaker import CircuitBreaker

    return CircuitBreaker(threshold=2, cool_down_seconds=60)


# --------------------------------------------------------------------------
# Streaming, connections, search, scrutiny note
# --------------------------------------------------------------------------


def test_streamed_submission_reports_every_stage_then_the_priority(client):
    events = []
    with client.stream("POST", "/api/demo/samples/deccan/stream", headers={"X-Role": "officer"}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            events.append(json.loads(line))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "application" and kinds[-1] == "done"
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    assert stages[:6] == ["extraction", "certificate_fields", "qr_checks", "issuer_verification", "signature_check", "duplicate_check"]
    assert "faculty_consistency" in stages and "photo_reuse" in stages
    assert events[-1]["risk"]["level"] == "high"


def test_streaming_requires_the_submit_permission(client):
    # Every assertable role may submit in the shipped config; an unknown one is refused.
    response = client.post("/api/demo/samples/deccan/stream", headers={"X-Role": "visitor"})
    assert response.status_code == 400


def test_connections_link_the_institutions_that_share_evidence(client):
    network = client.get("/api/network").json()
    labels = {n["id"]: n["label"] for n in network["nodes"]}
    pairs = {frozenset((labels[e["source"]], labels[e["target"]])): {k["kind"] for k in e["kinds"]} for e in network["edges"]}
    assert "certificate_number_reuse" in pairs[frozenset(("Malabar Institute of Technology", "Chenab Valley College of Technology"))]
    assert "photo_reuse" in pairs[frozenset(("Shantiniketan Institute of Technology", "Vindhya Institute of Technology"))]
    # Template siblings with different numbers are not drawn.
    assert all("near_duplicate_page_image" not in kinds or len(kinds) > 1 for kinds in pairs.values())


def test_search_finds_a_certificate_number_wherever_it_appears(client):
    results = client.get("/api/search", params={"q": "2024/00622"}).json()["results"]
    institutions = {r["institution"] for r in results["certificates"]}
    assert {"Malabar Institute of Technology", "Chenab Valley College of Technology"} <= institutions


def test_search_is_audited_without_the_query_text(client):
    client.get("/api/search", params={"q": "Malabar"}, headers={"X-Actor": "searcher.test"})
    entries = client.get("/api/audit", params={"action": "search_performed"}, headers=signed_in(client, "admin")).json()["entries"]
    mine = [e for e in entries if e["actor"] == "searcher.test"]
    assert mine and "Malabar" not in json.dumps(mine[0]["details"])


def test_scrutiny_note_is_a_pdf_with_the_disclaimer_and_is_audited(client):
    import fitz

    app_id = client.get("/api/applications").json()[0]["id"]
    response = client.get(f"/api/applications/{app_id}/export.pdf", headers={"X-Actor": "exporter.test"})
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    text = "".join(page.get_text() for page in fitz.open("pdf", response.content))
    assert "Decision support only" in text and "not findings of fact" in text
    assert not contains_banned_term(text)
    audit = client.get(f"/api/applications/{app_id}/audit", headers=signed_in(client, "admin")).json()
    assert any(e["action"] == "scrutiny_note_exported" and e["actor"] == "exporter.test" for e in audit)


# --------------------------------------------------------------------------
# Application validation and regulatory compliance
# --------------------------------------------------------------------------


def _submit(client, key):
    response = client.post(f"/api/demo/samples/{key}/submit", headers={"X-Role": "officer"})
    assert response.status_code == 200, response.text
    return response.json()["application_id"]


def test_a_genuine_certificate_of_another_institution_is_caught(client):
    app_id = _submit(client, "saraswati")
    report = client.get(f"/api/applications/{app_id}/report").json()
    check = report["application_check"]
    types = {f["type"]: f["strength"] for f in check["findings"]}
    assert types["certificate_names_other_institution"] == "strong"
    assert types["application_document_missing"] == "medium"
    missing = next(f for f in check["findings"] if f["type"] == "application_document_missing")["evidence"]["missing"]
    assert {m["label"] for m in missing} == {"Faculty list", "Laboratory photograph"}
    # The issuer still verifies the certificate: only the applicant comparison sees it.
    lookups = report["documents"][0]["runs"][-1]["verification_lookups"]
    assert lookups and lookups[0]["status"] == "verified"


def test_inflation_that_meets_a_norm_only_on_paper_is_reported(client):
    app_id = _submit(client, "gomati")
    check = client.get(f"/api/applications/{app_id}/report").json()["application_check"]
    finding = next(f for f in check["findings"] if f["type"] == "compliance_met_only_as_declared")
    assert finding["evidence"]["declared_meets"] is True and finding["evidence"]["recorded_meets"] is False
    assert "mock" in finding["reason"]


def test_clean_applications_meet_every_configured_norm(client):
    shantiniketan = next(r for r in client.get("/api/applications").json() if r["institution"].startswith("Shantiniketan"))
    check = client.get(f"/api/applications/{shantiniketan['id']}/report").json()["application_check"]
    assert check["findings"] == []
    assert {c["outcome"] for c in check["checks"]} == {"met"}


def test_application_level_findings_can_be_reviewed_and_rescore(client):
    app_id = next(r["id"] for r in client.get("/api/applications").json() if r["institution"].startswith("Saraswati"))
    finding = next(f for f in client.get("/api/findings", params={"application_id": app_id}).json() if f["type"] == "certificate_names_other_institution")
    assert finding["original_filename"] == "whole application"
    response = client.post(f"/api/findings/{finding['id']}/review", json={"action": "confirm", "note": "checked with issuer"}, headers=signed_in(client, "senior_reviewer"))
    assert response.status_code == 200
    assert response.json()["finding"]["review_status"] == "confirmed"
