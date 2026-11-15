from dimcheck.contract import load_contract
from dimcheck.refs import check_refs
from dimcheck.sources import load_dataset

FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def _check(project):
    contract = load_contract(project.contract)
    return check_refs(contract, load_dataset(contract))


def _write_fact(project, customer_keys):
    rows = []
    for index, key in enumerate(customer_keys, start=1):
        rows.append([1000 + index, key, min(index, 3), min(index, 3),
                     10.0, 1, 10.0, 100])
    project.write_csv("fct_sales.csv", FACT_HEADER, rows)


def test_clean_references(project):
    findings, metrics = _check(project)
    assert findings == []
    assert all(item["matched_rows"] == 3 for item in metrics["references"].values())
    assert metrics["snowflake"] == {}


def test_orphan_foreign_key(project):
    _write_fact(project, [99, 2, 3])
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-002"]
    assert findings[0].severity == "error"
    assert findings[0].evidence["unknown_count"] == 1
    metric = metrics["references"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert metric["unknown_keys"] == 1


def test_optional_orphan_and_whitelist(project):
    ref = project.model["facts"][0]["refs"][0]
    ref["optional"] = True
    ref["unknown_members"] = [99]
    project.save()
    _write_fact(project, [99, 2, 3])
    findings, metrics = _check(project)
    assert findings == []
    metric = metrics["references"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert metric["whitelisted_keys"] == 1
    assert metric["unknown_rows"] == 0


def test_optional_orphan_still_warns(project):
    project.model["facts"][0]["refs"][0]["optional"] = True
    project.save()
    _write_fact(project, [99, 2, 3])
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-002"]
    assert findings[0].severity == "warning"


def test_required_null_foreign_key(project):
    _write_fact(project, ["", 2, 3])
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-003"]
    metric = metrics["references"]["fct_sales.customer_key->dim_customer.customer_key"]
    assert metric["null_rows"] == 1


def test_missing_reference_column(project):
    project.write_csv(
        "fct_sales.csv", ["sale_id", "product_key", "date_key", "amount", "quantity",
                          "avg_price", "on_hand"],
        [[1001, 1, 1, 10.0, 2, 5.0, 100]],
    )
    findings, metrics = _check(project)
    assert "DIM-REF-001" in [item.code for item in findings]
    assert metrics["references"][
        "fct_sales.customer_key->dim_customer.customer_key"
    ]["missing_columns"] == ["customer_key"]


def _add_region(project, customer_keys, region_rows=None, with_column=True):
    project.model["dimensions"].append({
        "name": "dim_region", "source": "dim_region.csv", "format": "csv",
        "surrogate_key": "region_key", "natural_keys": ["region_code"],
        "attributes": [{"name": "region_key", "type": "integer"},
                       {"name": "region_code", "type": "string"}],
    })
    customer = next(item for item in project.model["dimensions"]
                    if item["name"] == "dim_customer")
    customer["snowflake"] = {"parent": "dim_region", "child_key": "region_key",
                             "parent_key": "region_key"}
    header = ["customer_key", "customer_id", "segment"]
    rows = [[1, "C001", "retail"], [2, "C002", "corp"], [3, "C003", "public"]]
    if with_column:
        header.append("region_key")
        for row, region in zip(rows, customer_keys):
            row.append(region)
        customer["attributes"].append({"name": "region_key", "type": "integer"})
    project.write_csv("dim_customer.csv", header, rows)
    project.write_csv("dim_region.csv", ["region_key", "region_code"],
                      region_rows or [[1, "R01"], [2, "R02"]])
    project.save()


def test_snowflake_orphan(project):
    _add_region(project, [1, 1, 9])
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-004"]
    assert findings[0].evidence["orphan_keys"] == [9]
    assert metrics["snowflake"]["dim_customer -> dim_region"]["orphan_rows"] == 1


def test_snowflake_missing_column(project):
    _add_region(project, [1, 1, 1], with_column=False)
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-004"]
    assert findings[0].evidence["missing"][0]["column"] == "region_key"


def test_snowflake_parent_key_duplicate(project):
    _add_region(project, [1, 1, 2], region_rows=[[1, "R01"], [1, "R02"], [2, "R03"]])
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-004"]
    assert metrics["snowflake"]["dim_customer -> dim_region"]["duplicate_parent_keys"] == 1


def test_snowflake_cycle(project):
    project.model["dimensions"].extend([
        {"name": "dim_a", "source": "dim_a.csv", "format": "csv",
         "surrogate_key": "a_key", "natural_keys": ["a_key"],
         "attributes": [{"name": "a_key", "type": "integer"},
                        {"name": "b_key", "type": "integer"}],
         "snowflake": {"parent": "dim_b", "child_key": "b_key", "parent_key": "b_key"}},
        {"name": "dim_b", "source": "dim_b.csv", "format": "csv",
         "surrogate_key": "b_key", "natural_keys": ["b_key"],
         "attributes": [{"name": "b_key", "type": "integer"},
                        {"name": "a_key", "type": "integer"}],
         "snowflake": {"parent": "dim_a", "child_key": "a_key", "parent_key": "a_key"}},
    ])
    project.write_csv("dim_a.csv", ["a_key", "b_key"], [[1, 1]])
    project.write_csv("dim_b.csv", ["b_key", "a_key"], [[1, 1]])
    project.save()
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-REF-005"]
    assert metrics["cycles"] == [["dim_a", "dim_b"]]
