"""Tests for step 7: the evaluation generator, runner and metrics.

Most of these are fast. The two that are not - a full-dataset generation and a
full oracle run - carry `slow`, because the full generation renders eighty
documents and the full run OCRs sixteen of them.

Nothing here touches the network, and nothing writes a key to disk.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from app.core.config import load_config
from evaluation import distances, metrics, recipes, report
from evaluation.generate import assign_splits, generate, plan_cases
from evaluation.world import build_world


@pytest.fixture
def generation_config() -> dict:
    return copy.deepcopy(load_config("eval_generation"))


@pytest.fixture
def small_config(generation_config) -> dict:
    """A handful of cases, enough to exercise every code path cheaply."""
    config = copy.deepcopy(generation_config)
    config["cases"] = {
        "clean": 3,
        "number_edited_qr_untouched": 1,
        "qr_lookalike_domain": 1,
        "unknown_certificate": 1,
        "signed_then_tampered": 1,
        "duplicate_file_other_institution": 1,
    }
    config["issuer_states"] = {"revoked": 1}
    return config


# --------------------------------------------------------------------------
# The independence rule
# --------------------------------------------------------------------------


def test_the_generator_imports_no_detector_code():
    """The rule the whole measurement rests on.

    If the generator could ask a detector what it had built, the labels would
    be the detectors' own opinion and every number would be circular. Only the
    runner may touch `app`, and only to drive the pipeline and read results
    back.
    """
    package = Path(__file__).resolve().parent.parent / "evaluation"
    forbidden = (
        "app.qr",
        "app.issuer",
        "app.signatures",
        "app.duplicates",
        "app.certificates",
        "app.extraction",
        "app.similarity",
        "app.mock_issuer",
    )
    # The runner drives the pipeline and reads the database back; that is its
    # job. Everything else in the package must stay clear of the detectors.
    allowed_to_touch_app = {"run.py", "distances.py"}

    offences: list[str] = []
    for path in sorted(package.glob("*.py")):
        if path.name in allowed_to_touch_app:
            continue
        text = path.read_text(encoding="utf-8")
        for module in forbidden:
            if re.search(rf"^\s*(from|import)\s+{re.escape(module)}\b", text, re.M):
                offences.append(f"{path.name} imports {module}")
    assert offences == [], offences


def test_only_the_runner_and_distances_touch_the_pipeline():
    package = Path(__file__).resolve().parent.parent / "evaluation"
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "app.pipeline" in text:
            assert path.name == "run.py", (
                f"{path.name} reaches into the pipeline; only the runner may"
            )


def test_labels_come_from_the_recipe_not_from_a_run(small_config, tmp_path):
    """Every fraud case carries its expectation before anything is executed."""
    generate(config=small_config, out_root=tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))

    for entry in labels:
        recipe = recipes.recipe_for(entry["case_type"])
        assert entry["is_fraud"] == recipe.is_fraud
        if recipe.is_fraud:
            assert entry["expected"], entry["case_type"]
        # The printed fields are the generator's own record of what it drew.
        assert entry["printed_fields"]["certificate_number"]


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_same_seed_gives_a_byte_identical_manifest_and_labels(
    small_config, tmp_path
):
    """An evaluation whose dataset reshuffles cannot be compared with last
    week's, and a detection rate you cannot compare is not a measurement.

    Signed documents are the one unreproducible part - a signature embeds a
    signing time and a fresh throwaway key - so the manifest records no hash
    for them and names them in `non_reproducible_documents`. The manifest
    itself is therefore byte-identical, and the gap is stated rather than
    hidden.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    generate(config=small_config, out_root=first)
    generate(config=small_config, out_root=second)

    for name in ("manifest.json", "labels.json", "world.json", "issuer_registry.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name

    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["non_reproducible_documents"]
    for entry in manifest["applications"]:
        if entry["form"] == "signed_pdf":
            assert entry["document_sha256"] is None
            assert entry["reproducible_bytes"] is False
        else:
            assert entry["document_sha256"]
            assert entry["reproducible_bytes"] is True


def test_a_different_seed_gives_a_different_dataset(small_config, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    generate(config=small_config, out_root=first, seed=1)
    generate(config=small_config, out_root=second, seed=2)
    assert (first / "labels.json").read_bytes() != (
        second / "labels.json"
    ).read_bytes()


def test_documents_are_byte_identical_across_runs(small_config, tmp_path):
    """The documents too, not just the metadata.

    Signed PDFs are the exception and are excluded: a signature embeds a
    signing time and a fresh key, so two runs cannot produce the same bytes.
    The manifest records each document's hash, so a changed *unsigned*
    document would still be caught here.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    manifest_a = generate(config=small_config, out_root=first)
    generate(config=small_config, out_root=second)

    for entry in manifest_a["applications"]:
        if entry["form"] == "signed_pdf":
            continue
        left = (first / entry["document"]).read_bytes()
        right = (second / entry["document"]).read_bytes()
        assert left == right, entry["document"]


# --------------------------------------------------------------------------
# Manifest consistency
# --------------------------------------------------------------------------


def test_every_manifest_document_exists(small_config, tmp_path):
    manifest = generate(config=small_config, out_root=tmp_path)
    for entry in manifest["applications"]:
        path = tmp_path / entry["document"]
        assert path.exists(), entry["document"]
        assert path.stat().st_size > 0


def test_case_counts_match_config(small_config, tmp_path):
    manifest = generate(config=small_config, out_root=tmp_path)
    expected = dict(small_config["cases"])
    for key, count in small_config["issuer_states"].items():
        expected[f"issuer_state_{key}"] = count
    assert manifest["case_counts"] == {
        key: value for key, value in expected.items() if value > 0
    }


def test_every_duplicate_case_has_an_earlier_source(small_config, tmp_path):
    """A duplicate only exists relative to a corpus, so its source must have
    been processed first."""
    manifest = generate(config=small_config, out_root=tmp_path)
    by_id = {
        entry["application_id"]: entry for entry in manifest["applications"]
    }
    found = 0
    for entry in manifest["applications"]:
        source_id = entry.get("source_application_id")
        if source_id is None:
            continue
        found += 1
        source = by_id[source_id]
        assert source["processing_order"] < entry["processing_order"], entry
    assert found, "the small set should contain at least one duplicate case"


def test_every_fraud_case_has_at_least_one_expected_group(small_config, tmp_path):
    generate(config=small_config, out_root=tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    for entry in labels:
        if entry["is_fraud"]:
            assert entry["expected"], entry["application_id"]
            for group in entry["expected"]:
                assert group["any_of"]
                assert group["min_strength"] in ("weak", "medium", "strong")


def test_every_hard_negative_has_a_must_not_list():
    for case_type in recipes.hard_negative_case_types():
        assert recipes.recipe_for(case_type).must_not, case_type


def test_the_registry_holds_a_record_for_every_genuine_certificate(
    small_config, tmp_path
):
    generate(config=small_config, out_root=tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    registry = json.loads(
        (tmp_path / "issuer_registry.json").read_text(encoding="utf-8")
    )
    known = {record["certificate_id"] for record in registry["records"]}

    for entry in labels:
        if entry["case_type"] != "clean":
            continue
        assert entry["printed_fields"]["certificate_number"] in known, entry


def test_the_unknown_certificate_case_is_absent_from_the_registry(
    small_config, tmp_path
):
    generate(config=small_config, out_root=tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    registry = json.loads(
        (tmp_path / "issuer_registry.json").read_text(encoding="utf-8")
    )
    known = {record["certificate_id"] for record in registry["records"]}

    unknown = [e for e in labels if e["case_type"] == "unknown_certificate"]
    assert unknown
    for entry in unknown:
        assert entry["printed_fields"]["certificate_number"] not in known


# --------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------


def test_the_split_is_deterministic_and_disjoint(generation_config):
    planned = plan_cases(generation_config)
    first = assign_splits(planned, generation_config)
    second = assign_splits(planned, generation_config)

    assert first == second
    assert set(first.values()) <= {"dev", "holdout"}
    assert len(first) == len(planned)


def test_the_split_is_stratified_so_every_case_type_reaches_dev(generation_config):
    """A holdout that omits a case type says nothing about that case type, and
    a dev set that omits one cannot be tuned on it."""
    planned = plan_cases(generation_config)
    splits = assign_splits(planned, generation_config)

    by_type: dict[str, set[str]] = {}
    for case in planned:
        by_type.setdefault(case.case_type, set()).add(splits[case.index])

    for case_type, seen in by_type.items():
        assert "dev" in seen, f"{case_type} has no dev case"


def test_the_dev_ratio_is_roughly_honoured(generation_config):
    planned = plan_cases(generation_config)
    splits = assign_splits(planned, generation_config)
    dev = sum(1 for value in splits.values() if value == "dev")
    ratio = dev / len(splits)
    # Loose, because stratification with small per-type counts cannot hit an
    # exact global ratio - and rounding each type up to at least one dev case
    # deliberately pulls it above 0.7.
    assert 0.6 <= ratio <= 0.85, ratio


# --------------------------------------------------------------------------
# Safety: no keys, no real brands
# --------------------------------------------------------------------------


def test_generation_writes_no_key_or_certificate_material(small_config, tmp_path):
    """The signing key is generated in memory and never written.

    Checked two ways: no file with a key-ish extension, and no PEM header in
    any generated byte stream. The second matters more - a key accidentally
    embedded in a JSON file would not have a .pem name.
    """
    generate(config=small_config, out_root=tmp_path)

    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        assert path.suffix.lower() not in (
            ".pem",
            ".key",
            ".p12",
            ".pfx",
            ".der",
            ".jks",
        ), path

    markers = (
        b"-----BEGIN PRIVATE KEY",
        b"-----BEGIN RSA PRIVATE KEY",
        b"-----BEGIN ENCRYPTED PRIVATE KEY",
        b"-----BEGIN CERTIFICATE-----",
    )
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for marker in markers:
            assert marker not in data, f"{path} contains {marker!r}"


def test_no_real_accreditation_body_appears_in_the_generated_text(
    small_config, tmp_path
):
    """Every institution and body is invented, and nothing imitates a real one.

    `AICTE` is checked as a whole word: it must not appear as an issuing body
    on a synthetic certificate, however harmlessly it reads in our own docs.
    """
    generate(config=small_config, out_root=tmp_path)

    forbidden = (
        r"\bAICTE\b",
        r"\bNational Board of Accreditation\b",
        r"\bNAAC\b",
        r"\bUGC\b",
        r"\bAll India Council\b",
    )
    text = "\n".join(
        (tmp_path / name).read_text(encoding="utf-8")
        for name in ("labels.json", "world.json", "issuer_registry.json")
    )
    for pattern in forbidden:
        assert not re.search(pattern, text), pattern


def test_every_generated_domain_is_under_the_reserved_test_tld(
    small_config, tmp_path
):
    """A .test domain cannot resolve, so a payload that leaked into a request
    could not reach anything. Belt to the no-network braces."""
    generate(config=small_config, out_root=tmp_path)
    world = json.loads((tmp_path / "world.json").read_text(encoding="utf-8"))

    for body in world["issuing_bodies"]:
        assert body["domain"].endswith(".test"), body
    for institution in world["institutions"]:
        assert ".test" in institution["website"], institution

    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    for entry in labels:
        payload = entry.get("qr_payload")
        if not payload or entry["case_type"] == "qr_punycode_or_shortener":
            # The shortener case deliberately uses a non-.test host, because
            # a shortener that resolved nowhere would not be a shortener.
            continue
        assert ".test" in payload, entry["application_id"]


# --------------------------------------------------------------------------
# Metric maths, on hand-built sets with hand-computed answers
# --------------------------------------------------------------------------


def _label(
    application_id: int,
    case_type: str,
    is_fraud: bool,
    expected=None,
    must_not=None,
    split: str = "dev",
    expected_issuer_status=None,
):
    return {
        "application_id": application_id,
        "case_type": case_type,
        "is_fraud": is_fraud,
        "split": split,
        "expected": expected or [],
        "must_not": must_not or [],
        "expected_issuer_status": expected_issuer_status,
    }


def test_strength_ranking_puts_an_unknown_label_lowest():
    """An unrecognised strength must not satisfy an expectation or trip a
    must_not: a label we do not understand is not evidence."""
    assert metrics.strength_rank("weak") == 0
    assert metrics.strength_rank("strong") == 2
    assert metrics.strength_rank("catastrophic") == -1
    assert metrics.meets("catastrophic", "weak") is False
    assert metrics.meets("strong", "medium") is True
    assert metrics.meets("weak", "medium") is False


def test_a_group_is_satisfied_by_any_listed_type_at_or_above_its_strength():
    label = _label(
        1,
        "x",
        True,
        expected=[{"any_of": ["a", "b"], "min_strength": "strong"}],
    )
    assert metrics.score_application(label, [("b", "strong")]).detected is True
    assert metrics.score_application(label, [("b", "medium")]).detected is False
    assert metrics.score_application(label, [("c", "strong")]).detected is False


def test_an_either_side_group_can_be_satisfied_by_the_pair():
    """Which run discovers a relationship depends on processing order, and the
    measurement must not."""
    label = _label(
        1,
        "dup",
        True,
        expected=[
            {"any_of": ["duplicate_file_exact"], "min_strength": "strong",
             "either_side": True}
        ],
    )
    assert metrics.score_application(label, [], pair_findings=[]).detected is False
    assert (
        metrics.score_application(
            label, [], pair_findings=[("duplicate_file_exact", "strong")]
        ).detected
        is True
    )


def test_a_case_with_no_expectation_is_not_counted_as_detected():
    """A clean document is not 'detected'. Counting it as such would inflate
    the detection rate with cases that expect nothing."""
    score = metrics.score_application(_label(1, "clean", False), [])
    assert score.detected is None

    rows = metrics.per_case_type([score])
    assert rows[0]["scorable"] == 0
    assert rows[0]["detection_rate"] is None


def test_must_not_violations_are_only_counted_at_medium_or_above():
    label = _label(1, "hard_negative_x", False, must_not=["noisy"])
    assert metrics.score_application(label, [("noisy", "weak")]).must_not_violations == []
    assert metrics.score_application(label, [("noisy", "medium")]).must_not_violations
    assert metrics.score_application(label, [("noisy", "strong")]).must_not_violations


def test_document_level_maths_on_a_hand_built_set():
    """Hand-computed: TP=2, FN=1, FP=1, TN=2.

    precision = 2/3 = 0.6667, recall = 2/3 = 0.6667,
    F1 = 2*(2/3)*(2/3)/(4/3) = 0.6667
    """
    scores = [
        metrics.score_application(_label(1, "fraud_a", True), [("x", "strong")]),
        metrics.score_application(_label(2, "fraud_a", True), [("x", "medium")]),
        metrics.score_application(_label(3, "fraud_a", True), [("x", "weak")]),
        metrics.score_application(_label(4, "clean", False), [("y", "strong")]),
        metrics.score_application(_label(5, "clean", False), []),
        metrics.score_application(_label(6, "clean", False), [("y", "weak")]),
    ]
    doc = metrics.document_level(scores)

    assert doc["true_positive"] == 2
    assert doc["false_negative"] == 1
    assert doc["false_positive"] == 1
    assert doc["true_negative"] == 2
    assert doc["precision"] == pytest.approx(2 / 3)
    assert doc["recall"] == pytest.approx(2 / 3)
    assert doc["f1"] == pytest.approx(2 / 3)
    assert doc["clean_documents"] == 3
    assert doc["clean_flagged"] == 1
    assert doc["clean_false_positive_rate"] == pytest.approx(1 / 3)


def test_precision_breakdown_separates_expected_flags_from_real_ones():
    """A revoked certificate is genuine and SHOULD be flagged.

    Hand-computed: one fraud detected (TP=1), and two genuine documents
    flagged, so the headline metric sees FP=2 and precision = 1/3 = 33.3%.
    One of those two was flagged for a reason the label expected, so counting
    it as correct gives (1+1)/((1+1)+1) = 2/3 = 66.7%.

    The gap between those two numbers is the whole point of the breakdown: the
    headline metric penalises the system for correctly surfacing a certificate
    the issuer withdrew, and without the second figure someone would "fix"
    that by suppressing the issuer-state findings.
    """
    revoked = _label(
        2,
        "issuer_state_revoked",
        False,
        expected=[{"any_of": ["issuer_status_not_active"], "min_strength": "strong"}],
    )
    scores = [
        metrics.score_application(_label(1, "fraud", True), [("x", "strong")]),
        metrics.score_application(revoked, [("issuer_status_not_active", "strong")]),
        metrics.score_application(_label(3, "clean", False), [("y", "strong")]),
    ]
    doc = metrics.document_level(scores)

    assert doc["true_positive"] == 1
    assert doc["false_positive"] == 2
    assert doc["precision"] == pytest.approx(1 / 3)
    assert doc["genuine_flagged_as_expected"] == 1
    assert doc["genuine_flagged_unexpectedly"] == 1
    assert doc["precision_counting_expected_flags_as_correct"] == pytest.approx(2 / 3)


def test_an_empty_denominator_gives_none_not_zero():
    """'Nothing was detected' and 'there was nothing to detect' are different
    statements, and they print identically unless the difference is kept."""
    doc = metrics.document_level([])
    assert doc["precision"] is None
    assert doc["recall"] is None
    assert doc["clean_false_positive_rate"] is None
    assert report.pct(None) == "n/a"


def test_the_percentile_is_nearest_rank_so_it_names_a_real_duration():
    scores = []
    for index, duration in enumerate([100, 200, 300, 400, 500], start=1):
        score = metrics.score_application(_label(index, "clean", False), [])
        score.duration_ms = duration
        scores.append(score)

    timing = metrics.timing(scores, percentile=95)
    assert timing["mean_ms"] == pytest.approx(300)
    # Nearest-rank: ceil(0.95 * 5) = 5th value.
    assert timing["p95_ms"] == 500
    assert timing["min_ms"] == 100
    assert timing["max_ms"] == 500


def test_qr_decode_rate_is_broken_down_by_noise_level():
    rows = []
    for index, (level, decoded) in enumerate(
        [("none", True), ("none", True), ("hard", False), ("hard", True)], start=1
    ):
        score = metrics.score_application(_label(index, "clean", False), [])
        score.qr_expected = True
        score.qr_detected = decoded
        score.noise_level = level
        rows.append(score)

    qr = metrics.qr_decode_rates(rows)
    assert qr["expected"] == 4
    assert qr["decoded"] == 3
    assert qr["rate"] == pytest.approx(0.75)
    assert qr["by_noise_level"]["none"]["rate"] == pytest.approx(1.0)
    assert qr["by_noise_level"]["hard"]["rate"] == pytest.approx(0.5)
    assert len(qr["failures"]) == 1


def test_hard_negative_rates_distinguish_strong_from_medium():
    label = _label(1, "hard_negative_x", False, must_not=["y"])
    strong = metrics.score_application(label, [("y", "strong")])
    medium = metrics.score_application(
        _label(2, "hard_negative_x", False, must_not=["y"]), [("y", "medium")]
    )
    quiet = metrics.score_application(
        _label(3, "hard_negative_x", False, must_not=["y"]), [("y", "weak")]
    )

    rows = metrics.hard_negative_rates([strong, medium, quiet])
    assert rows[0]["cases"] == 3
    assert rows[0]["false_positive_at_strong"] == 1
    assert rows[0]["false_positive_at_medium_or_above"] == 2
    assert rows[0]["false_positive_rate_at_strong"] == pytest.approx(1 / 3)
    assert rows[0]["false_positive_rate_at_medium_or_above"] == pytest.approx(2 / 3)


def test_field_accuracy_counts_a_null_expectation_as_correct_only_when_empty():
    from evaluation.run import _compare_fields

    printed = {"grade": None, "institution_name": "Konkan Coast Inst."}
    assert _compare_fields(printed, {"institution_name": "konkan  coast   inst."}) == {
        "grade": True,
        "institution_name": True,
    }
    # Inventing a value for a field that was not printed is an error.
    assert _compare_fields(printed, {"grade": "A+", "institution_name": "x"}) == {
        "grade": False,
        "institution_name": False,
    }


def test_programme_field_accuracy_compares_as_a_comma_split_set():
    from evaluation.run import _compare_fields

    printed = {"programme": "Bachelor of Technology, Civil Engineering"}
    assert _compare_fields(
        printed, {"programme": ["Bachelor of Technology", "Civil Engineering"]}
    ) == {"programme": True}
    assert _compare_fields(printed, {"programme": ["Civil Engineering"]}) == {
        "programme": False
    }


# --------------------------------------------------------------------------
# The world
# --------------------------------------------------------------------------


def test_the_world_is_deterministic_and_fully_fictional(generation_config):
    first = build_world(generation_config).to_dict()
    second = build_world(generation_config).to_dict()
    assert first == second
    assert len(first["institutions"]) == generation_config["world"][
        "institution_count"
    ]
    for institution in first["institutions"]:
        assert institution["abbreviated_name"]
        assert institution["departments"]


def test_the_abbreviated_name_is_a_plausible_short_form(generation_config):
    """The hard negative only means something if a human would write this."""
    world = build_world(generation_config)
    shortened = [
        i for i in world.institutions if i.abbreviated_name != i.name
    ]
    assert shortened, "no institution produced an abbreviation"
    for institution in shortened:
        assert "." in institution.abbreviated_name


# --------------------------------------------------------------------------
# The allowlist override
# --------------------------------------------------------------------------


def test_the_eval_allowlist_covers_this_world_without_weakening_the_rule(
    small_config, tmp_path
):
    """The runner widens the allowlist to this world's issuers.

    What it must NOT do is make the lookalike acceptable. Each host goes in
    with `allow_subdomains: false`, so step 3's equality-or-dot-boundary rule
    still rejects `verify.nbta-demo.test.evil-mirror.test`.
    """
    from app.qr.checks import host_is_allowlisted
    from evaluation.run import qr_config_for_world

    generate(config=small_config, out_root=tmp_path)
    config = qr_config_for_world(tmp_path, small_config)
    assert config is not None

    hosts = [entry["host"] for entry in config["allowlist"]]
    assert "verify.nbta-demo.test" in hosts
    assert all(entry["allow_subdomains"] is False for entry in config["allowlist"])

    # It returns (matched, which host matched).
    assert host_is_allowlisted("verify.nbta-demo.test", config)[0] is True
    # The fraud shapes must stay detectable.
    assert (
        host_is_allowlisted("verify.nbta-demo.test.evil-mirror.test", config)[0]
        is False
    )
    assert host_is_allowlisted("evil-mirror.test", config)[0] is False
    assert host_is_allowlisted("xn--80ak6aa92e.test", config)[0] is False


# --------------------------------------------------------------------------
# A mini end-to-end run
# --------------------------------------------------------------------------


@pytest.mark.qr
@pytest.mark.ocr
def test_a_five_application_oracle_run_end_to_end(small_config, tmp_path):
    """The whole harness, small: generate, run, score, write."""
    from evaluation.run import RunContext, load_dataset, run_once

    run_config = copy.deepcopy(load_config("eval_run"))
    run_config["limits"]["max_applications"] = 5
    run_config["issuer"]["registry"] = str(tmp_path / "issuer_registry.json")
    run_config["cache"]["enabled"] = False

    generate(config=small_config, out_root=tmp_path)
    labels, manifest = load_dataset(tmp_path, small_config)

    context = RunContext(
        mode="oracle",
        split="all",
        out_root=tmp_path,
        labels=labels,
        manifest=manifest,
        run_config=run_config,
        generation_config=small_config,
    )
    payload = run_once(context, verbose=False)

    assert payload["applications_run"] == 5
    assert payload["mode"] == "oracle"
    # Oracle mode never reports extraction accuracy: the answers are the
    # ground truth, so the figure would be 100% and would mean nothing.
    assert payload["extraction_accuracy_reported"] is False
    assert "field_accuracy" not in payload["all"]

    assert (tmp_path / "results" / "results-oracle.json").exists()
    assert (tmp_path / "results" / "per_application-oracle.csv").exists()
    assert (tmp_path / "results" / "summary-oracle.md").exists()

    for entry in payload["per_application"]:
        assert entry["error"] is None, entry


@pytest.mark.qr
@pytest.mark.ocr
def test_the_runner_touches_no_network(small_config, tmp_path, monkeypatch):
    """Nothing leaves the process: the mock issuer is in-process and oracle
    mode makes no model call at all."""
    import socket

    from evaluation.run import RunContext, load_dataset, run_once

    generate(config=small_config, out_root=tmp_path)
    labels, manifest = load_dataset(tmp_path, small_config)

    run_config = copy.deepcopy(load_config("eval_run"))
    run_config["limits"]["max_applications"] = 3
    run_config["issuer"]["registry"] = str(tmp_path / "issuer_registry.json")
    run_config["cache"]["enabled"] = False

    attempts: list[str] = []

    def forbidden(name):
        def blocked(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"network access attempted via {name}")

        return blocked

    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "getfqdn",
    ):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, forbidden(f"socket.{name}"))

    # Prove the trap is armed before trusting it.
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.getaddrinfo("verify.nbta-demo.test", 443)
    attempts.clear()

    context = RunContext(
        mode="oracle",
        split="all",
        out_root=tmp_path,
        labels=labels,
        manifest=manifest,
        run_config=run_config,
        generation_config=small_config,
    )
    run_once(context, verbose=False)

    assert attempts == []


# --------------------------------------------------------------------------
# Order independence comparison
# --------------------------------------------------------------------------


LABELS_FOR_PAIR = [
    {"application_id": 1, "case_type": "clean", "pair_application_id": 2},
    {
        "application_id": 2,
        "case_type": "duplicate_file_other_institution",
        "pair_application_id": 1,
    },
]


def test_order_comparison_survives_the_finding_moving_to_the_other_side():
    """The regression this key exists for.

    A relationship is recorded by whichever of the two documents is processed
    second, so reversing the order moves the finding from the duplicate to its
    source. Keying on the discovering document's case type reported three
    relationships "only in normal order" and three "only in shuffled order"
    when they were the same three seen from the other end - a false alarm that
    looked exactly like a real order-dependence bug.
    """
    from evaluation.run import compare_orderings

    # Normal order: the duplicate (app 2) ran second and recorded it.
    normal = {
        "per_application": [
            {"application_id": 1, "case_type": "clean", "findings": []},
            {
                "application_id": 2,
                "case_type": "duplicate_file_other_institution",
                "findings": [{"type": "duplicate_file_exact", "strength": "strong"}],
            },
        ]
    }
    # Shuffled: the clean source ran second and recorded the same relationship.
    shuffled = {
        "per_application": [
            {
                "application_id": 1,
                "case_type": "clean",
                "findings": [{"type": "duplicate_file_exact", "strength": "strong"}],
            },
            {
                "application_id": 2,
                "case_type": "duplicate_file_other_institution",
                "findings": [],
            },
        ]
    }
    comparison = compare_orderings(normal, shuffled, LABELS_FOR_PAIR)

    assert comparison["identical"] is True
    assert comparison["only_in_normal_order"] == []
    assert comparison["only_in_shuffled_order"] == []
    assert comparison["shared"] == [
        ("clean", "duplicate_file_other_institution", "duplicate_file_exact")
    ]


def test_order_comparison_reports_a_genuine_difference():
    """A relationship found in one order and not the other is a real failure."""
    from evaluation.run import compare_orderings

    normal = {
        "per_application": [
            {"application_id": 1, "case_type": "clean", "findings": []},
            {
                "application_id": 2,
                "case_type": "duplicate_file_other_institution",
                "findings": [{"type": "duplicate_file_exact", "strength": "strong"}],
            },
        ]
    }
    shuffled = {
        "per_application": [
            {"application_id": 1, "case_type": "clean", "findings": []},
            {
                "application_id": 2,
                "case_type": "duplicate_file_other_institution",
                "findings": [],
            },
        ]
    }
    comparison = compare_orderings(normal, shuffled, LABELS_FOR_PAIR)

    assert comparison["identical"] is False
    assert comparison["only_in_normal_order"] == [
        ("clean", "duplicate_file_other_institution", "duplicate_file_exact")
    ]


# --------------------------------------------------------------------------
# The distances report
# --------------------------------------------------------------------------


def test_the_distance_classifier_keeps_relayout_pairs_out_of_the_copy_bucket():
    """`number_reuse_other_institution` re-renders on a DIFFERENT layout, so
    the pages are not meant to look alike - the number check catches it.

    Bucketing it as a recompressed copy would charge the perceptual hash with
    a miss it was never given, which is exactly the mistake the first version
    of this module made.
    """
    clean = {
        "application_id": 1,
        "case_type": "clean",
        "template": "classic",
        "institution_id": "INST-001",
        "source_application_id": None,
    }
    relayout = {
        "application_id": 2,
        "case_type": "number_reuse_other_institution",
        "template": "tabular",
        "institution_id": "INST-002",
        "source_application_id": 1,
    }
    duplicate = {
        "application_id": 3,
        "case_type": "duplicate_file_other_institution",
        "template": "classic",
        "institution_id": "INST-003",
        "source_application_id": 1,
    }
    edited = {
        "application_id": 4,
        "case_type": "resaved_copy_with_edit",
        "template": "classic",
        "institution_id": "INST-004",
        "source_application_id": 1,
    }

    assert distances._classify(clean, relayout) == "same_number_different_layout"
    assert distances._classify(clean, duplicate) == "same_document_recompressed"
    assert distances._classify(clean, edited) == "edited_copy"


def test_the_distance_verdict_names_the_side_that_failed():
    """Two one-sided checks, not one accuracy figure: a missed resubmission and
    a flagged honest applicant are not interchangeable errors."""
    too_tight = distances._verdict(
        {
            "same_document_recompressed": {"max": 20},
            "unrelated": {"min": 40},
        },
        threshold=12,
    )
    assert too_tight["separates_copies_from_unrelated"] is False
    assert any("ABOVE the threshold" in note for note in too_tight["notes"])

    too_loose = distances._verdict(
        {
            "same_document_recompressed": {"max": 5},
            "unrelated": {"min": 10},
        },
        threshold=12,
    )
    assert too_loose["separates_copies_from_unrelated"] is False
    assert any("BELOW the threshold" in note for note in too_loose["notes"])

    good = distances._verdict(
        {
            "same_document_recompressed": {"max": 8},
            "unrelated": {"min": 40},
        },
        threshold=12,
    )
    assert good["separates_copies_from_unrelated"] is True


def test_the_distances_module_never_writes_config():
    """Report only. A threshold that moved itself would make every earlier run
    incomparable."""
    source = (
        Path(__file__).resolve().parent.parent / "evaluation" / "distances.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("write_text", "safe_dump", "yaml.dump", "open("):
        if forbidden == "write_text":
            # It does write its own report; it must not write into config/.
            assert "config/" not in source
            continue
        assert forbidden not in source


# --------------------------------------------------------------------------
# The LLM cache
# --------------------------------------------------------------------------


def test_the_cache_key_covers_prompt_model_provider_and_prompt_version(tmp_path):
    """Leave any of them out and a cached answer is eventually served for a
    different question."""
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    def provider(model: str, name: str, version: str):
        inner = StubProvider(responses=["{}"] * 10)
        inner.model = model
        inner.name = name
        return CachingProvider(inner, root=tmp_path, prompt_version=version)

    base = provider("m1", "groq", "v1")
    key = base.key_for("hello")

    assert provider("m2", "groq", "v1").key_for("hello") != key
    assert provider("m1", "other", "v1").key_for("hello") != key
    assert provider("m1", "groq", "v2").key_for("hello") != key
    assert provider("m1", "groq", "v1").key_for("goodbye") != key
    # Same everything, same key.
    assert provider("m1", "groq", "v1").key_for("hello") == key


def test_a_cache_hit_is_recorded_and_costs_no_call(tmp_path):
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    inner = StubProvider(responses=['{"a": 1}'])
    cache = CachingProvider(inner, root=tmp_path, prompt_version="v1")

    first = cache.complete("prompt")
    assert inner.calls == 1
    assert first.metadata.get("cache_hit") is not True

    second = cache.complete("prompt")
    # The stub had exactly one response, so a second real call would have
    # raised. It did not: the answer came from disk.
    assert inner.calls == 1
    assert second.metadata["cache_hit"] is True
    assert second.text == first.text
    assert cache.hits == 1 and cache.misses == 1


def test_a_damaged_cache_entry_costs_a_call_not_a_failure(tmp_path):
    """An unreadable cache must never fail a run."""
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    inner = StubProvider(responses=["{}", "{}"])
    cache = CachingProvider(inner, root=tmp_path, prompt_version="v1")
    cache.complete("prompt")

    path = cache.path_for("prompt")
    path.write_text("{ this is not json", encoding="utf-8")

    result = cache.complete("prompt")
    assert result.text == "{}"
    assert inner.calls == 2


def test_a_read_only_cache_serves_hits_but_writes_nothing(tmp_path):
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    inner = StubProvider(responses=["{}", "{}"])
    cache = CachingProvider(
        inner, root=tmp_path, prompt_version="v1", read_only=True
    )
    cache.complete("prompt")
    assert cache.writes == 0
    assert not cache.path_for("prompt").exists()


def test_a_disabled_cache_is_a_pass_through(tmp_path):
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    inner = StubProvider(responses=["{}", "{}"])
    cache = CachingProvider(
        inner, root=tmp_path, prompt_version="v1", enabled=False
    )
    cache.complete("prompt")
    cache.complete("prompt")
    assert inner.calls == 2
    assert cache.hits == 0


def test_the_cache_stores_a_prompt_hash_not_the_prompt(tmp_path):
    """The prompt contains document text, so it does not go on disk."""
    from app.llm.cache import CachingProvider
    from app.llm.stub_provider import StubProvider

    inner = StubProvider(responses=["{}"])
    cache = CachingProvider(inner, root=tmp_path, prompt_version="v1")
    cache.complete("Certificate Number: NBTA/EVAL/2024/00417")

    stored = cache.path_for("Certificate Number: NBTA/EVAL/2024/00417").read_text(
        encoding="utf-8"
    )
    assert "NBTA/EVAL/2024/00417" not in stored
    assert "prompt_sha256" in stored


# --------------------------------------------------------------------------
# The oracle
# --------------------------------------------------------------------------


def test_the_oracle_grounds_its_snippets_in_the_real_page_text():
    from evaluation.oracle import OracleProvider

    printed = {
        "institution_name": "Konkan Coast Institute of Engineering",
        "certificate_number": "NBTA/EVAL/2024/00417",
        "programme": "Bachelor of Technology, Civil Engineering",
        "accreditation_status_or_grade": None,
    }
    prompt = (
        "instructions ...\n## Certificate text\n"
        "Institution: Konkan Coast Institute of Engineering\n"
        "Certificate Number: NBTA/EVAL/2024/00417\n"
        "Programme: Bachelor of Technology, Civil Engineering\n"
    )
    answer = json.loads(OracleProvider(printed).complete(prompt).text)

    assert answer["certificate_number"]["value"] == "NBTA/EVAL/2024/00417"
    # The snippet is the whole line, which is a stronger claim than the bare
    # value and is what step 2's guard wants.
    assert (
        answer["certificate_number"]["source_snippet"]
        == "Certificate Number: NBTA/EVAL/2024/00417"
    )
    # A field the generator did not print stays null; inventing one would turn
    # the missing-grade hard negative into a mismatch.
    assert answer["accreditation_status_or_grade"]["value"] is None
    # programme is a list in the schema, split on commas.
    assert answer["programme"]["value"] == [
        "Bachelor of Technology",
        "Civil Engineering",
    ]


def test_the_oracle_reports_a_value_it_could_not_ground():
    """Oracle mode does not bypass the snippet guard.

    On unreadable input the printed value genuinely is not in the page text,
    the snippet is null, and step 2 will discard the field - exactly as it
    would for a real model. The guard is part of the system under test.
    """
    from evaluation.oracle import OracleProvider

    oracle = OracleProvider({"certificate_number": "NBTA/EVAL/2024/00417"})
    answer = json.loads(
        oracle.complete("## Certificate text\nunreadable sludge\n").text
    )

    assert answer["certificate_number"]["source_snippet"] is None
    assert answer["certificate_number"]["confidence"] == "low"
    assert "certificate_number" in oracle.ungrounded


def test_the_oracle_reports_no_tokens():
    """A fabricated token count would pollute the cost totals."""
    from evaluation.oracle import OracleProvider

    completion = OracleProvider({}).complete("## Certificate text\nx")
    assert completion.input_tokens == 0
    assert completion.output_tokens == 0


# --------------------------------------------------------------------------
# Full dataset and full run (slow)
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_the_full_dataset_generates_and_is_internally_consistent(tmp_path):
    manifest = generate(out_root=tmp_path)

    assert manifest["generated_case_count"] >= 60
    assert manifest["fraud_count"] > 0
    assert manifest["genuine_count"] > 0

    # Every fraud case type the recipes define is represented.
    for case_type in recipes.fraud_case_types():
        assert case_type in manifest["case_counts"], case_type

    # Every template is exercised, which is the point of this set.
    assert len(manifest["template_counts"]) >= 4

    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    assert len(labels) == manifest["generated_case_count"]
    orders = [entry["processing_order"] for entry in labels]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)
