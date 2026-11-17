import json

import pytest

from dimcheck.contract import load_contract
from dimcheck.sources import SourceError, check_declared_schema, load_dataset


def _load(project):
    return load_dataset(load_contract(project.contract))


def test_loads_tables_and_profiles(project):
    dataset = _load(project)
    assert dataset.total_rows == 12
    assert set(dataset.tables) == {"dim_customer", "dim_product", "dim_date", "fct_sales"}
    assert dataset.table("dim_customer").profiles["segment"]["type"] == "string"
    assert dataset.table("dim_date").profiles["calendar_date"]["type"] == "date"
    assert dataset.table("fct_sales").profiles["quantity"]["type"] == "integer"
    assert dataset.table("fct_sales").profiles["amount"]["distinct_count"] == 3


def test_null_normalization(project):
    project.write_csv(
        "fct_sales.csv",
        ["sale_id", "customer_key", "product_key", "date_key", "amount", "quantity",
         "avg_price", "on_hand"],
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90],
         [1004, 1, 1, 1, "NULL", 1, 3.0, 80]],
    )
    table = _load(project).table("fct_sales")
    assert table.rows[-1]["amount"] is None
    assert table.profiles["amount"]["null_count"] == 1
    assert table.profiles["amount"]["distinct_count"] == 3


def test_jsonl_loading_and_column_union(project):
    project.model["dimensions"].append({
        "name": "dim_channel", "source": "dim_channel.jsonl", "format": "jsonl",
        "surrogate_key": "channel_key", "natural_keys": ["channel_code"],
        "attributes": [{"name": "channel_key", "type": "integer"},
                       {"name": "channel_code", "type": "string"},
                       {"name": "note", "type": "string"}],
    })
    project.save()
    lines = [
        {"channel_key": 1, "channel_code": "W01"},
        {"channel_key": 2, "channel_code": "W02", "note": "example"},
    ]
    (project.data / "dim_channel.jsonl").write_text(
        "\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8"
    )
    table = _load(project).table("dim_channel")
    assert table.columns == ("channel_code", "channel_key", "note")
    assert table.rows[0]["note"] is None
    assert table.profiles["note"]["distinct_count"] == 1


def test_row_limit_is_enforced(project):
    project.model["dataset"]["max_rows"] = 1
    project.save()
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-LIM-001"
    assert exc.value.finding.evidence["limit"] == 1


def test_path_traversal_is_rejected(project, tmp_path):
    (tmp_path / "secret.csv").write_text("x\n1\n", encoding="utf-8")
    project.model["dimensions"][0]["source"] = "../secret.csv"
    project.save()
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-PATH-001"


def test_missing_source_is_coded(project):
    (project.data / "dim_product.csv").unlink()
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-PATH-002"


def test_malformed_jsonl(project):
    project.model["dimensions"].append({
        "name": "dim_bad", "source": "bad.jsonl", "format": "jsonl",
        "surrogate_key": "id", "natural_keys": ["id"],
        "attributes": [{"name": "id", "type": "integer"}],
    })
    project.save()
    (project.data / "bad.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-SRC-002"


def test_duplicate_csv_header(project):
    (project.data / "dim_customer.csv").write_text(
        "customer_key,customer_key,segment\n1,1,retail\n", encoding="utf-8"
    )
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-SRC-003"


def test_csv_row_width(project):
    (project.data / "dim_customer.csv").write_text(
        "customer_key,customer_id\n1,C001,extra\n", encoding="utf-8"
    )
    with pytest.raises(SourceError) as exc:
        _load(project)
    assert exc.value.code == "DIM-SRC-002"
    assert exc.value.finding.evidence["line"] == 2


def test_declared_type_mismatch(project):
    project.model["dimensions"][0]["attributes"][2]["type"] = "integer"
    project.save()
    contract = load_contract(project.contract)
    findings = check_declared_schema(contract, load_dataset(contract))
    assert [item.code for item in findings] == ["DIM-SRC-005"]
    assert findings[0].evidence["declared"] == "integer"
    assert findings[0].evidence["observed"] == "string"


def test_empty_source_warning(project):
    project.write_csv(
        "fct_sales.csv",
        ["sale_id", "customer_key", "product_key", "date_key", "amount", "quantity",
         "avg_price", "on_hand"],
        [],
    )
    contract = load_contract(project.contract)
    findings = check_declared_schema(contract, load_dataset(contract))
    assert [item.code for item in findings] == ["DIM-SRC-004"]
    assert findings[0].object_name == "fct_sales"
