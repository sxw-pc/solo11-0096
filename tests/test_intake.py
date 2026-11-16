import json
import sqlite3
from pathlib import Path

import pytest

from dimcheck.intake import inspect_manifest, register


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_register_replays_same_delivery(tmp_path):
    manifest = FIXTURES / "dimension-delivery.json"
    ledger = tmp_path / "intake.sqlite3"
    first = register(manifest, FIXTURES, ledger)
    assert first == register(manifest, FIXTURES, ledger)
    assert {item["dimension"] for item in first["files"]} == {"dim_line", "dim_station"}
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT count(*) FROM intake_batch").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM intake_file").fetchone()[0] == 2


def test_changed_batch_identity_is_rejected(tmp_path):
    manifest = json.loads((FIXTURES / "dimension-delivery.json").read_text())
    ledger = tmp_path / "intake.sqlite3"
    original = tmp_path / "original.json"
    original.write_text(json.dumps(manifest), encoding="utf-8")
    register(original, FIXTURES, ledger)
    manifest["producer"] = "sample-producer-b"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="intake_batch_identity_conflict"):
        register(changed, FIXTURES, ledger)


def test_bad_hash_does_not_create_ledger(tmp_path):
    manifest = json.loads((FIXTURES / "dimension-delivery.json").read_text())
    manifest["dimensions"][0]["sha256"] = "0" * 64
    changed = tmp_path / "bad.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    ledger = tmp_path / "intake.sqlite3"
    with pytest.raises(ValueError, match="intake_hash_mismatch"):
        inspect_manifest(changed, FIXTURES)
    assert not ledger.exists()
