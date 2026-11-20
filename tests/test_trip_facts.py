"""Integration tests: versioned trip facts, provenance, contract gating, CLI."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dimcheck.cli import main
from dimcheck.tap_index import FareContract, TripError, load_station_index
from dimcheck.trip_facts import FACT_NAME, publish_version, verify_version

FIX = Path(__file__).resolve().parents[1] / "fixtures"
TRIPS = FIX / "trips"
CONTINUITY = TRIPS / "fare-paid-area-continuity.json"
BREAKS = TRIPS / "fare-gate-exit-breaks.json"
THRESHOLD_300 = TRIPS / "fare-transfer-300.json"
UNSPECIFIED = TRIPS / "fare-service-day-unspecified.json"


def publish(taps, contract, out, contract_file=None):
    return publish_version(
        FIX / taps, FIX / "dim_station.csv", FIX / "transfer-links.json",
        FIX / "dim_date.csv", contract, out, contract_file=contract_file)


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_two_contracts_yield_different_correct_fact_shapes(tmp_path):
    continuity = publish("tap-events.jsonl", FareContract.load(CONTINUITY),
                         tmp_path / "out", CONTINUITY)
    breaks = publish("tap-events.jsonl", FareContract.load(BREAKS),
                     tmp_path / "out", BREAKS)
    rows_c = continuity["manifest"]["trips"]
    rows_b = breaks["manifest"]["trips"]
    assert len(rows_c) == 1 and rows_c[0]["segment_count"] == 2
    assert rows_c[0]["transfer_count"] == 1
    assert len(rows_b) == 2
    assert all(row["segment_count"] == 1 and row["transfer_count"] == 0
               for row in rows_b)
    assert {row["trip_id"] for row in rows_c}.isdisjoint(
        row["trip_id"] for row in rows_b)
    for row in rows_c + rows_b:
        assert row["service_date_key"] == 1 and row["origin_station_key"]
        assert row["entry_tap_ids"] and row["exit_tap_ids"]


def test_every_fact_row_traces_to_tap_ids(tmp_path):
    out = publish("tap-events.jsonl", FareContract.load(BREAKS), tmp_path / "out", BREAKS)
    rows = read_csv(out["dir"] / f"{FACT_NAME}.csv")
    tap_ids = {json.loads(line)["event_id"]
               for line in (FIX / "tap-events.jsonl").read_text().splitlines() if line}
    for row in rows:
        traced = set(row["entry_tap_ids"].split(";")) | set(row["exit_tap_ids"].split(";"))
        assert traced and traced <= tap_ids


def test_uncertain_transfer_publishes_zero_facts_and_pending_range(tmp_path):
    out = publish("trips/uncertain-transfer.jsonl", FareContract.load(THRESHOLD_300),
                  tmp_path / "out", THRESHOLD_300)
    manifest = out["manifest"]
    assert manifest["counts"]["legs"] == 2
    assert manifest["counts"]["trip_facts"] == 0
    pending = manifest["pending"]
    assert len(pending) == 1 and pending[0]["reason"] == "transfer_uncertain"
    assert pending[0]["evidence"]["transfer_gap_seconds_window"] == [180, 420]
    assert pending[0]["evidence"]["transfer_limit_seconds"] == 300


def test_shuffled_and_replayed_inputs_are_equivalent(tmp_path):
    base = publish("tap-events.jsonl", FareContract.load(BREAKS),
                   tmp_path / "a", BREAKS)
    shuffled = publish("trips/shuffled.jsonl", FareContract.load(BREAKS),
                       tmp_path / "b", BREAKS)
    replayed = publish("trips/replayed.jsonl", FareContract.load(BREAKS),
                       tmp_path / "c", BREAKS)
    for out in (shuffled, replayed):
        assert out["manifest"]["trips"] == base["manifest"]["trips"]
        assert out["manifest"]["counts"] == base["manifest"]["counts"]


def test_divergent_identity_isolates_and_keeps_other_segment(tmp_path):
    out = publish("trips/divergent-identity.jsonl", FareContract.load(BREAKS),
                  tmp_path / "out", BREAKS)
    manifest = out["manifest"]
    assert manifest["counts"]["conflicts"] == 1
    assert manifest["conflicts"][0]["reason"] == "same_identity_divergent_payload"
    assert [row["entry_tap_ids"] for row in manifest["trips"]] == ["tap-3"]


def test_missing_end_and_midnight_pending_scenarios(tmp_path):
    missing = publish("trips/missing-end.jsonl", FareContract.load(BREAKS),
                      tmp_path / "m", BREAKS)
    assert missing["manifest"]["pending"][0]["reason"] == "missing_partner"
    night = publish("trips/midnight.jsonl", FareContract.load(UNSPECIFIED),
                    tmp_path / "n", UNSPECIFIED)
    assert night["manifest"]["counts"]["trip_facts"] == 0
    assert night["manifest"]["pending"][0]["reason"] == "service_day_unresolved"
    specified = publish("trips/midnight.jsonl", FareContract.load(BREAKS),
                        tmp_path / "d", BREAKS)
    assert specified["manifest"]["trips"][0]["service_date_key"] == 1


def test_ambiguous_fixture_never_greedy(tmp_path):
    out = publish("trips/ambiguous-pairing.jsonl", FareContract.load(BREAKS),
                  tmp_path / "out", BREAKS)
    assert out["manifest"]["counts"]["trip_facts"] == 0
    assert out["manifest"]["pending"][0]["reason"] == "ambiguous_pairing"


def test_station_code_conflict_rejected(tmp_path):
    conflict = tmp_path / "stations.csv"
    conflict.write_text(
        "station_key,station_code,station_name,line_key\n"
        "1,S-A01,甲一,1\n9,S-A01,漂移,1\n2,S-A02,甲二,1\n", encoding="utf-8")
    with pytest.raises(TripError, match="DIM-TAP-001"):
        load_station_index(conflict)


def test_version_changes_with_bytes_and_never_overwrites(tmp_path):
    first = publish("tap-events.jsonl", FareContract.load(CONTINUITY),
                    tmp_path / "out", CONTINUITY)
    again = publish("tap-events.jsonl", FareContract.load(CONTINUITY),
                    tmp_path / "out", CONTINUITY)
    assert first["version"] == again["version"]  # idempotent replay
    breaks = publish("tap-events.jsonl", FareContract.load(BREAKS),
                     tmp_path / "out", BREAKS)
    assert breaks["version"] != first["version"]
    changed_taps = tmp_path / "changed.jsonl"
    changed_taps.write_text(
        (FIX / "tap-events.jsonl").read_text().replace("08:25:00Z", "08:26:00Z"),
        encoding="utf-8")
    changed = publish_version(changed_taps, FIX / "dim_station.csv",
                              FIX / "transfer-links.json", FIX / "dim_date.csv",
                              FareContract.load(CONTINUITY), tmp_path / "out",
                              contract_file=CONTINUITY)
    assert changed["version"] != first["version"]
    index = json.loads((tmp_path / "out" / "versions.json").read_text(encoding="utf-8"))
    assert len(index) == 3 and all(item["version"] for item in index)
    # original proof remains on disk
    assert (first["dir"] / "fct_trip.csv").is_file()


def test_contract_verification_gates_publishability(tmp_path):
    good = publish("tap-events.jsonl", FareContract.load(BREAKS),
                   tmp_path / "out", BREAKS)
    verdict = verify_version(good["dir"])
    assert verdict["publishable"] and verdict["verdict"] == "pass"
    csv_path = good["dir"] / f"{FACT_NAME}.csv"
    lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
    row = lines[1].split(",")
    duplicate = list(row)
    duplicate[2] = "99"  # orphan station key, duplicate trip_id grain
    csv_path.write_text("\n".join([lines[0], lines[1], ",".join(duplicate)]) + "\n",
                        encoding="utf-8")
    blocked = verify_version(good["dir"])
    assert not blocked["publishable"] and blocked["exit_code"] == 2
    assert "DIM-KEY-001" in blocked["error_codes"]
    assert "DIM-REF-002" in blocked["error_codes"]


def test_provenance_artifacts_are_complete(tmp_path):
    out = publish("tap-events.jsonl", FareContract.load(CONTINUITY),
                  tmp_path / "out", CONTINUITY)
    version_dir = out["dir"]
    for name in ("fct_trip.csv", "fct_trip.jsonl", "pending.json", "manifest.json",
                 "station-manifest.json", "trip-contract.yaml", "verification.json",
                 "tap-events.jsonl", "transfer-links.json", "dim_station.csv",
                 "dim_date.csv", "fare-paid-area-continuity.json"):
        assert (version_dir / name).is_file(), name
    manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["source_hashes"]) == {
        "taps_sha256", "stations_sha256", "links_sha256", "dates_sha256",
        "fare_contract_sha256"}
    assert manifest["fare_contract"]["kind"] == "paid_area_continuity"


def test_cli_trips_end_to_end(tmp_path, capsys):
    code = main(["trips", "--taps", str(FIX / "tap-events.jsonl"),
                 "--stations", str(FIX / "dim_station.csv"),
                 "--links", str(FIX / "transfer-links.json"),
                 "--dates", str(FIX / "dim_date.csv"),
                 "--fare-contract", str(CONTINUITY),
                 "--out", str(tmp_path / "out")])
    assert code == 0
    printed = capsys.readouterr().out
    assert "publishable=True" in printed and "facts=1" in printed
    code = main(["trips", "--taps", str(FIX / "trips" / "uncertain-transfer.jsonl"),
                 "--stations", str(FIX / "dim_station.csv"),
                 "--links", str(FIX / "transfer-links.json"),
                 "--dates", str(FIX / "dim_date.csv"),
                 "--fare-contract", str(THRESHOLD_300),
                 "--out", str(tmp_path / "out2")])
    assert code == 2


def test_cli_trips_rejects_bad_contract(tmp_path, capsys):
    code = main(["trips", "--taps", str(FIX / "tap-events.jsonl"),
                 "--stations", str(FIX / "dim_station.csv"),
                 "--links", str(FIX / "transfer-links.json"),
                 "--dates", str(FIX / "dim_date.csv"),
                 "--fare-contract", str(FIX / "trips" / "missing.json"),
                 "--out", str(tmp_path / "out")])
    assert code == 1
