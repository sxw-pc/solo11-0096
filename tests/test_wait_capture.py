from pathlib import Path

import pytest

from dimcheck.wait_capture import load_case


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "fixtures" / "wait_tail" / "case.json"


def test_wait_case_matches_reported_mean_and_full_roster():
    case = load_case(CASE)
    assert case.case_id == "station-wait-tail-01"
    assert case.fact_row["event_id"] == "R00003"
    assert case.fact_row["ride_count"] == 32
    assert case.fact_row["avg_wait_seconds"] == 31.5
    assert sum(r["wait_lower_seconds"] for r in case.observations if r["observation"] == "boarded") == 800
    assert sum(r["wait_upper_seconds"] for r in case.observations if r["observation"] == "boarded") == 1400
    assert [r["wait_lower_seconds"] for r in case.observations
            if r["observation"] == "right_censored"] == [180, 190]
    assert len(case.observations) == 34
    assert len(case.digest) == 64


def test_wait_case_rejects_outside_manifest(tmp_path):
    outside = tmp_path / "case.json"
    outside.write_bytes(CASE.read_bytes())
    with pytest.raises(ValueError, match="inside repository"):
        load_case(outside)
