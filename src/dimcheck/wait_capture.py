"""Freeze a complete synthetic arrival roster; no tail certificate yet."""
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
class WaitCase:
    case_id: str
    digest: str
    policy: dict
    fact_row: dict
    observations: tuple[dict, ...]


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("wait case sources must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError(f"wait case source unavailable: {relative}")
    return path


def load_case(manifest: str | Path) -> WaitCase:
    root = Path(__file__).resolve().parents[2]
    path = Path(manifest).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("wait case manifest must be inside repository")
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {"model", "ridership", "station", "date", "observations", "policy"}
    if set(raw) != fields | {"sha256"}:
        raise ValueError("wait case manifest fields differ")
    names = {raw[key] for key in fields}
    if len(names) != len(fields) or set(raw["sha256"]) != names:
        raise ValueError("wait case digest coverage differs")
    paths = {name: _inside(root, name) for name in names}
    for name, source in paths.items():
        if hashlib.sha256(source.read_bytes()).hexdigest() != raw["sha256"][name]:
            raise ValueError(f"wait case source changed: {name}")
    contract = load_contract(paths[raw["model"]])
    dataset = load_dataset(contract)
    policy = json.loads(paths[raw["policy"]].read_text(encoding="utf-8"))
    expected_policy = {"case_id", "covered_fact_event_id", "arrival_roster_complete",
                       "p95_order_rule", "sla_threshold_seconds",
                       "observation_horizon_seconds", "eligible_contracts"}
    if set(policy) != expected_policy or policy["arrival_roster_complete"] is not True:
        raise ValueError("wait policy fields differ")
    fact_rows = [row for row in dataset.table("fct_ridership").rows
                 if row["event_id"] == policy["covered_fact_event_id"]]
    if len(fact_rows) != 1:
        raise ValueError("covered fact row is missing or ambiguous")
    fact = fact_rows[0]
    stations = {row["station_key"] for row in dataset.table("dim_station").rows}
    dates = {row["date_key"] for row in dataset.table("dim_date").rows}
    with paths[raw["observations"]].open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_columns = ["person_id", "event_id", "station_key", "service_date_key",
                            "observation", "wait_lower_seconds", "wait_upper_seconds"]
        if reader.fieldnames != expected_columns:
            raise ValueError("observation schema differs")
        observations = []
        for row in reader:
            lower, upper = int(row["wait_lower_seconds"]), int(row["wait_upper_seconds"])
            station, date = int(row["station_key"]), int(row["service_date_key"])
            if not row["person_id"] or row["event_id"] != fact["event_id"] or \
               station != fact["station_key"] or date != fact["service_date_key"] or \
               station not in stations or date not in dates or \
               row["observation"] not in {"boarded", "right_censored"} or \
               lower < 0 or lower > upper or upper > policy["observation_horizon_seconds"]:
                raise ValueError("observation does not fit covered station-day")
            observations.append({**row, "wait_lower_seconds": lower,
                                 "wait_upper_seconds": upper})
    if len({row["person_id"] for row in observations}) != len(observations):
        raise ValueError("arrival roster repeats person identity")
    completed = [row for row in observations
                 if row["observation"] == "boarded"]
    reported_total = Decimal(str(fact["avg_wait_seconds"])) * fact["ride_count"]
    if not completed or len(completed) != fact["ride_count"] or \
       not (sum(row["wait_lower_seconds"] for row in completed) <= reported_total <=
            sum(row["wait_upper_seconds"] for row in completed)):
        raise ValueError("completed wait observations disagree with original fact")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return WaitCase(policy["case_id"], digest, policy, fact, tuple(observations))


def main() -> int:
    default = Path(__file__).resolve().parents[2] / "fixtures" / "wait_tail" / "case.json"
    case = load_case(sys.argv[1] if len(sys.argv) > 1 else default)
    boarded = [r for r in case.observations if r["observation"] == "boarded"]
    censored = [r for r in case.observations if r["observation"] == "right_censored"]
    print(json.dumps({"case_id": case.case_id, "digest": case.digest,
                      "covered_event": case.fact_row["event_id"],
                      "ride_count": case.fact_row["ride_count"],
                      "reported_avg_wait": case.fact_row["avg_wait_seconds"],
                      "reported_completed_wait_total": str(Decimal(str(case.fact_row["avg_wait_seconds"])) *
                                                           case.fact_row["ride_count"]),
                      "boarded_count": len(boarded), "censored_lower_seconds":
                      [r["wait_lower_seconds"] for r in censored],
                      "arrival_count": len(case.observations)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
