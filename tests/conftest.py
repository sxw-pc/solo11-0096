from __future__ import annotations

import csv
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _dimension(name, key, natural, attrs, **extra):
    return {
        "name": name, "source": f"{name}.csv", "format": "csv",
        "surrogate_key": key, "natural_keys": [natural],
        "attributes": [{"name": item, "type": kind} for item, kind in attrs],
        **extra,
    }


def _base_model(data_dir: Path) -> dict:
    d = lambda name, key, natural, attrs: _dimension(name, key, natural, attrs)
    return {
        "schema_version": 1, "run_epoch": 1767225600,
        "dataset": {"name": "test_dw", "root": str(data_dir), "max_rows": 1000,
                    "null_tokens": ["NULL", "null"]},
        "dimensions": [
            d("dim_customer", "customer_key", "customer_id",
              [("customer_key", "integer"), ("customer_id", "string"),
               ("segment", "string")]),
            d("dim_product", "product_key", "product_id",
              [("product_key", "integer"), ("product_id", "string"),
               ("category", "string")]),
            d("dim_date", "date_key", "date_id",
              [("date_key", "integer"), ("date_id", "string"),
               ("calendar_date", "date")]),
        ],
        "facts": [{
            "name": "fct_sales", "source": "fct_sales.csv", "format": "csv",
            "grain": ["sale_id"],
            "refs": [
                {"dimension": "dim_customer", "fact_key": "customer_key",
                 "dimension_key": "customer_key", "role": "buyer"},
                {"dimension": "dim_product", "fact_key": "product_key",
                 "dimension_key": "product_key", "role": "product"},
                {"dimension": "dim_date", "fact_key": "date_key",
                 "dimension_key": "date_key", "role": "sale_date"},
            ],
            "measures": [
                {"name": "amount", "column": "amount", "agg": "sum",
                 "additivity": "additive"},
                {"name": "quantity", "column": "quantity", "agg": "sum",
                 "additivity": "additive"},
                {"name": "avg_price", "column": "avg_price", "agg": "avg",
                 "additivity": "non_additive"},
                {"name": "avg_unit_price", "agg": "ratio", "additivity": "ratio",
                 "numerator": "amount", "denominator": "quantity"},
                {"name": "on_hand", "column": "on_hand", "agg": "sum",
                 "additivity": "semi_additive", "semi_additive_axis": "dim_date"},
            ],
        }],
    }


@dataclass
class Project:
    root: Path
    data: Path
    contract: Path
    model: dict

    def save(self) -> Path:
        self.contract.write_text(yaml.safe_dump(self.model, sort_keys=False,
                                                allow_unicode=True), encoding="utf-8")
        return self.contract

    def write_csv(self, name: str, header: list[str], rows: list[list]) -> None:
        with (self.data / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    data = tmp_path / "data"
    data.mkdir()
    contract = tmp_path / "contracts" / "model.yaml"
    contract.parent.mkdir()
    result = Project(tmp_path, data, contract, _base_model(data))
    result.write_csv("dim_customer.csv", ["customer_key", "customer_id", "segment"],
                     [[1, "C001", "retail"], [2, "C002", "corp"], [3, "C003", "public"]])
    result.write_csv("dim_product.csv", ["product_key", "product_id", "category"],
                     [[1, "P001", "card"], [2, "P002", "ticket"], [3, "P003", "service"]])
    result.write_csv("dim_date.csv", ["date_key", "date_id", "calendar_date"],
                     [[1, "D01", "2026-01-01"], [2, "D02", "2026-01-02"],
                      [3, "D03", "2026-01-03"]])
    result.write_csv(
        "fct_sales.csv",
        ["sale_id", "customer_key", "product_key", "date_key", "amount", "quantity",
         "avg_price", "on_hand"],
        [[1001, 1, 1, 1, 10.0, 2, 5.0, 100], [1002, 2, 2, 2, 20.0, 4, 5.0, 120],
         [1003, 1, 3, 3, 7.5, 3, 2.5, 90]],
    )
    result.save()
    return result


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host not in (None, "", "localhost", "127.0.0.1"):
            raise RuntimeError(f"offline test guard blocked host: {host}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """Empty intake ledger shared by takeover tests."""
    return tmp_path / "intake.sqlite3"
