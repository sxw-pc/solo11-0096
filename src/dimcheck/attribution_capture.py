"""Freeze aggregate revenue slices and dimension references, not attribution."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .contract import load_contract
from .sources import load_dataset


@dataclass(frozen=True)
class AttributionCase:
    case_id: str
    digest: str
    policy: dict
    rows: tuple[dict, ...]
    sparse_rows: tuple[dict, ...]


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("bridge case sources must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError(f"bridge source unavailable: {relative}")
    return path


def _read_rows(path: Path, policy: dict, allowed: dict) -> tuple[dict, ...]:
    columns = ["revenue_id", "accounting_date_key", "station_key", "fare_band_key",
               "payment_key", "revenue_amount", "transaction_count", "refund_amount"]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise ValueError("bridge row schema differs")
        rows = []
        for item in reader:
            row = {key: int(item[key]) for key in columns[1:5]}
            row.update({key: Decimal(item[key]) for key in columns[5:6]})
            row["transaction_count"] = int(item["transaction_count"])
            row["refund_amount"] = Decimal(item["refund_amount"])
            row["revenue_id"] = item["revenue_id"]
            unit = row["revenue_amount"] / row["transaction_count"] if row["transaction_count"] else None
            band = allowed["bands"].get(row["fare_band_key"])
            if not row["revenue_id"] or row["accounting_date_key"] not in allowed["dates"] or \
               row["accounting_date_key"] not in {policy["base_accounting_date_key"],
                                                    policy["current_accounting_date_key"]} or \
               row["station_key"] != policy["station_key"] or \
               row["station_key"] not in allowed["stations"] or \
               row["fare_band_key"] != policy["fare_band_key"] or band is None or \
               row["payment_key"] not in allowed["payments"] or \
               row["transaction_count"] <= 0 or row["revenue_amount"] < 0 or \
               row["refund_amount"] < 0 or row["refund_amount"] > row["revenue_amount"] or \
               unit is None or not (Decimal(str(band["min_fare"])) <= unit <=
                                        Decimal(str(band["max_fare"]))):
                raise ValueError("bridge row violates period, reference, or fare range")
            rows.append(row)
    if len({row["revenue_id"] for row in rows}) != len(rows):
        raise ValueError("bridge row identity repeats")
    return tuple(rows)


def load_case(manifest: str | Path) -> AttributionCase:
    root = Path(__file__).resolve().parents[2]
    path = Path(manifest).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("bridge case manifest must be inside repository")
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {"model", "dates", "station", "fare_band", "payment", "periods", "sparse", "policy"}
    if set(raw) != fields | {"sha256"}:
        raise ValueError("bridge manifest fields differ")
    names = {raw[key] for key in fields}
    if len(names) != len(fields) or set(raw["sha256"]) != names:
        raise ValueError("bridge digest coverage differs")
    paths = {name: _inside(root, name) for name in names}
    for name, source in paths.items():
        if hashlib.sha256(source.read_bytes()).hexdigest() != raw["sha256"][name]:
            raise ValueError(f"bridge source changed: {name}")
    contract = load_contract(paths[raw["model"]])
    dataset = load_dataset(contract)
    fact = next((f for f in contract.facts if f.name == "fct_fare_revenue"), None)
    if fact is None or not {"revenue_amount", "transaction_count", "refund_amount"}.issubset(
            {m.name for m in fact.measures}):
        raise ValueError("revenue fact contract unavailable")
    policy = json.loads(paths[raw["policy"]].read_text(encoding="utf-8"))
    expected = {"case_id", "base_accounting_date_key", "current_accounting_date_key",
                "station_key", "fare_band_key", "cell_key", "contracts",
                "currency_unit", "sparse_cell_handling"}
    if set(policy) != expected or policy["base_accounting_date_key"] == \
            policy["current_accounting_date_key"] or policy["cell_key"] != "payment_key":
        raise ValueError("bridge policy fields differ")
    allowed = {
        "dates": {r["date_key"] for r in dataset.table("dim_accounting_date").rows},
        "stations": {r["station_key"] for r in dataset.table("dim_station").rows},
        "bands": {r["fare_band_key"]: r for r in dataset.table("dim_fare_band").rows},
        "payments": {r["payment_key"] for r in dataset.table("dim_payment").rows},
    }
    rows = _read_rows(paths[raw["periods"]], policy, allowed)
    sparse = _read_rows(paths[raw["sparse"]], policy, allowed)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return AttributionCase(policy["case_id"], digest, policy, rows, sparse)


def main() -> int:
    path = Path(__file__).resolve().parents[2] / "fixtures" / "revenue_bridge" / "case.json"
    case = load_case(sys.argv[1] if len(sys.argv) > 1 else path)
    totals = {}
    for date in (case.policy["base_accounting_date_key"],
                 case.policy["current_accounting_date_key"]):
        rows = [r for r in case.rows if r["accounting_date_key"] == date]
        totals[str(date)] = {"transactions": sum(r["transaction_count"] for r in rows),
                             "gross": str(sum((r["revenue_amount"] for r in rows), Decimal(0))),
                             "refunds": str(sum((r["refund_amount"] for r in rows), Decimal(0)))}
    print(json.dumps({"case_id": case.case_id, "digest": case.digest,
                      "period_totals": totals, "sparse_rows": len(case.sparse_rows)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
