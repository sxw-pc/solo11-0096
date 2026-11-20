#!/usr/bin/env python3
"""Generate deterministic, entirely fictional AFC fixture data."""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date
from pathlib import Path


def _csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def generate(root: str | Path, quiet: bool = False) -> dict[str, int]:
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20260101)
    days = [date(2026, 1, number) for number in range(1, 8)]
    lines = [
        [number, f"L-{number:02d}", f"示例{index}号线"]
        for index, number in enumerate(range(1, 5), start=1)
    ]
    stations = [
        [1, "S-A01", "示例甲一", 1], [2, "S-A02", "示例甲二", 1],
        [3, "S-B01", "示例乙一", 2], [4, "S-B02", "示例乙二", 2],
        [5, "S-C01", "示例丙一", 3], [6, "S-D01", "示例丁一", 4],
    ]
    calendar = [
        [index, f"D{day:%Y%m%d}", f"{day:%Y-%m-%d}", day.weekday() >= 5, day.year]
        for index, day in enumerate(days, start=1)
    ]
    fares = [
        [1, "B1", 0.0, 2.0], [2, "B2", 2.0, 4.0],
        [3, "B3", 4.0, 7.0], [4, "B4", 7.0, 12.0],
    ]
    payments = [
        {"payment_key": 1, "payment_code": "P01", "payment_type": "example_pass"},
        {"payment_key": 2, "payment_code": "P02", "payment_type": "example_qr"},
        {"payment_key": 3, "payment_code": "P03", "payment_type": "example_cash"},
        {"payment_key": 4, "payment_code": "P04", "payment_type": "example_coupon"},
    ]
    ridership = []
    sequence = 1
    for day_index in range(1, 7):
        for station_key in range(1, 7):
            rides = rng.randint(5, 40) + station_key
            ridership.append([
                f"R{sequence:05d}", day_index, station_key, rides,
                round(rides * 2.5 + rng.randint(0, 9) * 0.5, 2),
                round(20 + rng.randint(0, 40) + day_index * 0.5, 1),
                1000 + day_index * 10 + station_key * 3,
            ])
            sequence += 1
    revenue = []
    sequence = 1
    for day_index in range(1, 5):
        for station_key in range(1, 7):
            amount = round((day_index * 7 + station_key * 3) * (1.25 + rng.randint(0, 3) * 0.05), 2)
            revenue.append([
                f"V{sequence:05d}", day_index, station_key,
                (day_index + station_key) % 4 + 1, (day_index * station_key) % 4 + 1,
                amount, day_index + station_key + 2, round(amount * 0.05, 2),
            ])
            sequence += 1
    _csv(target / "dim_line.csv", ["line_key", "line_code", "line_name"], lines)
    _csv(target / "dim_station.csv",
         ["station_key", "station_code", "station_name", "line_key"], stations)
    header = ["date_key", "date_id", "calendar_date", "is_weekend", "fiscal_year"]
    _csv(target / "dim_date.csv", header, calendar)
    _csv(target / "dim_accounting_date.csv", header, calendar)
    _csv(target / "dim_fare_band.csv",
         ["fare_band_key", "band_code", "min_fare", "max_fare"], fares)
    _jsonl(target / "dim_payment.jsonl", payments)
    _csv(target / "fct_ridership.csv",
         ["event_id", "service_date_key", "station_key", "ride_count", "fare_amount",
          "avg_wait_seconds", "card_balance"], ridership)
    _csv(target / "fct_fare_revenue.csv",
         ["revenue_id", "accounting_date_key", "station_key", "fare_band_key",
          "payment_key", "revenue_amount", "transaction_count", "refund_amount"], revenue)
    counts = _write_trip_fixtures(target)
    counts.update({
        "dim_line": len(lines), "dim_station": len(stations), "dim_date": len(calendar),
        "dim_accounting_date": len(calendar), "dim_fare_band": len(fares),
        "dim_payment": len(payments), "fct_ridership": len(ridership),
        "fct_fare_revenue": len(revenue),
    })
    if not quiet:
        print(f"fixtures: {sum(counts.values())} rows in {len(counts)} files -> {target}")
    return counts


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def _jsonl_compact(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")


def _tap(event_id, card, kind, code, at, error=0, day="2026-01-01") -> dict:
    return {"event_id": event_id, "card_token": card, "kind": kind,
            "station_code": code, "at": f"{day}T{at}Z",
            "clock_error_seconds": error}


def _write_trip_fixtures(target: Path) -> dict:
    """Frozen synthetic gate-event scenarios and fare contracts for trip building."""
    trip_dir = target / "trips"
    trip_dir.mkdir(parents=True, exist_ok=True)
    card = "synthetic-H"
    base = [
        _tap("tap-1", card, "entry", "S-A01", "08:00:00"),
        _tap("tap-2", card, "exit", "S-A02", "08:20:00"),
        _tap("tap-3", card, "entry", "S-B01", "08:25:00"),
        _tap("tap-4", card, "exit", "S-B02", "08:40:00"),
    ]
    scenarios = {
        "uncertain-transfer.jsonl": [
            base[0], _tap("tap-2", card, "exit", "S-A02", "08:20:00", 60),
            _tap("tap-3", card, "entry", "S-B01", "08:25:00", 60), base[3]],
        "shuffled.jsonl": [base[3], base[1], base[2], base[0]],
        "replayed.jsonl": [base[0], base[0], base[1], base[2], base[2], base[3]],
        "divergent-identity.jsonl": [
            base[0], _tap("tap-1", card, "entry", "S-A01", "08:01:00"),
            base[1], base[2], base[3]],
        "ambiguous-pairing.jsonl": [
            _tap("e1", "synthetic-K", "entry", "S-A01", "08:00:00"),
            _tap("e2", "synthetic-K", "entry", "S-A01", "08:10:00"),
            _tap("x1", "synthetic-K", "exit", "S-A02", "08:20:00")],
        "midnight.jsonl": [
            _tap("n1", "synthetic-N", "entry", "S-A01", "23:55:00"),
            _tap("n2", "synthetic-N", "exit", "S-A02", "00:10:00", day="2026-01-02")],
        "missing-end.jsonl": [_tap("m1", "synthetic-M", "entry", "S-A01", "08:00:00")],
    }
    for name, rows in scenarios.items():
        _jsonl_compact(trip_dir / name, rows)
    contracts = {
        "fare-paid-area-continuity.json": {
            "contract_id": "fc-paid-area-continuity-v1",
            "kind": "paid_area_continuity", "max_ride_seconds": 1800,
            "transfer_limit_seconds": 600,
            "service_day_policy": "entry_business_date"},
        "fare-gate-exit-breaks.json": {
            "contract_id": "fc-gate-exit-breaks-v1",
            "kind": "gate_exit_breaks", "max_ride_seconds": 1800,
            "transfer_limit_seconds": 600,
            "service_day_policy": "entry_business_date"},
        "fare-transfer-300.json": {
            "contract_id": "fc-transfer-threshold-five-min-v1",
            "kind": "paid_area_continuity", "max_ride_seconds": 1800,
            "transfer_limit_seconds": 300,
            "service_day_policy": "entry_business_date"},
        "fare-service-day-unspecified.json": {
            "contract_id": "fc-service-day-unspecified-v1",
            "kind": "gate_exit_breaks", "max_ride_seconds": 1800,
            "transfer_limit_seconds": 600, "service_day_policy": "unspecified"},
    }
    for name, value in contracts.items():
        _json(trip_dir / name, value)
    return {f"trips/{name}": len(rows) for name, rows in scenarios.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 dimcheck 示例 fixture")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1] / "fixtures"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    generate(args.root, args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
