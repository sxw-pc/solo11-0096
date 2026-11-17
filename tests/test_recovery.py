"""Publication protocol: recovery decisions, corruption, concurrency, legacy."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dimcheck.ledger import (DB_NAME, STATUS_COMMITTED, STATUS_ISOLATED,
                             STATUS_LEGACY, read_isolated_publications,
                             read_metrics, read_publications, read_runs)
from dimcheck.pubprotocol import (ARTIFACT_NAMES, PublicationFault,
                                  build_manifest, report_digest_of, verify_unit)
from dimcheck.publishing import recover
from tests.publication_helpers import current_unit, publish, staging_dirs


def _crash_after_files_before_ledger(project, out: Path, monkeypatch):
    """Publish A, then leave B's full unit on disk with no B ledger row."""
    publish(project, out)
    monkeypatch.setenv("DIMCHECK_FAULT", "db_commit")
    with pytest.raises(PublicationFault):
        publish(project, out)
    monkeypatch.delenv("DIMCHECK_FAULT", raising=False)
    staging = staging_dirs(out)
    assert len(staging) == 1
    with sqlite3.connect(out / DB_NAME) as connection:
        connection.execute("DELETE FROM publications WHERE staging_id = ?",
                           (staging[0].name,))
        connection.commit()
    return staging[0]


def test_recovery_backfills_verifiable_unit_as_new_latest(project, tmp_path, monkeypatch):
    out = tmp_path / "out"
    _crash_after_files_before_ledger(project, out, monkeypatch)
    before = len(read_runs(out))
    result = recover(out)
    assert result["backfilled"], result
    assert not staging_dirs(out)
    runs = read_runs(out)
    assert len(runs) == before + 1
    backfilled = runs[0]
    assert backfilled["publication_status"] == STATUS_COMMITTED
    assert current_unit(out).name == backfilled["publication_id"]
    assert verify_unit(current_unit(out))[0]


def test_recovery_isolates_unverifiable_unit_and_keeps_previous(
        project, tmp_path, monkeypatch):
    out = tmp_path / "out"
    staging = _crash_after_files_before_ledger(project, out, monkeypatch)
    good = current_unit(out)
    run_path = staging / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    doc["verdict"] = "tampered"
    run_path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    payloads = {key: (staging / name).read_text(encoding="utf-8")
                for key, name in ARTIFACT_NAMES.items()}
    digest = report_digest_of(staging)
    (staging / "manifest.json").write_text(
        json.dumps(build_manifest(payloads, digest), sort_keys=True, indent=2)
        + "\n", encoding="utf-8")
    result = recover(out)
    assert not result["backfilled"]
    assert result["isolated"][0]["reason"] == "metadata_unverifiable"
    assert current_unit(out) == good
    assert len(read_runs(out)) == 1
    assert not staging.exists() and (out / "quarantine").is_dir()


def test_repeated_recovery_never_duplicates_ledger_rows(project, tmp_path):
    out = tmp_path / "out"
    publish(project, out)
    for _ in range(3):
        recover(out)
    assert len(read_runs(out)) == 1
    assert len(read_publications(out)) == 1


def test_committed_unit_with_corrupt_digest_is_quarantined(project, tmp_path):
    out = tmp_path / "out"
    publish(project, out)
    unit = current_unit(out)
    report_path = unit / "report.json"
    text = report_path.read_text(encoding="utf-8")
    report_path.write_text(text.replace('"pass"', '"fail"', 1), encoding="utf-8")
    result = recover(out)
    assert result["isolated"][0]["reason"].startswith("digest_mismatch")
    assert not unit.exists()
    assert read_runs(out)[0]["publication_status"] == STATUS_ISOLATED
    assert read_isolated_publications(out)
    current = out / "current"
    assert not current.is_symlink() or current.exists()


def test_corrupt_newest_falls_back_to_previous_healthy_unit(project, tmp_path):
    from dimcheck.ledger import read_metrics
    out = tmp_path / "out"
    publish(project, out)
    first = current_unit(out)
    project.write_csv(
        "fct_sales.csv",
        ["sale_id", "customer_key", "product_key", "date_key", "amount",
         "quantity", "avg_price", "on_hand"],
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90], [1004, 2, 2, 2, 9.0, 1, 9.0, 60]],
    )
    publish(project, out)
    newest = current_unit(out)
    assert newest != first
    target = newest / "report.json"
    original = target.read_text(encoding="utf-8")
    assert '"verdict": "pass"' in original
    target.write_text(original.replace('"verdict": "pass"',
                                       '"verdict": "fail"', 1),
                      encoding="utf-8")
    result = recover(out)
    assert result["isolated"][0]["reason"].startswith("digest_mismatch")
    # Latest view falls back; both ledger rows and the old unit survive.
    assert current_unit(out) == first
    runs = read_runs(out, limit=100)
    assert len(runs) == 2
    assert read_metrics(out, 1)
    assert first.exists() and not newest.exists()


def test_concurrent_processes_serialize_and_share_one_latest(project, tmp_path):
    out = tmp_path / "out"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    driver = tmp_path / "_driver.py"
    driver.write_text(
        "from dimcheck.cli import run_once\n"
        f"run_once({str(project.contract)!r}, {str(out)!r}, quiet=True)\n",
        encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(driver)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(2)]
    assert [p.wait() for p in procs] == [0, 0]
    runs = read_runs(out)
    assert len(runs) == 2 and len({r["pk"] for r in runs}) == 2
    assert all(p["status"] == STATUS_COMMITTED
               for p in read_publications(out))
    current = current_unit(out)
    for name in ARTIFACT_NAMES.values():
        assert (out / name).resolve() == (current / name).resolve()
    assert verify_unit(current)[0]


def test_legacy_flat_output_is_migrated_but_not_marked_verified(tmp_path):
    import shutil
    out = tmp_path / "out"
    out.mkdir()
    legacy_root = Path(__file__).resolve().parents[1] / "out"
    for name in ARTIFACT_NAMES.values():
        (out / name).write_bytes((legacy_root / name).read_bytes())
    shutil.copy(legacy_root / DB_NAME, out / DB_NAME)
    result = recover(out)
    assert result["backfilled"] == []
    runs = read_runs(out, limit=100)
    assert runs
    assert all(r["publication_status"] == STATUS_LEGACY for r in runs)
    assert read_publications(out) == []
    assert read_isolated_publications(out)[0]["reason"] == "legacy_unverified"
    assert (out / "current").is_symlink()
    assert (out / "report.json").is_symlink()
    assert read_metrics(out, 1)
    recover(out)
    assert len(read_runs(out, limit=100)) == len(runs)


def test_serve_recovery_routes_expose_publication_registry(project, tmp_path):
    import threading
    from urllib.request import urlopen
    from dimcheck.server import make_server
    out = tmp_path / "out"
    publish(project, out)
    server = make_server(out, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/publications.json", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["current_view_verified"] is True
        assert payload["publications"][0]["status"] == STATUS_COMMITTED
        with urlopen(base + "/report.json", timeout=5) as response:
            assert json.loads(response.read().decode("utf-8"))["verdict"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
