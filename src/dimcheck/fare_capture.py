"""Freeze a synthetic AFC journey and fare-band case without computing caps."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .contract import load_contract
from .sources import load_dataset


@dataclass(frozen=True)
class FareCase:
    case_id: str
    digest: str
    policy: dict
    journeys: tuple[dict, ...]


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("fare case paths must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError(f"fare case source unavailable: {relative}")
    return path


def _instant(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("journey time requires timezone")
    return value


def load_case(manifest: str | Path) -> FareCase:
    root = Path(__file__).resolve().parents[2]
    path = Path(manifest).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("fare case manifest must be inside repository")
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {"model", "station", "line", "bands", "date", "journeys", "policy"}
    if set(raw) != fields | {"sha256"}:
        raise ValueError("fare case manifest fields differ")
    names = {raw[key] for key in fields}
    if len(names) != len(fields) or set(raw["sha256"]) != names:
        raise ValueError("fare case digest coverage differs")
    paths = {name: _inside(root, name) for name in names}
    for name, item in paths.items():
        if hashlib.sha256(item.read_bytes()).hexdigest() != raw["sha256"][name]:
            raise ValueError(f"fare case source changed: {name}")
    model = load_contract(paths[raw["model"]])
    dataset = load_dataset(model)
    policy = json.loads(paths[raw["policy"]].read_text(encoding="utf-8"))
    if set(policy) != {"case_id", "service_date_key", "line_cap_minor",
                       "network_cap_minor", "line_cap_sponsor", "network_cap_sponsor",
                       "first_receipt_cutoff_utc",
                       "second_receipt_cutoff_utc", "eligible_orders"}:
        raise ValueError("fare policy fields differ")
    station = {row["station_key"]: row["line_key"]
               for row in dataset.table("dim_station").rows}
    lines = {row["line_key"] for row in dataset.table("dim_line").rows}
    bands = {row["fare_band_key"]: row
             for row in dataset.table("dim_fare_band").rows}
    dates = {row["date_key"] for row in dataset.table("dim_date").rows}
    with paths[raw["journeys"]].open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = ["journey_id", "card_token", "station_key", "service_date_key",
                    "occurred_at_utc", "received_at_utc", "fare_band_key", "base_fare_minor"]
        if reader.fieldnames != expected:
            raise ValueError("journey schema differs")
        journeys = []
        for row in reader:
            key, band, date = (int(row[name]) for name in
                               ("station_key", "fare_band_key", "service_date_key"))
            fare = int(row["base_fare_minor"])
            if not row["journey_id"] or not row["card_token"] or key not in station or \
               station[key] not in lines or band not in bands or date not in dates or fare < 0:
                raise ValueError("journey identity or dimension reference differs")
            lo = Decimal(str(bands[band]["min_fare"])) * 100
            hi = Decimal(str(bands[band]["max_fare"])) * 100
            if not lo <= fare <= hi:
                raise ValueError("base fare lies outside declared band")
            _instant(row["occurred_at_utc"])
            _instant(row["received_at_utc"])
            journeys.append({**row, "line_key": station[key], "base_fare_minor": fare})
    if not journeys or len({row["journey_id"] for row in journeys}) != len(journeys):
        raise ValueError("journey identities are empty or repeated")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return FareCase(policy["case_id"], digest, policy, tuple(journeys))


def main() -> int:
    default = Path(__file__).resolve().parents[2] / "fixtures" / "fare_cap" / "case.json"
    case = load_case(sys.argv[1] if len(sys.argv) > 1 else default)
    first = _instant(case.policy["first_receipt_cutoff_utc"])
    initial = [row for row in case.journeys if _instant(row["received_at_utc"]) <= first]
    print(json.dumps({"case_id": case.case_id, "digest": case.digest,
                      "initial_journeys": [row["journey_id"] for row in initial],
                      "initial_gross_minor": sum(row["base_fare_minor"] for row in initial),
                      "all_gross_minor": sum(row["base_fare_minor"] for row in case.journeys),
                      "lines": {row["journey_id"]: row["line_key"] for row in case.journeys}},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
