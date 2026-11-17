"""The frozen remediation case has two distinct, valid business outcomes."""
from pathlib import Path

import pytest

from dimcheck.fanout import check_fanout
from dimcheck.keys import check_keys
from dimcheck.refs import check_refs
from dimcheck.remediation import _csv, load_case
from dimcheck.sources import Dataset, _build_table


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "fixtures" / "remediation" / "case.json"


def _dataset(case, *, supplement_station=False, supplement_line=False,
             quarantine=False):
    tables = dict(case.observed.tables)
    for dimension, enabled in (("dim_station", supplement_station),
                               ("dim_line", supplement_line)):
        if not enabled:
            continue
        old = tables[dimension]
        additions = _csv(case.supplements[dimension], old.columns, {"", "null"})
        tables[dimension] = _build_table(dimension, old.source, list(old.columns),
                                         list(old.rows) + additions)
    if quarantine:
        for fact, grain in (("fct_ridership", "event_id"),
                            ("fct_fare_revenue", "revenue_id")):
            old = tables[fact]
            kept = [row for row in old.rows if row[grain] not in
                    {"R99001", "R99002", "V99001"}]
            tables[fact] = _build_table(fact, old.source, list(old.columns), kept)
    return Dataset(case.observed.name, tables,
                   sum(len(table.rows) for table in tables.values()),
                   case.observed.max_rows)


def _codes(case, dataset):
    return {name: [item.code for item in checker(case.contract, dataset)[0]]
            for name, checker in (("keys", check_keys), ("refs", check_refs),
                                  ("fanout", check_fanout))}


def test_frozen_case_and_dependency():
    case = load_case(CASE)
    assert case.findings == {"keys": [], "refs": ["DIM-REF-002", "DIM-REF-002"],
                             "fanout": []}
    assert len(case.digest) == 64
    station_only = _codes(case, _dataset(case, supplement_station=True))
    assert "DIM-REF-002" not in station_only["refs"]
    assert "DIM-REF-004" in station_only["refs"]  # line 5 is still missing


def test_valid_supplement_and_quarantine_have_different_coverage():
    case = load_case(CASE)
    supplemented = _dataset(case, supplement_station=True, supplement_line=True)
    quarantined = _dataset(case, quarantine=True)
    assert _codes(case, supplemented) == _codes(case, quarantined) == {
        "keys": [], "refs": [], "fanout": []}
    assert len(supplemented.table("fct_ridership").rows) == 38
    assert len(quarantined.table("fct_ridership").rows) == 36


def test_digest_mismatch_blocks_replay(monkeypatch, tmp_path):
    import dimcheck.remediation as module

    original = module._under
    altered = tmp_path / "station-supplement.csv"
    altered.write_bytes((ROOT / "fixtures" / "remediation" /
                         "station-supplement.csv").read_bytes() + b"\n")

    def replace_one(root, relative):
        if relative == "fixtures/remediation/station-supplement.csv":
            return altered
        return original(root, relative)

    monkeypatch.setattr(module, "_under", replace_one)
    with pytest.raises(ValueError, match="case input changed"):
        load_case(CASE)
