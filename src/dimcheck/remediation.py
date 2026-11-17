"""Freeze an isolated remediation case and replay its observed defect state."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .contract import Contract, load_contract
from .fanout import check_fanout
from .keys import check_keys
from .refs import check_refs
from .sources import Dataset, _build_table, _scalar, load_dataset


@dataclass(frozen=True)
class RemediationCase:
    case_id: str
    digest: str
    contract: Contract
    observed: Dataset
    supplements: dict[str, Path]
    findings: dict[str, list[str]]


def _under(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("case paths must be nonempty and relative")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
        raise ValueError(f"case path escapes root or is unavailable: {relative}")
    return target


def _csv(path: Path, columns: tuple[str, ...], nulls: set[str]) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(columns):
            raise ValueError(f"case CSV columns differ from contract source: {path.name}")
        rows = list(reader)
    if not rows or any(None in row for row in rows):
        raise ValueError(f"case CSV has no rows or malformed rows: {path.name}")
    return [{key: _scalar(value, nulls) for key, value in row.items()}
            for row in rows]


def load_case(path: str | Path) -> RemediationCase:
    case_path = Path(path).resolve()
    root = Path(__file__).resolve().parents[2]
    if not case_path.is_relative_to(root) or not case_path.is_file():
        raise ValueError("case must be a file inside the repository")
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    if set(raw) != {"case_id", "contract", "sources", "append_facts",
                    "supplements", "sha256"}:
        raise ValueError("case manifest fields differ from expected schema")
    if not isinstance(raw["case_id"], str) or not raw["case_id"]:
        raise ValueError("case_id is required")
    paths = {name: _under(root, name) for name in raw["sha256"]}
    if set(paths) != {raw["contract"], *raw["sources"], *raw["append_facts"].values(),
                      *raw["supplements"].values()}:
        raise ValueError("case inputs and digest list differ")
    for name, target in paths.items():
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != raw["sha256"][name]:
            raise ValueError(f"case input changed: {name}")
    contract = load_contract(paths[raw["contract"]])
    declared_sources = {
        (contract.root / entity.source).resolve().relative_to(root).as_posix()
        for entity in (*contract.dimensions, *contract.facts)
    }
    if set(raw["sources"]) != declared_sources:
        raise ValueError("source list differs from contract")
    dataset = load_dataset(contract)
    tables = dict(dataset.tables)
    nulls = {token.strip().lower() for token in contract.null_tokens} | {""}
    for fact_name, relative in raw["append_facts"].items():
        if fact_name not in contract.facts_by_name():
            raise ValueError(f"unknown fact: {fact_name}")
        old = tables[fact_name]
        new_rows = _csv(paths[relative], old.columns, nulls)
        tables[fact_name] = _build_table(fact_name, old.source, list(old.columns),
                                         list(old.rows) + new_rows)
    observed = Dataset(dataset.name, tables, sum(len(t.rows) for t in tables.values()),
                       dataset.max_rows)
    supplements = {}
    for label, relative in raw["supplements"].items():
        if label not in contract.dimensions_by_name():
            raise ValueError(f"unknown dimension: {label}")
        _csv(paths[relative], tables[label].columns, nulls)
        supplements[label] = paths[relative]
    findings = {}
    for label, checker in (("keys", check_keys), ("refs", check_refs),
                           ("fanout", check_fanout)):
        found, _ = checker(contract, observed)
        findings[label] = [item.code for item in found]
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return RemediationCase(raw["case_id"], hashlib.sha256(canonical).hexdigest(),
                           contract, observed, supplements, findings)


def main() -> int:
    case = load_case(sys.argv[1] if len(sys.argv) > 1 else
                     Path(__file__).resolve().parents[2] / "fixtures" / "remediation" / "case.json")
    print(json.dumps({"case_id": case.case_id, "digest": case.digest,
                      "fact_rows": {fact.name: len(case.observed.table(fact.name).rows)
                                    for fact in case.contract.facts},
                      "findings": case.findings,
                      "supplements": sorted(case.supplements)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
