from dimcheck.contract import load_contract
from dimcheck.measures import check_measures
from dimcheck.sources import load_dataset

HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
          "quantity", "avg_price", "on_hand"]
ROWS = [[1001, 1, 1, 1, 10.0, 2, 5.0, 100],
        [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
        [1003, 1, 3, 3, 7.5, 3, 2.5, 90]]


def _check(project):
    contract = load_contract(project.contract)
    return check_measures(contract, load_dataset(contract))


def test_clean_aggregation_matrix(project):
    findings, metrics = _check(project)
    assert findings == []
    assert metrics["summary"] == {"total": 5, "safe_to_sum": 2, "unsafe_to_sum": 3}
    assert metrics["measures"]["fct_sales.amount"]["safe_to_sum"] is True
    assert metrics["measures"]["fct_sales.on_hand"]["safe_to_sum"] is False
    assert metrics["measures"]["fct_sales.on_hand"]["safe_aggregations"] == [
        "sum_by_non_axis_dimensions"
    ]
    assert metrics["measures"]["fct_sales.avg_unit_price"]["safe_to_sum"] is False
    assert metrics["measures"]["fct_sales.avg_unit_price"]["safe_aggregations"][-1] == (
        "derive_ratio"
    )


def test_non_additive_cannot_sum(project):
    project.model["facts"][0]["measures"][2]["agg"] = "sum"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-002"]
    assert findings[0].severity == "error"


def test_additive_must_sum(project):
    project.model["facts"][0]["measures"][0]["agg"] = "avg"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-002"]


def test_semi_additive_axis_must_be_referenced(project):
    project.model["facts"][0]["measures"][4]["semi_additive_axis"] = "dim_missing"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-003"]
    assert findings[0].evidence["axis"] == "dim_missing"


def test_semi_additive_axis_is_required(project):
    project.model["facts"][0]["measures"][4].pop("semi_additive_axis")
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-003"]


def test_ratio_requires_ratio_aggregation(project):
    project.model["facts"][0]["measures"][3]["agg"] = "sum"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-002"]


def test_ratio_component_must_be_additive(project):
    project.model["facts"][0]["measures"][3]["denominator"] = "avg_price"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-007"]


def test_missing_measure_column(project):
    project.model["facts"][0]["measures"][0]["column"] = "missing_amount"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-001"]


def test_non_numeric_measure_column(project):
    rows = [list(row) for row in ROWS]
    rows[0][5] = "many"
    project.write_csv("fct_sales.csv", HEADER, rows)
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-005"]
    assert findings[0].evidence["type"] == "mixed"


def test_null_in_additive_measure_warns(project):
    project.write_csv(
        "fct_sales.csv", HEADER,
        ROWS + [[1004, 1, 1, 1, "", 1, 3.0, 80]],
    )
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-006"]
    assert findings[0].evidence["null_count"] == 1


def test_zero_denominator_warns(project):
    rows = [list(row) for row in ROWS]
    rows[0][5] = 0
    project.write_csv("fct_sales.csv", HEADER, rows)
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-MEA-007"]
    assert "分母" in findings[0].message
    assert metrics["measures"]["fct_sales.avg_unit_price"]["safe_to_sum"] is False
