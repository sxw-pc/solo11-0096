from pathlib import Path

import pytest

from dimcheck.attribution_capture import load_case


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "fixtures" / "revenue_bridge" / "case.json"


def test_revenue_bridge_case_is_complete_and_unattributed():
    case = load_case(CASE)
    assert case.case_id == "fare-revenue-bridge-01"
    assert len(case.rows) == 4
    assert len(case.sparse_rows) == 3
    assert [r["transaction_count"] for r in case.rows] == [80, 20, 50, 100]
    assert [str(r["revenue_amount"]) for r in case.rows] == ["160.00", "80.00", "120.00", "360.00"]
    assert len(case.digest) == 64


def test_revenue_bridge_rejects_outside_manifest(tmp_path):
    outside = tmp_path / "case.json"
    outside.write_bytes(CASE.read_bytes())
    with pytest.raises(ValueError, match="inside repository"):
        load_case(outside)
