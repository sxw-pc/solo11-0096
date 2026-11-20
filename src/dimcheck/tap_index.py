"""Frozen gate-event, station, fare-contract and service-day models for trip building."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLICIES = ("entry_business_date", "exit_business_date", "unspecified")
CONTRACT_KINDS = ("paid_area_continuity", "gate_exit_breaks")


class TripError(ValueError):
    """Validation failure carrying a stable dimcheck error code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def instant(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise TripError("DIM-TAP-001", "tap_timezone_required")
    return moment


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Tap:
    event_id: str
    card_token: str
    kind: str
    station_code: str
    at: datetime
    clock_error_seconds: int
    arrival_index: int

    @property
    def true_lo(self) -> datetime:
        return self.at - timedelta(seconds=self.clock_error_seconds)

    @property
    def true_hi(self) -> datetime:
        return self.at + timedelta(seconds=self.clock_error_seconds)

    def evidence(self) -> dict:
        return {
            "event_id": self.event_id, "card_token": self.card_token, "kind": self.kind,
            "station_code": self.station_code, "at": iso(self.at),
            "clock_error_seconds": self.clock_error_seconds,
            "arrival_index": self.arrival_index,
            "true_window": [iso(self.true_lo), iso(self.true_hi)],
        }


@dataclass(frozen=True)
class StationIndex:
    by_code: dict[str, tuple[int, str]]

    def resolve(self, code: str) -> tuple[int, str] | None:
        return self.by_code.get(code)


@dataclass(frozen=True)
class DateIndex:
    by_date: dict[str, int]

    def key_for(self, day: str) -> int | None:
        return self.by_date.get(day)


@dataclass(frozen=True)
class TransferLink:
    from_station: str
    to_station: str
    max_walk_seconds: int

    def key(self) -> tuple[str, str]:
        return self.from_station, self.to_station


@dataclass(frozen=True)
class FareContract:
    contract_id: str
    kind: str
    max_ride_seconds: int
    transfer_limit_seconds: int
    service_day_policy: str

    @classmethod
    def load(cls, path: str | Path) -> "FareContract":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        contract = cls(raw["contract_id"], raw["kind"], int(raw["max_ride_seconds"]),
                       int(raw["transfer_limit_seconds"]), raw["service_day_policy"])
        contract.validate()
        return contract

    def validate(self) -> None:
        if not self.contract_id or self.kind not in CONTRACT_KINDS:
            raise TripError("DIM-TAP-001", "bad_fare_contract")
        if (not isinstance(self.max_ride_seconds, int) or self.max_ride_seconds <= 0
                or not isinstance(self.transfer_limit_seconds, int)
                or self.transfer_limit_seconds < 0
                or self.service_day_policy not in POLICIES):
            raise TripError("DIM-TAP-001", "bad_fare_contract")


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_station_index(path: str | Path) -> StationIndex:
    rows = _csv_rows(Path(path))
    by_code: dict[str, tuple[int, str]] = {}
    for row in rows:
        code, raw_key = row.get("station_code"), row.get("station_key")
        if not code or raw_key in (None, ""):
            raise TripError("DIM-TAP-001", "bad_station_dimension")
        key = int(raw_key)
        if code in by_code and by_code[code][0] != key:
            raise TripError("DIM-TAP-001", "station_code_maps_to_many_keys")
        by_code.setdefault(code, (key, row.get("station_name", "")))
    return StationIndex(by_code)


def load_date_index(path: str | Path) -> DateIndex:
    by_date = {row["calendar_date"]: int(row["date_key"]) for row in _csv_rows(Path(path))}
    return DateIndex(by_date)


def load_links(path: str | Path, stations: StationIndex) -> dict[tuple[str, str], int]:
    links: dict[tuple[str, str], int] = {}
    for raw in json.loads(Path(path).read_text(encoding="utf-8")):
        link = TransferLink(raw["from_station"], raw["to_station"], int(raw["max_walk_seconds"]))
        if (link.from_station not in stations.by_code or link.to_station not in stations.by_code
                or link.max_walk_seconds < 0):
            raise TripError("DIM-TAP-001", "bad_transfer_link")
        links[link.key()] = link.max_walk_seconds
    return links
