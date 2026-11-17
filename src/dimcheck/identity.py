"""Freeze candidate natural-key correspondence evidence for conformed dimensions."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .conform import check_conform
from .contract import load_contract
from .sources import load_dataset


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def register_case(design_path: str | Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    path = Path(design_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("identity design must be inside repository")
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8-sig"))
    if set(doc) != {"case_id", "contract_path", "left_dimension", "right_dimension",
                    "alias_path", "file_sha256"}:
        raise ValueError("identity design fields differ")
    if not doc["case_id"] or not doc["left_dimension"] or not doc["right_dimension"]:
        raise ValueError("identity case fields missing")
    for relative, expected in doc["file_sha256"].items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
            raise ValueError("identity source unavailable")
        if _sha(source.read_bytes()) != expected:
            raise ValueError("identity source bytes changed")
    if doc["contract_path"] not in doc["file_sha256"] or doc["alias_path"] not in doc["file_sha256"]:
        raise ValueError("identity source digest missing")
    contract = load_contract(root / doc["contract_path"])
    dataset = load_dataset(contract)
    dimensions = contract.dimensions_by_name()
    left = dimensions[doc["left_dimension"]]
    right = dimensions[doc["right_dimension"]]
    if left.conformed_group is None or left.conformed_group != right.conformed_group:
        raise ValueError("identity dimensions must share conformed group")
    if len(left.natural_keys) != 1 or len(right.natural_keys) != 1:
        raise ValueError("fixture needs single natural keys")
    left_rows = dataset.table(left.name).rows
    right_rows = dataset.table(right.name).rows
    left_keys = {str(row[left.natural_keys[0]]) for row in left_rows}
    right_keys = {str(row[right.natural_keys[0]]) for row in right_rows}
    with (root / doc["alias_path"]).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["left_code", "right_code"]:
            raise ValueError("identity alias columns differ")
        aliases = [(row["left_code"], row["right_code"]) for row in reader]
    if not aliases or len(set(aliases)) != len(aliases) or any(
            a not in left_keys or b not in right_keys for a, b in aliases):
        raise ValueError("identity aliases duplicate or unknown")
    existing, _ = check_conform(contract, dataset)
    return {"case_id": doc["case_id"], "design_hash": _sha(raw),
            "contract": contract, "dataset": dataset, "left": left.name, "right": right.name,
            "aliases": aliases, "existing_findings": [item.code for item in existing]}


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    case = register_case(root / "fixtures" / "identity" / "design.json")
    print(json.dumps({key: value for key, value in case.items()
                      if key not in {"contract", "dataset"}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
