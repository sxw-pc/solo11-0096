from dimcheck.contract import load_contract
from dimcheck.keys import check_keys
from dimcheck.sources import load_dataset

CUSTOMER_HEADER = ["customer_key", "customer_id", "segment"]
FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def _check(project):
    contract = load_contract(project.contract)
    return check_keys(contract, load_dataset(contract))


def test_clean_keys(project):
    findings, metrics = _check(project)
    assert findings == []
    assert metrics["facts"]["fct_sales"]["grain_unique"] is True
    assert metrics["dimensions"]["dim_customer"]["surrogate_unique"] is True
    assert metrics["dimensions"]["dim_customer"]["distinct_surrogate_keys"] == 3


def test_duplicate_fact_grain(project):
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100],
         [1001, 2, 2, 2, 99.0, 9, 11.0, 999]],
    )
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-001"]
    assert findings[0].severity == "error"
    assert findings[0].evidence["groups"][0]["count"] == 2
    assert metrics["facts"]["fct_sales"]["grain_duplicate_rows"] == 2


def test_duplicate_dimension_surrogate_key(project):
    project.write_csv(
        "dim_customer.csv", CUSTOMER_HEADER,
        [[1, "C001", "retail"], [2, "C002", "corp"], [2, "C003", "public"]],
    )
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-002"]
    assert metrics["dimensions"]["dim_customer"]["surrogate_duplicate_groups"] == 1


def test_duplicate_natural_key_is_warning(project):
    project.write_csv(
        "dim_customer.csv", CUSTOMER_HEADER,
        [[1, "C001", "retail"], [2, "C001", "corp"], [3, "C003", "public"]],
    )
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-003"]
    assert findings[0].severity == "warning"
    assert metrics["dimensions"]["dim_customer"]["natural_duplicate_groups"] == 1


def test_missing_dimension_key_column(project):
    project.write_csv(
        "dim_customer.csv", ["customer_id", "segment"],
        [["C001", "retail"], ["C002", "corp"], ["C003", "public"]],
    )
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-005"]
    assert findings[0].object_name == "dim_customer.surrogate_key.customer_key"


def test_missing_fact_grain_column(project):
    project.write_csv(
        "fct_sales.csv", FACT_HEADER[1:],
        [[1, 1, 1, 10.0, 2, 5.0, 100]],
    )
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-005"]
    assert findings[0].object_name == "fct_sales.grain.sale_id"


def test_null_dimension_and_fact_keys(project):
    project.write_csv(
        "dim_customer.csv", CUSTOMER_HEADER,
        [[1, "C001", "retail"], ["", "C002", "corp"], [3, "C003", "public"]],
    )
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100],
         ["", 2, 2, 2, 20.0, 4, 5.0, 120]],
    )
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-004", "DIM-KEY-004"]
    assert {item.object_name for item in findings} == {
        "dim_customer.customer_key", "fct_sales.grain"
    }


def test_composite_grain(project):
    project.model["facts"][0]["grain"] = ["customer_key", "date_key"]
    project.write_csv(
        "fct_sales.csv", FACT_HEADER,
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100],
         [1002, 1, 2, 1, 20.0, 4, 5.0, 120]],
    )
    project.save()
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-KEY-001"]
    assert metrics["facts"]["fct_sales"]["grain"] == ["customer_key", "date_key"]
