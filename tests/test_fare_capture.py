from pathlib import Path

import pytest

from dimcheck.fare_capture import load_case


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "fixtures" / "fare_cap" / "case.json"


def test_fare_case_freezes_real_dimension_links_and_late_journey():
    case = load_case(CASE)
    assert case.case_id == "afc-two-layer-fare-cap-01"
    assert [j["journey_id"] for j in case.journeys] == ["J1", "J2", "J3", "J0"]
    assert [j["line_key"] for j in case.journeys] == [1, 1, 2, 2]
    assert [j["base_fare_minor"] for j in case.journeys] == [300, 300, 300, 200]
    assert case.policy["eligible_orders"] == ["line_first", "network_first"]
    assert len(case.digest) == 64


def test_fare_case_rejects_manifest_outside_repo(tmp_path):
    outside = tmp_path / "case.json"
    outside.write_bytes(CASE.read_bytes())
    with pytest.raises(ValueError, match="inside repository"):
        load_case(outside)
