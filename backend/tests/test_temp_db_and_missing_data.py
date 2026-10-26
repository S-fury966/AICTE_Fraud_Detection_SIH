"""The temporary-database helper and the missing-data fast-fail.

Fast, machine-independent: no network, no key, no pre-generated dataset. The
one genuinely Windows-only behaviour (a SQLite file blocking its own directory
delete) is simulated cross-platform by patching, and the tests that assert the
*clean* message hold on every platform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.engine import Database, temporary_sqlite_database


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


def test_helper_yields_a_usable_database_and_removes_the_dir_on_success():
    captured: dict[str, Path] = {}
    with temporary_sqlite_database(name="scratch.db") as db:
        assert isinstance(db, Database)
        assert db.schema_version()  # the schema was created
        captured["dir"] = Path(db.engine.url.database).parent
        assert captured["dir"].exists()
    # Disposed and cleaned up: the directory is gone.
    assert not captured["dir"].exists()


def test_helper_disposes_and_removes_the_dir_even_on_exception():
    """The reproduced bug: an exception inside must not leave the directory
    un-removable. After it, the temp directory is gone - no WinError."""
    captured: dict[str, Path] = {}

    with pytest.raises(ValueError, match="boom"):
        with temporary_sqlite_database() as db:
            captured["dir"] = Path(db.engine.url.database).parent
            # Touch the database so the engine really holds the file open,
            # which is what blocked the delete on Windows.
            db.schema_version()
            raise ValueError("boom")

    assert "dir" in captured
    assert not captured["dir"].exists()


def test_disposing_the_database_twice_is_safe():
    with temporary_sqlite_database() as db:
        db.dispose()
        db.dispose()  # the helper will dispose a third time in finally
    # No error is the assertion.


def test_a_cleanup_failure_never_masks_the_real_exception(monkeypatch):
    """Even if directory cleanup itself raised, the original exception is what
    propagates - the cleanup error is only logged."""
    import tempfile as tempfile_module

    real_ctor = tempfile_module.TemporaryDirectory

    class ExplodingCleanup(real_ctor):  # type: ignore[misc]
        def cleanup(self):
            raise OSError("simulated cleanup failure")

    monkeypatch.setattr("app.db.engine.tempfile.TemporaryDirectory", ExplodingCleanup)

    with pytest.raises(ValueError, match="original"):
        with temporary_sqlite_database():
            raise ValueError("original")

    # And a clean exit with an exploding cleanup also does not raise.
    with temporary_sqlite_database():
        pass


# --------------------------------------------------------------------------
# demo.evaluate / demo.load missing-data fast-fail (entry points)
# --------------------------------------------------------------------------


def _assert_clean_missing_message(capsys, code: int):
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 1
    assert "Run: python -m demo.generate" in text
    assert "WinError" not in text
    assert "NotADirectoryError" not in text
    assert "Traceback" not in text


def test_demo_evaluate_entry_point_on_empty_data_exits_with_a_hint(tmp_path, capsys):
    from demo.evaluate import main

    code = main(["--data", str(tmp_path), "--out", str(tmp_path / "out.json")])
    _assert_clean_missing_message(capsys, code)


def test_demo_load_entry_point_on_empty_data_exits_with_a_hint(tmp_path, capsys):
    from demo.load import main

    code = main(["--data", str(tmp_path), "--database-url", "sqlite:///:memory:"])
    _assert_clean_missing_message(capsys, code)


def test_a_partially_generated_demo_dataset_names_the_missing_file(tmp_path, capsys):
    """Folder and labels present, one referenced document gone."""
    from demo.evaluate import main

    (tmp_path / "applications").mkdir()
    labels = {
        "applications": [
            {"key": "x", "documents": [{"file": "applications/x/certificate.pdf"}]}
        ]
    }
    (tmp_path / "labels.json").write_text(__import__("json").dumps(labels), encoding="utf-8")

    code = main(["--data", str(tmp_path), "--out", str(tmp_path / "out.json")])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 1
    assert "applications/x/certificate.pdf" in text
    assert "Run: python -m demo.generate" in text
    assert "WinError" not in text


def test_ensure_demo_dataset_returns_labels_when_complete(tmp_path):
    import json

    from demo.load import ensure_demo_dataset

    (tmp_path / "applications" / "x").mkdir(parents=True)
    (tmp_path / "applications" / "x" / "certificate.pdf").write_bytes(b"%PDF-1.7\n")
    labels = {"applications": [{"key": "x", "documents": [{"file": "applications/x/certificate.pdf"}]}]}
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")

    assert ensure_demo_dataset(tmp_path) == labels


# --------------------------------------------------------------------------
# evaluation.run missing-data fast-fail (entry point)
# --------------------------------------------------------------------------


def test_evaluation_run_entry_point_on_empty_data_exits_with_a_hint(tmp_path, capsys):
    from evaluation.run import main

    code = main(["--mode", "oracle", "--out", str(tmp_path)])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 1
    assert "Run: python -m evaluation.generate" in text
    assert "WinError" not in text
    assert "Traceback" not in text


def test_a_partially_generated_eval_dataset_names_the_missing_file(tmp_path):
    import json

    from app.core.config import load_config
    from evaluation.run import DatasetNotFound, load_dataset

    gen = load_config("eval_generation")
    labels_name = gen.get("output", {}).get("labels", "labels.json")
    manifest_name = gen.get("output", {}).get("manifest", "manifest.json")
    (tmp_path / manifest_name).write_text("{}", encoding="utf-8")
    (tmp_path / labels_name).write_text(
        json.dumps([{"application_id": 1, "document": "documents/app-0001.pdf"}]),
        encoding="utf-8",
    )

    with pytest.raises(DatasetNotFound, match="documents/app-0001.pdf"):
        load_dataset(tmp_path, gen)
