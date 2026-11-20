"""Versioned, append-only trip-fact publishing with provenance and contract gating."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

import yaml

from .tap_index import (DateIndex, FareContract, TripError, canonical_sha,
                        load_date_index, load_station_index)
from .trip_build import Leg, BuildResult, build
from .trip_journey import Journey

FACT_NAME = "fct_trip"


def journey_id_factory(contract_id: str, card: str, day: str,
                       legs: tuple[Leg, ...]) -> str:
    material = [contract_id, card, day,
                [(leg.entry.event_id, leg.exit.event_id) for leg in legs]]
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"trip-{digest[:16]}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _journey_row(journey: Journey, date_index: DateIndex) -> dict | None:
    date_key = date_index.key_for(journey.service_day)
    if date_key is None:
        return None
    first, last = journey.legs[0], journey.legs[-1]
    return {
        "trip_id": journey.journey_id,
        "service_date_key": date_key,
        "origin_station_key": first.origin_key,
        "destination_station_key": last.destination_key,
        "segment_count": len(journey.legs),
        "transfer_count": journey.transfers,
        "entry_tap_ids": ";".join(leg.entry.event_id for leg in journey.legs),
        "exit_tap_ids": ";".join(leg.exit.event_id for leg in journey.legs),
        "card_token": journey.card_token,
        "fare_contract_id": journey.fare_contract_id,
    }


def _write_fact_csv(path: Path, rows: list[dict]) -> None:
    header = ["trip_id", "service_date_key", "origin_station_key", "destination_station_key",
              "segment_count", "transfer_count", "entry_tap_ids", "exit_tap_ids",
              "card_token", "fare_contract_id"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_fact_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _contract_document() -> dict:
    dim = lambda name, key, natural, attrs, **extra: {
        "name": name, "source": f"{name}.csv", "format": "csv",
        "surrogate_key": key, "natural_keys": [natural],
        "attributes": [{"name": item, "type": kind} for item, kind in attrs], **extra}
    return {
        "schema_version": 1, "run_epoch": 1767225600,
        "dataset": {"name": "afc_trip_derivation", "root": ".", "max_rows": 500000,
                    "null_tokens": ["NULL", "null"]},
        "dimensions": [
            dim("dim_date", "date_key", "date_id",
                [("date_key", "integer"), ("date_id", "string"),
                 ("calendar_date", "date"), ("is_weekend", "boolean"),
                 ("fiscal_year", "integer")]),
            dim("dim_station", "station_key", "station_code",
                [("station_key", "integer"), ("station_code", "string"),
                 ("station_name", "string"), ("line_key", "integer")]),
        ],
        "facts": [{
            "name": FACT_NAME, "source": f"{FACT_NAME}.csv", "format": "csv",
            "grain": ["trip_id"],
            "refs": [
                {"dimension": "dim_date", "fact_key": "service_date_key",
                 "dimension_key": "date_key", "role": "service_date"},
                {"dimension": "dim_station", "fact_key": "origin_station_key",
                 "dimension_key": "station_key", "role": "origin_station"},
                {"dimension": "dim_station", "fact_key": "destination_station_key",
                 "dimension_key": "station_key", "role": "destination_station"},
            ],
            "measures": [
                {"name": "segment_count", "column": "segment_count", "agg": "sum",
                 "additivity": "additive", "unit": "segments"},
                {"name": "transfer_count", "column": "transfer_count", "agg": "sum",
                 "additivity": "additive", "unit": "transfers"},
            ],
        }],
    }


def _manifest(result: BuildResult, rows: list[dict], hashes: dict, contract: FareContract,
              unresolved: list) -> dict:
    return {
        "schema_version": 1,
        "fare_contract": {"contract_id": contract.contract_id, "kind": contract.kind,
                          "max_ride_seconds": contract.max_ride_seconds,
                          "transfer_limit_seconds": contract.transfer_limit_seconds,
                          "service_day_policy": contract.service_day_policy},
        "source_hashes": hashes,
        "counts": {"taps": len(result.taps), "conflicts": len(result.conflicts),
                   "legs": len(result.legs), "journeys_pending": len(result.pending),
                   "trip_facts": len(rows), "unmapped_service_days": len(unresolved)},
        "legs": [leg.evidence() for leg in sorted(result.legs,
                                                  key=lambda item: item.entry.event_id)],
        "trips": rows,
        "pending": sorted(result.pending, key=lambda item: (item["reason"],
                                                            item["card_token"],
                                                            item["event_ids"])),
        "conflicts": result.conflicts,
    }


def _station_manifest(stations) -> list[dict]:
    return [{"station_code": code, "station_key": value[0], "station_name": value[1]}
            for code, value in sorted(stations.by_code.items())]


def publish_version(taps_file, stations_file, links_file, date_file, contract: FareContract,
                    out_root, contract_file=None) -> dict:
    taps_file, stations_file = Path(taps_file), Path(stations_file)
    links_file, date_file = Path(links_file), Path(date_file)
    out_root = Path(out_root)
    stations = load_station_index(stations_file)
    date_index = load_date_index(date_file)
    contract_hash = (_sha(Path(contract_file)) if contract_file is not None
                     else canonical_sha(json.dumps(contract.__dict__, sort_keys=True)))
    hashes = {"taps_sha256": _sha(taps_file), "stations_sha256": _sha(stations_file),
              "links_sha256": _sha(links_file), "dates_sha256": _sha(date_file),
              "fare_contract_sha256": contract_hash}
    version = hashlib.sha256(
        json.dumps([hashes, contract.__dict__], sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    version_dir = out_root / f"v-{version}"
    index_file = out_root / "versions.json"
    if version_dir.exists():
        try:
            stored = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
            stored_hashes = stored["source_hashes"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise TripError("DIM-TAP-002",
                            f"version manifest unreadable or altered {version}") from exc
        if stored_hashes != hashes:
            raise TripError("DIM-TAP-002", f"version directory conflict {version}")
    else:
        version_dir.mkdir(parents=True)
        for source in (taps_file, stations_file, links_file, date_file):
            shutil.copyfile(source, version_dir / source.name)
        if contract_file is not None:
            shutil.copyfile(Path(contract_file),
                            version_dir / Path(contract_file).name)
    result = build(version_dir / taps_file.name, stations, version_dir / links_file.name,
                   contract, journey_id_factory)
    rows, unresolved = [], []
    for journey in result.journeys:
        row = _journey_row(journey, date_index)
        (rows if row is not None else unresolved).append(row or journey)
    if unresolved:
        result.add_pending("service_date_unmapped", "",
                           [tap for journey in unresolved
                            for leg in journey.legs for tap in (leg.entry.event_id,
                                                                leg.exit.event_id)],
                           {"service_days": sorted({journey.service_day
                                                    for journey in unresolved})})
    rows.sort(key=lambda row: row["trip_id"])
    _write_fact_csv(version_dir / f"{FACT_NAME}.csv", rows)
    _write_fact_jsonl(version_dir / f"{FACT_NAME}.jsonl", rows)
    (version_dir / "trip-contract.yaml").write_text(
        yaml.safe_dump(_contract_document(), sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    (version_dir / "pending.json").write_text(
        json.dumps(sorted(result.pending, key=lambda item: item["reason"]),
                   ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (version_dir / "station-manifest.json").write_text(
        json.dumps(_station_manifest(stations), ensure_ascii=False,
                   sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest = _manifest(result, rows, hashes, contract, unresolved)
    (version_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    _append_index(index_file, version, hashes, contract, len(rows), len(result.pending))
    verdict = verify_version(version_dir)
    return {"version": version, "dir": version_dir, "manifest": manifest,
            "fact_rows": len(rows), "verification": verdict}


def _append_index(index_file: Path, version: str, hashes: dict, contract: FareContract,
                  rows: int, pending: int) -> None:
    record = {"version": version, "fare_contract_id": contract.contract_id,
              "fare_contract_kind": contract.kind, "source_hashes": hashes,
              "trip_fact_rows": rows, "pending_count": pending}
    history = []
    if index_file.exists():
        history = json.loads(index_file.read_text(encoding="utf-8"))
        if any(item["version"] == version for item in history):
            return
    history.append(record)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_file.write_text(json.dumps(history, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")


def verify_version(version_dir) -> dict:
    """Run the repository's existing keys/refs/fanout/measures checks on the new fact."""
    from .cli import run_once
    version_dir = Path(version_dir)
    check_dir = version_dir / "check"
    check_dir.mkdir(exist_ok=True)
    report, _ = run_once(version_dir / "trip-contract.yaml", check_dir, quiet=True)
    errors = [item for item in report["findings"] if item["severity"] == "error"]
    warnings = [item for item in report["findings"] if item["severity"] == "warning"]
    verdict = {"version": version_dir.name, "verdict": report["verdict"],
               "exit_code": report["exit_code"], "publishable": not errors,
               "error_codes": sorted({item["code"] for item in errors}),
               "warning_codes": sorted({item["code"] for item in warnings}),
               "fact_rows": report["dataset"]["tables"][FACT_NAME]["rows"],
               "findings": report["findings"],
               "report": "check/report.json"}
    (version_dir / "verification.json").write_text(
        json.dumps(verdict, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    return verdict
