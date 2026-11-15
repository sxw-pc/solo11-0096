from dimcheck.conform import check_conform
from dimcheck.contract import load_contract
from dimcheck.sources import load_dataset

CUSTOMER_HEADER = ["customer_key", "customer_id", "segment"]


def _check(project):
    contract = load_contract(project.contract)
    return check_conform(contract, load_dataset(contract))


def _add_alt(project, attrs=None, rows=None, group="party"):
    for dimension in project.model["dimensions"]:
        if dimension["name"] == "dim_customer":
            dimension["conformed_group"] = group
    project.model["dimensions"].append({
        "name": "dim_customer_alt", "source": "dim_customer_alt.csv", "format": "csv",
        "conformed_group": group, "surrogate_key": "customer_key",
        "natural_keys": ["customer_id"],
        "attributes": attrs or [{"name": "customer_key", "type": "integer"},
                                {"name": "customer_id", "type": "string"},
                                {"name": "segment", "type": "string"}],
    })
    project.write_csv(
        "dim_customer_alt.csv", CUSTOMER_HEADER,
        rows or [[1, "C001", "retail"], [2, "C002", "corp"], [3, "C003", "public"]],
    )
    project.save()


def test_clean_conformity_and_bus_matrix(project):
    findings, metrics = _check(project)
    assert findings == []
    assert metrics["coverage"] == 1.0
    assert metrics["unused_dimensions"] == []
    assert metrics["bus_matrix"]["fct_sales"]["dimensions"] == [
        "dim_customer", "dim_date", "dim_product"
    ]


def test_conformed_attribute_set_mismatch(project):
    _add_alt(project, attrs=[{"name": "customer_key", "type": "integer"},
                             {"name": "customer_id", "type": "string"}])
    findings, metrics = _check(project)
    codes = [item.code for item in findings]
    assert "DIM-CON-001" in codes
    assert metrics["conformed_groups"]["party"]["issues"] >= 1


def test_conformed_type_mismatch(project):
    _add_alt(project, attrs=[{"name": "customer_key", "type": "integer"},
                             {"name": "customer_id", "type": "string"},
                             {"name": "segment", "type": "integer"}])
    findings, _ = _check(project)
    assert "DIM-CON-002" in [item.code for item in findings]


def test_conformed_domain_mismatch_is_warning(project):
    _add_alt(project, rows=[[1, "C901", "retail"], [2, "C902", "corp"],
                            [3, "C903", "public"]])
    findings, _ = _check(project)
    domain = [item for item in findings if item.code == "DIM-CON-003"]
    assert len(domain) == 1
    assert domain[0].severity == "warning"
    assert domain[0].evidence["baseline_distinct"] == 3


def test_repeated_dimension_requires_role(project):
    project.model["facts"][0]["refs"].append({
        "dimension": "dim_customer", "fact_key": "customer_key",
        "dimension_key": "customer_key",
    })
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-004"]


def test_duplicate_role_in_fact(project):
    project.model["facts"][0]["refs"].append({
        "dimension": "dim_customer", "fact_key": "customer_key",
        "dimension_key": "customer_key", "role": "buyer",
    })
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-005"]


def test_role_name_collision_across_dimensions(project):
    project.model["facts"][0]["refs"][1]["role"] = "buyer"
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-007"]


def test_role_declaration_inconsistent_across_facts(project):
    project.model["facts"].append({
        "name": "fct_returns", "source": "fct_sales.csv", "format": "csv",
        "grain": ["sale_id"],
        "refs": [{"dimension": "dim_customer", "fact_key": "customer_key",
                  "dimension_key": "customer_key"}],
        "measures": [{"name": "amount", "column": "amount", "agg": "sum",
                      "additivity": "additive"}],
    })
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-006"]
    assert findings[0].evidence["facts_without_roles"] == ["fct_returns"]


def test_fact_without_dimensions_warns(project):
    project.model["facts"].append({
        "name": "fct_unrelated", "source": "fct_sales.csv", "format": "csv",
        "grain": ["sale_id"], "refs": [],
        "measures": [{"name": "amount", "column": "amount", "agg": "sum",
                      "additivity": "additive"}],
    })
    project.save()
    findings, _ = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-008"]


def test_unused_dimension_is_info(project):
    project.model["dimensions"].append({
        "name": "dim_unused", "source": "dim_unused.csv", "format": "csv",
        "surrogate_key": "id", "natural_keys": ["id"],
        "attributes": [{"name": "id", "type": "integer"}],
    })
    project.write_csv("dim_unused.csv", ["id"], [[1], [2]])
    project.save()
    findings, metrics = _check(project)
    assert [item.code for item in findings] == ["DIM-CON-009"]
    assert findings[0].severity == "info"
    assert metrics["unused_dimensions"] == ["dim_unused"]


def test_snowflake_parent_counts_as_used(project):
    project.model["dimensions"].append({
        "name": "dim_region", "source": "dim_region.csv", "format": "csv",
        "surrogate_key": "region_key", "natural_keys": ["region_code"],
        "attributes": [{"name": "region_key", "type": "integer"}],
    })
    customer = next(item for item in project.model["dimensions"]
                    if item["name"] == "dim_customer")
    customer["snowflake"] = {"parent": "dim_region", "child_key": "region_key",
                             "parent_key": "region_key"}
    project.write_csv("dim_region.csv", ["region_key"], [[1], [2]])
    project.save()
    findings, metrics = _check(project)
    assert findings == []
    assert "dim_region" in metrics["used_dimensions"]
    assert metrics["unused_dimensions"] == []
