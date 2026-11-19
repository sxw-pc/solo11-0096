"""Regression tests for station-line ownership takeover planning."""
from __future__ import annotations

import json
import sqlite3

import pytest

from dimcheck.contract import load_contract
from dimcheck.intake import register
from dimcheck.takeover import build_plan
from dimcheck.takeover_events import load_events, service_instant
from dimcheck.takeover_store import read_plan, save_plan, verify_plan

from takeover_support import (BATCHES, CONTRACT, TAKEOVER, plan, receipt,
                              register_batches, write_manifest)

OLD = BATCHES["old"][0]


def _by_date(result):
    return {str(item["service_date_key"]): item for item in result["partitions"]}


def _pending_reasons(result):
    return sorted((row["reason"], str(row["event_id"]))
                  for part in result["partitions"]
                  for row in part["pending_evidence"])


def test_clean_batch_verifies_both_policies(ledger):
    register_batches(ledger.parent, ledger, ("old", "new"))
    for policy in ("atomic_cutover", "partitioned_cutover"):
        result = plan(ledger, OLD, BATCHES["new"][0], policy)
        assert result["state"] == "confirmed"
        assert result["findings"] == []
        assert result["summary"]["pending_rows"] == 0
        assert result["summary"]["adopted_new_rows"] == 7
        assert result["summary"]["affected_rows"] == 2
        assert all(item["conclusion"] == "adopted_new" for item in result["partitions"])


def test_policy_counterexample_january_2_and_3(ledger):
    """S-A01 moves L-01->L-02 on Jan 2; S-C01 conflicts in the new batch on Jan 3."""
    register_batches(ledger.parent, ledger, ("old", "conflict"))
    atomic = plan(ledger, OLD, BATCHES["conflict"][0], "atomic_cutover")
    parted = plan(ledger, OLD, BATCHES["conflict"][0], "partitioned_cutover")
    atomic_parts = _by_date(atomic)
    parted_parts = _by_date(parted)
    assert atomic_parts["2"]["conclusion"] == "retained_old"
    assert atomic_parts["2"]["reason"] == "atomic_blocked"
    assert atomic_parts["3"]["conclusion"] == "retained_old"
    assert parted_parts["2"]["conclusion"] == "adopted_new"
    assert parted_parts["2"]["adopted_new_rows"] == 4
    assert parted_parts["3"]["conclusion"] == "retained_old"
    assert parted_parts["3"]["retained_old_rows"] == 3
    assert parted_parts["3"]["pending_rows"] == 1
    # Neither policy may pretend the Jan 3 conflict partition was verified.
    assert parted_parts["3"]["reason"] != "all_rows_verified"
    assert atomic["summary"]["adopted_new_rows"] == 0
    assert parted["summary"]["adopted_new_rows"] == 4
    assert parted["summary"]["retained_old_rows"] == 3
    assert parted["summary"]["affected_rows_by_receipt"] == {"old": 3, "new": 4}


def test_policy_is_frozen_in_plan(ledger):
    register_batches(ledger.parent, ledger, ("old", "new"))
    atomic = plan(ledger, OLD, BATCHES["new"][0], "atomic_cutover")
    parted = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    assert atomic["policy"] == "atomic_cutover"
    assert parted["policy"] == "partitioned_cutover"
    assert atomic["plan_id"] != parted["plan_id"]
    with pytest.raises(ValueError, match="takeover_policy_invalid"):
        plan(ledger, OLD, BATCHES["new"][0], "best_effort")


def test_surrogate_key_reuse_is_not_identity(ledger):
    """A key reused for a different station must not be treated as the same identity."""
    register_batches(ledger.parent, ledger, ("old", "reuse"))
    result = plan(ledger, OLD, BATCHES["reuse"][0], "partitioned_cutover")
    codes = {item["code"] for item in result["findings"]}
    assert "DIM-TO-007" in codes
    reasons = {row["reason"] for part in result["partitions"]
               for row in part["pending_evidence"]}
    assert "surrogate_key_reused" in reasons
    assert result["state"] == "pending"
    assert result["blocked"] is True
    assert result["summary"]["adopted_new_rows"] == 0
    assert result["summary"]["retained_old_rows"] == 7


def test_overlapping_events_block_the_plan(ledger):
    register_batches(ledger.parent, ledger, ("old", "new"))
    result = plan(ledger, OLD, BATCHES["new"][0], "atomic_cutover",
                  events=TAKEOVER / "transfers-overlap.json")
    assert result["state"] == "pending"
    assert result["summary"]["adopted_new_rows"] == 0
    assert all(item["conclusion"] == "retained_old" for item in result["partitions"])
    assert all(item["reason"] == "plan_pending" for item in result["partitions"])
    assert any(item["code"] == "DIM-TO-003" for item in result["findings"])


def test_event_pointing_at_missing_line_is_pending(ledger):
    register_batches(ledger.parent, ledger, ("old", "new"))
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover",
                  events=TAKEOVER / "transfers-missing.json")
    assert result["state"] == "pending"
    assert any(item["code"] == "DIM-TO-008" for item in result["findings"])
    assert result["summary"]["adopted_new_rows"] == 0


def test_half_open_boundary_uses_new_line_on_effective_day():
    events = load_events(TAKEOVER / "transfers.json")
    before = service_instant(__import__("datetime").date(2026, 1, 1))
    on = service_instant(__import__("datetime").date(2026, 1, 2))
    after = service_instant(__import__("datetime").date(2026, 1, 3))
    assert events.line_at("S-A01", before) == ("L-01", "before_first_event")
    assert events.line_at("S-A01", on) == ("L-02", None)
    assert events.line_at("S-A01", after) == ("L-02", None)


def test_half_open_boundary_in_plan(ledger):
    register_batches(ledger.parent, ledger, ("old", "new"))
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    parts = _by_date(result)
    station = next(item for item in parts["2"]["stations"]
                   if item["old_station_code"] == "S-A01")
    assert station["expected_line_code"] == "L-02"
    assert station["old_line_code"] == "L-01"
    assert station["new_line_code"] == "L-02"
    earlier = next(item for item in parts["2"]["stations"]
                   if item["old_station_code"] == "S-A02")
    assert earlier["expected_line_code"] == earlier["old_line_code"] == "L-01"


def test_missing_service_date_keeps_row_evidence(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "new"))
    data = tmp_path / "data"
    data.mkdir()
    source = TAKEOVER / "fct_ridership.csv"
    text = source.read_text(encoding="utf-8").replace("R00006,3,2", "R00006,99,2")
    (data / "fct_ridership.csv").write_text(text, encoding="utf-8")
    for name in ("dim_date.csv",):
        (data / name).write_bytes((TAKEOVER / name).read_bytes())
    (data / "old").mkdir()
    (data / "old" / "dim_line.csv").write_bytes(
        (TAKEOVER / "old" / "dim_line.csv").read_bytes())
    (data / "old" / "dim_station.csv").write_bytes(
        (TAKEOVER / "old" / "dim_station.csv").read_bytes())
    contract_path = tmp_path / "model.yaml"
    model = CONTRACT.read_text(encoding="utf-8").replace("root: .", f"root: {data}")
    contract_path.write_text(model, encoding="utf-8")
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover",
                  contract=contract_path)
    reasons = _pending_reasons(result)
    assert ("missing_service_date", "R00006") in reasons
    pending = next(row for part in result["partitions"]
                   for row in part["pending_evidence"] if row["event_id"] == "R00006")
    assert pending["expected_line_code"] is None
    assert pending["new_station_code"] is None
    assert result["state"] == "pending"  # row-level pending evidence, no silent adoption
    assert result["blocked"] is False
    parts = _by_date(result)
    assert parts["2"]["conclusion"] == "adopted_new"  # the clean partition still cuts over
    assert parts["99"]["conclusion"] == "retained_old"  # unresolvable date partition
    assert parts["99"]["pending_evidence"][0]["reason"] == "missing_service_date"


def test_changed_source_bytes_invalidate_receipt(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "new"))
    copied = tmp_path / "delivery"
    (copied / "new-clean").mkdir(parents=True)
    for dimension in ("dim_line", "dim_station"):
        (copied / "new-clean" / f"{dimension}.csv").write_bytes(
            (TAKEOVER / "new-clean" / f"{dimension}.csv").read_bytes())
    manifest = write_manifest(tmp_path / "delivery.json", "batch-new",
                              "producer-b", "new-clean")
    # Re-register against the copy so the receipt root points at mutable bytes.
    register(manifest, copied, ledger)
    (copied / "new-clean" / "dim_station.csv").write_text(
        "station_key,station_code,station_name,line_key\n1,S-ZZZ,篡改,1\n",
        encoding="utf-8")
    result = build_plan(load_contract(CONTRACT), str(ledger),
                        [receipt(OLD), receipt("batch-new", copied)],
                        str(TAKEOVER / "transfers.json"), "partitioned_cutover",
                        1767225600)
    assert result["state"] == "pending"
    assert any(item["code"] == "DIM-TO-005" for item in result["findings"])
    assert result["summary"]["adopted_new_rows"] == 0
    assert all(item["reason"] == "plan_pending" for item in result["partitions"])


def test_replay_is_idempotent_and_old_plan_survives(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "new", "conflict"))
    database = tmp_path / "takeover.sqlite3"
    first = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    assert save_plan(database, first) is True
    assert save_plan(database, first) is False
    replay = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    assert replay["plan_id"] == first["plan_id"]
    second = plan(ledger, OLD, BATCHES["conflict"][0], "partitioned_cutover")
    assert second["plan_id"] != first["plan_id"]
    assert save_plan(database, second) is True
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT count(*) FROM takeover_plan").fetchone()[0]
    assert count == 2
    stored = read_plan(database, first["plan_id"])
    assert stored["plan"]["state"] == "confirmed"
    assert stored["summary"]["adopted_new_rows"] == 7
    assert read_plan(database, second["plan_id"])["summary"]["adopted_new_rows"] == 4


def test_tampered_plan_cannot_pass_verification(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "conflict"))
    database = tmp_path / "takeover.sqlite3"
    result = plan(ledger, OLD, BATCHES["conflict"][0], "partitioned_cutover")
    save_plan(database, result)
    contract = load_contract(CONTRACT)
    assert verify_plan(contract, ledger, database, result["plan_id"])["ok"] is True
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT ordinal FROM takeover_partition WHERE plan_id=? "
            "ORDER BY ordinal", (result["plan_id"],)).fetchall()
        target = rows[-1][0]
        connection.execute(
            "UPDATE takeover_partition SET conclusion='adopted_new', "
            "reason='all_rows_verified' WHERE plan_id=? AND ordinal=?",
            (result["plan_id"], target))
        connection.execute(
            "UPDATE takeover_plan SET summary_json=? WHERE plan_id=?",
            (json.dumps({**result["summary"], "adopted_new_rows": 7}),
             result["plan_id"]))
    verification = verify_plan(contract, ledger, database, result["plan_id"])
    assert verification["ok"] is False
    assert any(item["code"] == "DIM-TO-010" for item in verification["findings"])


def test_plan_conflict_on_same_id_never_overwrites(tmp_path):
    database = tmp_path / "takeover.sqlite3"
    fake = {"plan_id": "to-deadbeef", "fingerprint": "a" * 64,
            "policy": "atomic_cutover", "state": "confirmed",
            "contract": {"fingerprint": "c" * 64},
            "events": {"sha256": "e" * 64}, "events_path": "events.json",
            "generated_epoch": 1,
            "receipts": {"old": {"batch_id": "o", "producer": "p",
                                 "manifest_sha": "m", "source_root": ".",
                                 "files": []},
                         "new": {"batch_id": "n", "producer": "p",
                                 "manifest_sha": "m", "source_root": ".",
                                 "files": []}},
            "summary": {"adopted_new_rows": 0}, "partitions": []}
    assert save_plan(database, fake) is True
    changed = {**fake, "fingerprint": "b" * 64}
    with pytest.raises(ValueError, match="takeover_plan_identity_conflict"):
        save_plan(database, changed)


def test_cli_round_trip(ledger, tmp_path, capsys):
    from dimcheck.cli import main
    register_batches(ledger.parent, ledger, ("old", "new"))
    out = tmp_path / "out"
    code = main(["takeover", "--contract", str(CONTRACT), "--ledger", str(ledger),
                 "--receipt", OLD, "--receipt-root", str(TAKEOVER),
                 "--receipt", BATCHES["new"][0], "--receipt-root", str(TAKEOVER),
                 "--events", str(TAKEOVER / "transfers.json"),
                 "--policy", "partitioned_cutover", "--out", str(out)])
    assert code == 0
    payload = json.loads((out / "takeover.json").read_text(encoding="utf-8"))
    assert payload["state"] == "confirmed"
    assert (out / "takeover.md").is_file()
    html = (out / "takeover.html").read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html
    assert str(TAKEOVER) not in (out / "takeover.json").read_text(encoding="utf-8")
    verify_code = main(["takeover-verify", "--contract", str(CONTRACT),
                        "--ledger", str(ledger),
                        "--database", str(out / "takeover.sqlite3"),
                        "--out", str(out)])
    assert verify_code == 0
    assert "verify ok" in capsys.readouterr().out


def test_cli_pending_exit_code(ledger, tmp_path):
    from dimcheck.cli import main
    register_batches(ledger.parent, ledger, ("old", "new", "conflict"))
    blocked = main(["takeover", "--contract", str(CONTRACT), "--ledger", str(ledger),
                    "--receipt", OLD, "--receipt-root", str(TAKEOVER),
                    "--receipt", BATCHES["new"][0], "--receipt-root", str(TAKEOVER),
                    "--events", str(TAKEOVER / "transfers-overlap.json"),
                    "--out", str(tmp_path / "blocked")])
    assert blocked == 2  # a malformed event file blocks the whole plan
    conflict = main(["takeover", "--contract", str(CONTRACT), "--ledger", str(ledger),
                     "--receipt", OLD, "--receipt-root", str(TAKEOVER),
                     "--receipt", BATCHES["conflict"][0], "--receipt-root", str(TAKEOVER),
                     "--events", str(TAKEOVER / "transfers.json"),
                     "--policy", "partitioned_cutover",
                     "--out", str(tmp_path / "conflict")])
    assert conflict == 3  # row-level pending evidence, partitions still split
