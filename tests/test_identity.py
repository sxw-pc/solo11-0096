from pathlib import Path

from dimcheck.identity import register_case


ROOT = Path(__file__).resolve().parents[1]


def test_identity_fixture_freezes_ambiguous_alias_graph():
    case = register_case(ROOT / "fixtures/identity/design.json")
    assert case["left"] == "dim_boarding_station"
    assert case["right"] == "dim_finance_station"
    assert case["aliases"] == [
        ("a", "x"), ("b", "y"), ("b", "z"), ("c", "y"), ("c", "z")
    ]
    assert "DIM-CON-003" in case["existing_findings"]
