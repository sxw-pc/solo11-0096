from dimcheck.contract import load_contract
from dimcheck.fanout import check_fanout
from dimcheck.sources import load_dataset

HEADER = ["customer_key", "customer_id", "segment"]


def _check(project):
    contract = load_contract(project.contract)
    return check_fanout(contract, load_dataset(contract))


def test_clean_joins_do_not_amplify(project):
    findings, metrics = _check(project)
    assert findings == []
    assert metrics["max_measured_amplification"] == 1.0
    assert metrics["risky_join_count"] == 0
    for item in metrics["joins"].values():
        assert item["measured_amplification"] == 1.0
        assert item["risk"] == "none"
        assert item["extra_rows"] == 0


def test_duplicate_dimension_key_causes_medium_fanout(project):
    project.write_csv(
        "dim_customer.csv", HEADER,
        [[1, "C001", "retail"], [1, "C001B", "retail"], [2, "C002", "corp"],
         [3, "C003", "public"]],
    )
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-FAN-001"]
    assert findings[0].severity == "warning"
    item = metrics["joins"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert item["risk"] == "medium"
    assert item["measured_amplification"] == 1.666667
    assert item["estimated_amplification"] == 1.333333
    assert item["extra_rows"] == 2
    assert findings[0].evidence["referenced_duplicate_keys"] == 1


def test_high_fanout_grade(project):
    project.write_csv(
        "dim_customer.csv", HEADER,
        [[1, "C001", "retail"], [1, "C001B", "retail"], [1, "C001C", "retail"],
         [2, "C002", "corp"], [3, "C003", "public"]],
    )
    findings, metrics = _check(project)
    item = metrics["joins"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert item["risk"] == "high"
    assert item["measured_amplification"] == 2.333333
    assert findings[0].evidence["risk"] == "high"


def test_latent_duplicate_without_reference_is_low(project):
    project.write_csv(
        "dim_customer.csv", HEADER,
        [[1, "C001", "retail"], [2, "C002", "corp"], [3, "C003", "public"],
         [3, "C003B", "public"]],
    )
    findings, metrics = _check(project)
    assert findings == []
    item = metrics["joins"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert item["risk"] == "low"
    assert item["measured_amplification"] == 1.0
    assert item["duplicate_dimension_keys"] == 1
    assert item["referenced_duplicate_keys"] == 0


def test_unmatched_fact_keys_are_not_counted_as_matches(project):
    project.write_csv(
        "fct_sales.csv",
        ["sale_id", "customer_key", "product_key", "date_key", "amount", "quantity",
         "avg_price", "on_hand"],
        [[1001, 99, 1, 1, 10.0, 2, 5.0, 100]],
    )
    findings, metrics = _check(project)
    assert findings == []
    item = metrics["joins"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert item["matched_fact_rows"] == 0
    assert item["measured_amplification"] is None
    assert item["unmatched_fact_rows"] == 1
