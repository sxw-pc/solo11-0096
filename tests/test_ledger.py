import sqlite3

from dimcheck.cli import run_once
from dimcheck.ledger import DB_NAME, read_findings, read_ledger, read_metrics, read_runs

FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def test_runs_append_without_overwriting(project, tmp_path):
    out = tmp_path / "out"
    first, _ = run_once(project.contract, out, quiet=True)
    second, _ = run_once(project.contract, out, quiet=True)
    runs = read_runs(out)
    assert len(runs) == 2
    assert runs[0]["pk"] > runs[1]["pk"]
    assert runs[0]["run_id"] == first["run_id"] == second["run_id"]
    assert (out / DB_NAME).is_file()
    ledger = read_ledger(out)
    assert len(ledger["runs"]) == 2
    assert ledger["latest_findings"] == []


def test_findings_and_metric_snapshots_are_stored(project, tmp_path):
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, "", 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90]],
    )
    out = tmp_path / "out"
    report, _ = run_once(project.contract, out, quiet=True)
    run_pk = read_runs(out)[0]["pk"]
    findings = read_findings(out, run_pk)
    assert [item["code"] for item in findings] == ["DIM-MEA-006"]
    assert findings[0]["evidence"]["null_count"] == 1
    assert findings[0]["ordinal"] == 0
    assert report["summary"]["warnings"] == 1
    with sqlite3.connect(out / DB_NAME) as connection:
        count = connection.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 5
    assert runs == 1
    metrics = read_metrics(out, run_pk)
    assert [item["metric_name"] for item in metrics] == [
        "conform", "fanout", "keys", "measures", "refs"
    ]
    assert read_ledger(out)["latest_metrics"]["measures"]["summary"]["total"] == 5


def test_missing_ledger_reads_as_empty(tmp_path):
    assert read_runs(tmp_path) == []
    assert read_findings(tmp_path, 1) == []
    assert read_ledger(tmp_path) == {
        "runs": [], "latest_findings": [], "latest_metrics": {}
    }
