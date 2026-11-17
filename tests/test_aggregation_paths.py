"""Regression tests for fact-level aggregation-path adjudication."""
from __future__ import annotations

import csv
import sqlite3

import pytest

from dimcheck.aggregation_paths import check_aggregation_paths
from dimcheck.cli import run_once
from dimcheck.contract import ContractError, load_contract
from dimcheck.ledger import read_aggregation_paths, read_path_verdicts, read_runs
from dimcheck.sources import load_dataset


def _write(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _project(project, rows, *, a_rows=None, paths=None, measures=None,
             extra_ref=None, ref_key="a_key"):
    project.model["dimensions"] = [
        {"name": name, "source": f"{name}.csv", "format": "csv",
         "surrogate_key": f"{name[4:]}_key", "natural_keys": [f"{name[4:]}_code"],
         "attributes": [{"name": f"{name[4:]}_key", "type": "integer"},
                        {"name": f"{name[4:]}_code", "type": "string"}]}
        for name in ("dim_a", "dim_b")
    ]
    refs = [
        {"dimension": "dim_a", "fact_key": ref_key, "dimension_key": "a_key",
         "role": "first"},
        {"dimension": "dim_b", "fact_key": "b_key", "dimension_key": "b_key",
         "role": "second"},
    ]
    if extra_ref:
        refs.append(extra_ref)
    default_paths = [_path("inner", ("first", "second")),
                     _path("left", ("first", "second"), retain="left")]
    project.model["facts"] = [{
        "name": "fct_path", "source": "fct_path.csv", "format": "csv",
        "grain": ["row_id"], "refs": refs,
        "measures": measures or [{"name": "amount", "column": "amount", "agg": "sum",
                                  "additivity": "additive"}],
        "aggregation_paths": paths if paths is not None else default_paths}]
    _write(project.data / "dim_a.csv", ["a_key", "a_code"],
           a_rows or [[1, "A1"], [2, "A2"]])
    _write(project.data / "dim_b.csv", ["b_key", "b_code"], [[2, "B2"], [3, "B3"]])
    _write(project.data / "fct_path.csv", ["row_id", "a_key", "b_key", "amount"], rows)
    project.save()
    return project


def _check(project):
    contract = load_contract(project.contract)
    return check_aggregation_paths(contract, load_dataset(contract))


def _path(name, axes=("first",), retain="inner", measure="amount"):
    return {"name": name, "measure": measure, "axes": list(axes), "retain": retain}


# row 1: first axis has two matches and second has none; row 2: both match once;
# row 3: first axis has no match and second has one.
COUNTEREXAMPLE = [[1, 1, 99, 10.0], [2, 2, 2, 20.0], [3, 99, 3, 30.0]]
DUPLICATED_A = [[1, "A1"], [1, "A1B"], [2, "A2"]]


def test_inner_versus_left_business_verdicts(project):
    _project(project, COUNTEREXAMPLE, a_rows=DUPLICATED_A)
    findings, metrics = _check(project)
    paths = metrics["paths"]
    assert paths["fct_path.inner"]["verdict"] == "incomplete"
    assert paths["fct_path.inner"]["counts"] == {
        "fact_rows": 3, "participating_rows": 1, "lost_rows": 2,
        "amplified_rows": 0, "total_contributions": 1}
    assert paths["fct_path.left"]["verdict"] == "amplified"
    assert paths["fct_path.left"]["counts"] == {
        "fact_rows": 3, "participating_rows": 3, "lost_rows": 0,
        "amplified_rows": 1, "total_contributions": 4}
    assert [item.code for item in findings] == ["DIM-AGG-011", "DIM-AGG-010"]
    assert metrics["summary"]["incomplete"] == 1
    assert metrics["summary"]["amplified"] == 1


def test_evidence_samples_are_limited_and_row_ordinal_identified(project):
    rows = [list(row) for row in COUNTEREXAMPLE]
    rows.extend([index, 1, 99, float(index)] for index in range(4, 12))
    _project(project, rows, a_rows=DUPLICATED_A)
    _, metrics = _check(project)
    inner = metrics["paths"]["fct_path.inner"]
    assert inner["counts"]["lost_rows"] == 10
    assert len(inner["evidence"]["lost_rows"]) == 5
    assert inner["evidence"]["lost_rows"][0] == {
        "row_ordinal": 0, "grain": {"row_id": 1}, "contributions": 0}
    amplified = metrics["paths"]["fct_path.left"]["evidence"]["amplified_rows"]
    assert amplified[0] == {"row_ordinal": 0, "grain": {"row_id": 1}, "contributions": 2}


def test_non_unique_grain_still_distinguishes_duplicate_rows(project):
    rows = [[7, 1, 99, 10.0], [7, 1, 99, 11.0], [7, 2, 2, 12.0]]
    _project(project, rows, a_rows=DUPLICATED_A, paths=[_path("left", retain="left")])
    _, metrics = _check(project)
    entry = metrics["paths"]["fct_path.left"]
    amplified = entry["evidence"]["amplified_rows"]
    assert [item["row_ordinal"] for item in amplified] == [0, 1]
    assert all(item["grain"] == {"row_id": 7} for item in amplified)
    assert entry["counts"]["amplified_rows"] == 2


def test_lost_and_amplified_are_mixed_not_netted(project):
    # loss (2) cancels amplification (2) in the total; still not safe
    rows = [[1, 1, 2, 1.0], [2, 2, 2, 2.0], [3, 99, 2, 3.0],
            [4, 1, 2, 4.0], [5, 98, 2, 5.0]]
    _project(project, rows, a_rows=DUPLICATED_A)
    findings, metrics = _check(project)
    entry = metrics["paths"]["fct_path.inner"]
    assert entry["counts"] == {"fact_rows": 5, "participating_rows": 3, "lost_rows": 2,
                               "amplified_rows": 2, "total_contributions": 5}
    assert entry["counts"]["total_contributions"] == entry["counts"]["fact_rows"]
    assert entry["verdict"] == "mixed"
    assert [item.code for item in findings] == ["DIM-AGG-010", "DIM-AGG-010"]


def test_mixed_when_same_path_loses_and_amplifies(project):
    rows = [[1, 1, 99, 1.0], [2, 2, 99, 2.0], [3, 99, 99, 3.0]]
    _project(project, rows, a_rows=DUPLICATED_A, paths=[_path("single")])
    findings, metrics = _check(project)
    entry = metrics["paths"]["fct_path.single"]
    assert (entry["counts"]["lost_rows"], entry["counts"]["amplified_rows"]) == (1, 1)
    assert entry["verdict"] == "mixed" and [f.code for f in findings] == ["DIM-AGG-010"]


def test_ratio_and_non_additive_are_blocked_even_with_one_row(project):
    measures = [
        {"name": "amount", "column": "amount", "agg": "sum", "additivity": "additive"},
        {"name": "avg_amount", "column": "amount", "agg": "avg",
         "additivity": "non_additive"},
        {"name": "rate", "agg": "ratio", "additivity": "ratio",
         "numerator": "amount", "denominator": "amount"}]
    paths = [_path("ratio_path", measure="rate"),
             _path("avg_path", measure="avg_amount"), _path("ok_path")]
    _project(project, [[1, 1, 2, 5.0]], paths=paths, measures=measures)
    findings, metrics = _check(project)
    assert metrics["paths"]["fct_path.ratio_path"]["reason"] == "ratio_direct_sum"
    assert metrics["paths"]["fct_path.avg_path"]["verdict"] == "measure_blocked"
    assert metrics["paths"]["fct_path.ok_path"]["verdict"] == "safe"
    assert [item.code for item in findings] == ["DIM-AGG-012", "DIM-AGG-012"]


def test_semi_additive_axis_blocks_only_that_axis(project):
    measures = [{"name": "balance", "column": "amount", "agg": "sum",
                 "additivity": "semi_additive", "semi_additive_axis": "dim_a"}]
    _project(project, [[1, 1, 2, 5.0]],
             paths=[_path("on_axis", measure="balance"),
                    _path("off_axis", axes=("second",), measure="balance")],
             measures=measures)
    _, metrics = _check(project)
    blocked = metrics["paths"]["fct_path.on_axis"]
    assert (blocked["verdict"], blocked["reason"]) == (
        "measure_blocked", "semi_additive_axis_in_path")
    assert metrics["paths"]["fct_path.off_axis"]["verdict"] == "safe"


@pytest.mark.parametrize("changes, expected", [
    ({"ref_key": "absent_key"}, "missing_column"),
    ({"measures": [{"name": "amount", "column": "not_here", "agg": "sum",
                    "additivity": "additive"}]}, "missing_measure_column"),
])
def test_unreliable_prerequisites_are_indeterminate(project, changes, expected):
    _project(project, [[1, 1, 2, 5.0]], paths=[_path("p")], **changes)
    findings, metrics = _check(project)
    entry = metrics["paths"]["fct_path.p"]
    assert (entry["verdict"], entry["reason"]) == ("indeterminate", expected)
    assert entry["prerequisite"]["state"] == "indeterminate"
    assert [item.code for item in findings] == ["DIM-AGG-013"]


@pytest.mark.parametrize("paths, extra_ref, expected", [
    ([_path("dup"), _path("dup", axes=("second",))], None, "DIM-AGG-002"),
    ([_path("p", measure="missing")], None, "DIM-AGG-003"),
    ([_path("p", axes=("dim_a",))],
     {"dimension": "dim_a", "fact_key": "a_key", "dimension_key": "a_key",
      "role": "other_a"}, "DIM-AGG-005"),
    ([_path("p", axes=("first",))],
     {"dimension": "dim_a", "fact_key": "a_key", "dimension_key": "a_key",
      "role": "first"}, "DIM-AGG-005"),
    ([_path("p", axes=("dim_missing",))], None, "DIM-AGG-004"),
    ([_path("p", axes=("first", "first"))], None, "DIM-AGG-006"),
    ([_path("p", axes=("dim_a", "first"))], None, "DIM-AGG-006"),
    ([_path("p", retain="outer")], None, "DIM-AGG-007"),
    ([{"name": "p", "measure": "amount", "axes": [], "retain": "inner"}],
     None, "DIM-AGG-001"),
])
def test_contract_rejects_invalid_path_declarations(project, paths, extra_ref, expected):
    _project(project, [[1, 1, 2, 5.0]], paths=paths, extra_ref=extra_ref)
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert [item.code for item in exc.value.findings] == [expected]


def test_contract_without_paths_stays_compatible(project):
    assert load_contract(project.contract).facts[0].aggregation_paths == ()
    contract = load_contract(project.contract)
    findings, metrics = check_aggregation_paths(contract, load_dataset(contract))
    assert findings == [] and metrics["paths"] == {} and metrics["summary"]["unsafe"] == 0


def test_dimension_name_axis_allowed_when_referenced_once(project):
    _project(project, [[1, 1, 2, 5.0]], paths=[_path("p", axes=("dim_a",))])
    _, metrics = _check(project)
    assert metrics["paths"]["fct_path.p"]["verdict"] == "safe"
    assert metrics["paths"]["fct_path.p"]["axis_refs"][0]["role"] == "first"


def test_report_sections_and_output_are_deterministic(project, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    _project(project, COUNTEREXAMPLE, a_rows=DUPLICATED_A)
    first, first_paths = run_once(project.contract, tmp_path / "out1", quiet=True)
    second, second_paths = run_once(project.contract, tmp_path / "out2", quiet=True)
    for key in ("json", "markdown", "html"):
        assert first_paths[key].read_bytes() == second_paths[key].read_bytes(), key
    assert first["metrics"]["aggregation_paths"] == second["metrics"]["aggregation_paths"]
    markdown = first_paths["markdown"].read_text(encoding="utf-8")
    html_text = first_paths["html"].read_text(encoding="utf-8")
    assert "## 聚合路径裁定" in markdown and "fct_path.inner" in markdown
    assert "聚合路径裁定" in html_text and "fct_path.left" in html_text
    assert "<script" not in html_text.lower() and "http://" not in html_text


def test_ledger_appends_path_verdicts_per_run(project, tmp_path):
    _project(project, COUNTEREXAMPLE, a_rows=DUPLICATED_A)
    out = tmp_path / "out"
    first, _ = run_once(project.contract, out, quiet=True)
    second, _ = run_once(project.contract, out, quiet=True)
    runs = read_runs(out)
    rows = read_aggregation_paths(out, runs[0]["pk"])
    assert len(runs) == 2 and rows[0]["run_pk"] == runs[0]["pk"]
    assert [item["path_name"] for item in rows] == ["inner", "left"]
    assert [item["verdict"] for item in rows] == ["incomplete", "amplified"]
    assert rows[0]["run_id"] == first["run_id"] == second["run_id"]
    history = read_path_verdicts(out, first["run_id"])
    assert len(history) == 4
    assert {item["run_pk"] for item in history} == {runs[0]["pk"], runs[1]["pk"]}
    assert read_aggregation_paths(out, runs[1]["pk"])[0]["verdict"] == "incomplete"


LEGACY_SCHEMA = """
CREATE TABLE runs (pk INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  tool_version TEXT NOT NULL, contract_fingerprint TEXT NOT NULL,
  started_at TEXT NOT NULL, duration_ms INTEGER NOT NULL, exit_code INTEGER NOT NULL,
  verdict TEXT NOT NULL, errors INTEGER NOT NULL, warnings INTEGER NOT NULL,
  infos INTEGER NOT NULL, report_epoch INTEGER NOT NULL);
INSERT INTO runs VALUES (1,'legacy-run','0.0.9','old','2020-01-01T00:00:00Z',1,0,
  'pass',0,0,0,1600000000);
"""


def test_ledger_migrates_legacy_database_without_overwriting(project, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with sqlite3.connect(out / "ledger.sqlite3") as legacy:
        legacy.executescript(LEGACY_SCHEMA)
    _project(project, [[1, 2, 2, 5.0]], paths=[_path("inner", axes=("first", "second"))])
    run_once(project.contract, out, quiet=True)
    runs = read_runs(out)
    assert len(runs) == 2 and runs[1]["run_id"] == "legacy-run"
    with sqlite3.connect(out / "ledger.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM aggregation_path_runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT verdict FROM runs WHERE run_id='legacy-run'").fetchall() == [("pass",)]
    assert [item["verdict"] for item in read_aggregation_paths(out, runs[0]["pk"])] == ["safe"]
