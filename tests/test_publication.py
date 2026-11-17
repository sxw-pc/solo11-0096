"""Publication protocol: happy path, determinism, and interruption windows."""
from __future__ import annotations

import json

import pytest

from dimcheck.ledger import STATUS_COMMITTED, read_publications, read_runs
from dimcheck.pubprotocol import (ARTIFACT_NAMES, FAULT_WINDOWS,
                                  PublicationFault, verify_unit)
from dimcheck.publishing import recover
from tests.publication_helpers import (FACT, FACT_HEADER, current_unit,
                                       publish, staging_dirs, unit_dirs)


def test_publish_creates_immutable_verified_unit_and_latest_view(project, tmp_path):
    out = tmp_path / "out"
    report, paths = publish(project, out)
    units = unit_dirs(out)
    assert len(units) == 1
    unit = units[0]
    assert unit.name.startswith("pub-000001-")
    assert sorted(p.name for p in unit.iterdir()) == sorted(
        list(ARTIFACT_NAMES.values()) + ["manifest.json"])
    ok, digest, reason = verify_unit(unit)
    assert ok, reason
    assert unit.name == f"pub-000001-{digest[:12]}"
    assert (out / "current").is_symlink()
    for name in ARTIFACT_NAMES.values():
        link = out / name
        assert link.is_symlink()
        assert link.resolve() == (unit / name).resolve()
    assert paths["json"].resolve() == (unit / "report.json").resolve()
    run = read_runs(out)[0]
    assert run["publication_status"] == STATUS_COMMITTED
    assert run["publication_id"] == unit.name
    assert run["report_digest"] == digest
    pub = read_publications(out)[0]
    assert pub["status"] == STATUS_COMMITTED and pub["report_digest"] == digest


def test_publication_id_combines_primary_key_and_content_digest(project, tmp_path):
    out = tmp_path / "out"
    publish(project, out)
    publish(project, out)
    runs = read_runs(out)
    assert [r["pk"] for r in runs] == [2, 1]
    assert len({r["report_digest"] for r in runs}) == 1
    for run in runs:
        assert run["publication_id"] == \
            f"pub-{run['pk']:06d}-{run['report_digest'][:12]}"


def test_history_remains_append_only_and_readable(project, tmp_path):
    out = tmp_path / "out"
    publish(project, out)
    project.write_csv(
        FACT, FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, "", 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90]],
    )
    publish(project, out)
    from dimcheck.ledger import read_findings, read_metrics
    assert len(read_runs(out)) == 2
    for run_pk in (1, 2):
        assert read_metrics(out, run_pk)
        assert read_findings(out, run_pk) is not None
    assert len(unit_dirs(out)) == 2
    assert current_unit(out).name == read_runs(out)[0]["publication_id"]


def test_data_change_under_same_contract_yields_distinct_publication(project, tmp_path):
    out = tmp_path / "out"
    first, _ = publish(project, out)
    project.write_csv(
        FACT, FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90], [1004, 2, 2, 2, 9.0, 1, 9.0, 60]],
    )
    second, _ = publish(project, out)
    runs = read_runs(out)
    assert first["run_id"] == second["run_id"]
    assert len({r["contract_fingerprint"] for r in runs}) == 1
    assert len({r["publication_id"] for r in runs}) == 2
    assert len({r["report_digest"] for r in runs}) == 2
    assert current_unit(out).name == runs[0]["publication_id"]


def test_report_bodies_are_byte_stable_and_free_of_publication_metadata(
        project, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    _, first = publish(project, tmp_path / "out1")
    _, second = publish(project, tmp_path / "out2")
    for key in ("json", "markdown", "html"):
        assert first[key].read_bytes() == second[key].read_bytes(), key
    body = first["json"].read_text(encoding="utf-8")
    assert "publication_id" not in body and "report_digest" not in body
    run_doc = json.loads(first["run"].read_text(encoding="utf-8"))
    assert run_doc["publication_id"].startswith("pub-")
    assert len(run_doc["report_digest"]) == 64


@pytest.mark.parametrize("window", list(FAULT_WINDOWS))
def test_fault_in_each_window_recovers_without_half_published_latest(
        project, tmp_path, monkeypatch, window):
    out = tmp_path / "out"
    monkeypatch.setenv("DIMCHECK_FAULT", window)
    monkeypatch.setenv("DIMCHECK_FAULT_COUNT", "1")
    with pytest.raises(PublicationFault):
        publish(project, out)
    monkeypatch.delenv("DIMCHECK_FAULT", raising=False)
    publish(project, out)
    result = recover(out)
    assert result["isolated"] == [] or all(
        item["reason"].startswith(("artifact_missing", "unit_missing"))
        for item in result["isolated"])
    current = current_unit(out)
    ok, _digest, reason = verify_unit(current)
    assert ok, reason
    committed = [p for p in read_publications(out)
                 if p["status"] == STATUS_COMMITTED]
    assert committed and current.name == committed[0]["publication_id"]
    for name in ARTIFACT_NAMES.values():
        assert (out / name).resolve().parent == current


def test_file_write_fault_isolates_incomplete_unit_and_keeps_previous(
        project, tmp_path, monkeypatch):
    out = tmp_path / "out"
    publish(project, out)
    good = current_unit(out)
    monkeypatch.setenv("DIMCHECK_FAULT", "file_write")
    with pytest.raises(PublicationFault):
        publish(project, out)
    monkeypatch.delenv("DIMCHECK_FAULT", raising=False)
    result = recover(out)
    assert result["isolated"]
    assert result["active_publication_id"] == good.name
    assert current_unit(out) == good
    assert not staging_dirs(out)
    assert (out / "quarantine").is_dir()


def test_view_switch_fault_leaves_committed_unit_that_recovery_promotes(
        project, tmp_path, monkeypatch):
    out = tmp_path / "out"
    publish(project, out)
    first = current_unit(out)
    monkeypatch.setenv("DIMCHECK_FAULT", "view_switch")
    with pytest.raises(PublicationFault):
        publish(project, out)
    monkeypatch.delenv("DIMCHECK_FAULT", raising=False)
    result = recover(out)
    assert result["isolated"] == []
    promoted = current_unit(out)
    assert promoted != first and verify_unit(promoted)[0]
    assert len(read_runs(out)) == 2


def test_db_commit_fault_rolls_back_ledger_then_republishes_once(
        project, tmp_path, monkeypatch):
    import sqlite3
    from dimcheck.ledger import DB_NAME
    out = tmp_path / "out"
    publish(project, out)
    monkeypatch.setenv("DIMCHECK_FAULT", "db_commit")
    with pytest.raises(PublicationFault):
        publish(project, out)
    monkeypatch.delenv("DIMCHECK_FAULT", raising=False)
    with sqlite3.connect(out / DB_NAME) as connection:
        # The failed transaction left no partial run/publication rows.
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM publications").fetchone()[0] == 1
    # Recovery back-fills the fully-written, verifiable unit (legal route 1).
    result = recover(out)
    assert len(result["backfilled"]) == 1
    assert len(read_runs(out)) == 2
    # A further clean run appends exactly one more row, never a duplicate.
    publish(project, out)
    assert len(read_runs(out)) == 3
    assert len(read_publications(out)) == 3
    assert verify_unit(current_unit(out))[0]
