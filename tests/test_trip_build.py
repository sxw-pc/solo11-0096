"""Unit tests for proof-based pairing, clock-error windows and journey assembly."""
from __future__ import annotations

import json

import pytest

from dimcheck.tap_index import FareContract, StationIndex, Tap, TripError, instant
from dimcheck.trip_build import BuildResult, gap_window, load_taps, pair_legs
from dimcheck.trip_journey import build_journeys

STATIONS = StationIndex({
    "S-A01": (1, "甲一"), "S-A02": (2, "甲二"),
    "S-B01": (3, "乙一"), "S-B02": (4, "乙二"),
})
LINKS = {("S-A02", "S-B01"): 600}


def tap(event_id, kind, code, at, error=0, card="H", index=0):
    return Tap(event_id, card, kind, code, instant(at), error, index)


def write(path, rows):
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
                    encoding="utf-8")


BASE_ROWS = [
    {"event_id": "tap-1", "card_token": "H", "kind": "entry", "station_code": "S-A01",
     "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 0},
    {"event_id": "tap-2", "card_token": "H", "kind": "exit", "station_code": "S-A02",
     "at": "2026-01-01T08:20:00Z", "clock_error_seconds": 0},
    {"event_id": "tap-3", "card_token": "H", "kind": "entry", "station_code": "S-B01",
     "at": "2026-01-01T08:25:00Z", "clock_error_seconds": 0},
    {"event_id": "tap-4", "card_token": "H", "kind": "exit", "station_code": "S-B02",
     "at": "2026-01-01T08:40:00Z", "clock_error_seconds": 0},
]


def pair(rows, max_ride=1800):
    result = BuildResult()
    taps, conflicts = _load(rows)
    result.taps, result.conflicts = taps, conflicts
    pair_legs(taps, STATIONS, max_ride, result)
    return result


def _load(rows, tmp_path=None):
    if tmp_path is None:
        taps = [Tap(r["event_id"], r["card_token"], r["kind"], r["station_code"],
                    instant(r["at"]), r["clock_error_seconds"], i)
                for i, r in enumerate(rows)]
        return taps, []
    path = tmp_path / "taps.jsonl"
    write(path, rows)
    return load_taps(path, STATIONS)


def test_provable_pairing_publishes_two_legs():
    result = pair(BASE_ROWS)
    assert len(result.legs) == 2 and not result.pending
    assert [(leg.entry.event_id, leg.exit.event_id) for leg in result.legs] == [
        ("tap-1", "tap-2"), ("tap-3", "tap-4")]
    assert result.legs[0].origin_key == 1 and result.legs[0].destination_key == 2


def test_arrival_order_ignored_business_time_decides(tmp_path):
    shuffled = [BASE_ROWS[3], BASE_ROWS[1], BASE_ROWS[2], BASE_ROWS[0]]
    taps, _ = _load(shuffled, tmp_path)
    result = BuildResult(taps=taps)
    pair_legs(taps, STATIONS, 1800, result)
    assert sorted((leg.entry.event_id, leg.exit.event_id) for leg in result.legs) == [
        ("tap-1", "tap-2"), ("tap-3", "tap-4")]
    assert {tap.arrival_index for tap in taps} == {0, 1, 2, 3}


def test_exact_replay_is_idempotent(tmp_path):
    rows = [BASE_ROWS[0], BASE_ROWS[0], BASE_ROWS[1], BASE_ROWS[2],
            BASE_ROWS[2], BASE_ROWS[3]]
    taps, conflicts = _load(rows, tmp_path)
    assert len(taps) == 4 and conflicts == []


def test_same_identity_divergent_payload_is_quarantined(tmp_path):
    divergent = dict(BASE_ROWS[0])
    divergent["at"] = "2026-01-01T08:01:00Z"
    taps, conflicts = _load([BASE_ROWS[0], divergent] + BASE_ROWS[1:], tmp_path)
    assert len(conflicts) == 1 and conflicts[0]["event_id"] == "tap-1"
    dropped = {item["event_id"] for item in conflicts}
    result = BuildResult(taps=taps)
    pair_legs([t for t in taps if t.event_id not in dropped], STATIONS, 1800, result)
    assert [(leg.entry.event_id, leg.exit.event_id) for leg in result.legs] == [
        ("tap-3", "tap-4")]


def test_clock_error_gap_window_spans_three_to_seven_minutes():
    exit_tap = tap("tap-2", "exit", "S-A02", "2026-01-01T08:20:00Z", 60)
    entry_tap = tap("tap-3", "entry", "S-B01", "2026-01-01T08:25:00Z", 60)
    assert gap_window(exit_tap, entry_tap) == (180, 420)


def test_transfer_uncertain_at_five_minute_threshold(tmp_path):
    rows = [BASE_ROWS[0], {**BASE_ROWS[1], "clock_error_seconds": 60},
            {**BASE_ROWS[2], "clock_error_seconds": 60}, BASE_ROWS[3]]
    taps, _ = _load(rows, tmp_path)
    result = BuildResult(taps=taps)
    pair_legs(taps, STATIONS, 1800, result)
    assert len(result.legs) == 2
    contract = FareContract("fc", "paid_area_continuity", 1800, 300, "entry_business_date")
    journeys = build_journeys(result, contract, LINKS, lambda *a: "trip-x")
    assert journeys == []
    assert [item["reason"] for item in result.pending] == ["transfer_uncertain"]
    assert result.pending[0]["evidence"]["transfer_gap_seconds_window"] == [180, 420]


def test_paid_area_continuity_groups_one_journey_one_transfer():
    result = pair(BASE_ROWS)
    contract = FareContract("fc", "paid_area_continuity", 1800, 600, "entry_business_date")
    journeys = build_journeys(result, contract, LINKS, lambda *a: "trip-1")
    assert len(journeys) == 1
    assert journeys[0].transfers == 1 and len(journeys[0].legs) == 2


def test_gate_exit_breaks_groups_two_journeys_zero_transfers():
    result = pair(BASE_ROWS)
    contract = FareContract("fc", "gate_exit_breaks", 1800, 600, "entry_business_date")
    journeys = build_journeys(result, contract, LINKS, lambda *a: "trip-x")
    assert len(journeys) == 2
    assert all(journey.transfers == 0 and len(journey.legs) == 1 for journey in journeys)


def test_missing_end_is_pending_not_invented():
    result = pair([BASE_ROWS[0]])
    assert result.legs == []
    assert result.pending[0]["reason"] == "missing_partner"
    assert result.pending[0]["event_ids"] == ["tap-1"]


def test_ambiguous_two_entries_one_exit_is_not_greedy():
    rows = [
        {"event_id": "e1", "card_token": "K", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 0},
        {"event_id": "e2", "card_token": "K", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:10:00Z", "clock_error_seconds": 0},
        {"event_id": "x1", "card_token": "K", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-01T08:20:00Z", "clock_error_seconds": 0},
    ]
    result = pair(rows)
    assert result.legs == []
    pending = result.pending[0]
    assert pending["reason"] == "ambiguous_pairing"
    assert pending["evidence"]["feasible_maximum_matchings"] == 2


def test_unprovable_order_and_crossed_threshold_are_distinct_reasons():
    order_rows = [
        {"event_id": "o1", "card_token": "O", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 300},
        {"event_id": "o2", "card_token": "O", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 300},
    ]
    assert pair(order_rows).pending[0]["reason"] == "order_unprovable"
    long_rows = [
        {"event_id": "l1", "card_token": "L", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 0},
        {"event_id": "l2", "card_token": "L", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-01T09:00:00Z", "clock_error_seconds": 0},
    ]
    assert pair(long_rows).pending[0]["reason"] == "ride_threshold_crossed"


def test_midnight_without_service_day_rule_is_pending():
    rows = [
        {"event_id": "n1", "card_token": "N", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T23:55:00Z", "clock_error_seconds": 0},
        {"event_id": "n2", "card_token": "N", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-02T00:10:00Z", "clock_error_seconds": 0},
    ]
    result = pair(rows)
    contract = FareContract("fc", "gate_exit_breaks", 1800, 600, "unspecified")
    journeys = build_journeys(result, contract, LINKS, lambda *a: "trip-n")
    assert journeys == []
    assert result.pending[0]["reason"] == "service_day_unresolved"


def test_midnight_policies_anchor_different_service_days(tmp_path):
    rows = [
        {"event_id": "n1", "card_token": "N", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T23:55:00Z", "clock_error_seconds": 0},
        {"event_id": "n2", "card_token": "N", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-02T00:10:00Z", "clock_error_seconds": 0},
    ]
    result = pair(rows)
    by_entry = FareContract("a", "gate_exit_breaks", 1800, 600, "entry_business_date")
    by_exit = FareContract("b", "gate_exit_breaks", 1800, 600, "exit_business_date")
    j_entry = build_journeys(BuildResult(legs=list(result.legs)), by_entry, LINKS,
                             lambda *a: "trip-e")
    j_exit = build_journeys(BuildResult(legs=list(result.legs)), by_exit, LINKS,
                            lambda *a: "trip-x")
    assert j_entry[0].service_day == "2026-01-01"
    assert j_exit[0].service_day == "2026-01-02"
    unspecified = FareContract("c", "gate_exit_breaks", 1800, 600, "unspecified")
    blocked = BuildResult(legs=list(result.legs))
    assert build_journeys(blocked, unspecified, LINKS, lambda *a: "trip-u") == []
    assert blocked.pending[0]["reason"] == "service_day_unresolved"


def test_cards_are_pairing_islands(tmp_path):
    rows = [
        {"event_id": "a1", "card_token": "A", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 0},
        {"event_id": "b1", "card_token": "B", "kind": "entry", "station_code": "S-B01",
         "at": "2026-01-01T08:01:00Z", "clock_error_seconds": 0},
        {"event_id": "a2", "card_token": "A", "kind": "exit", "station_code": "S-A02",
         "at": "2026-01-01T08:10:00Z", "clock_error_seconds": 0},
        {"event_id": "b2", "card_token": "B", "kind": "exit", "station_code": "S-B02",
         "at": "2026-01-01T08:11:00Z", "clock_error_seconds": 0},
    ]
    result = pair(rows)
    assert len(result.legs) == 2
    assert {(leg.entry.card_token, leg.entry.event_id, leg.exit.event_id)
            for leg in result.legs} == {("A", "a1", "a2"), ("B", "b1", "b2")}


def test_unknown_station_and_bad_observations_raise(tmp_path):
    bad = dict(BASE_ROWS[0], station_code="S-Z99")
    path = tmp_path / "bad.jsonl"
    write(path, [bad])
    with pytest.raises(TripError, match="DIM-TAP-001"):
        load_taps(path, STATIONS)
    with pytest.raises(TripError):
        FareContract("x", "unknown_kind", 1, 1, "entry_business_date").validate()


def test_same_name_distinct_codes_keep_distinct_surrogate_keys(tmp_path):
    stations = StationIndex({"S-A01": (1, "同名"), "S-B01": (3, "同名")})
    rows = [
        {"event_id": "a", "card_token": "C", "kind": "entry", "station_code": "S-A01",
         "at": "2026-01-01T08:00:00Z", "clock_error_seconds": 0},
        {"event_id": "b", "card_token": "C", "kind": "exit", "station_code": "S-B01",
         "at": "2026-01-01T08:10:00Z", "clock_error_seconds": 0},
    ]
    taps, _ = _load(rows, tmp_path)
    result = BuildResult(taps=taps)
    pair_legs(taps, stations, 1800, result)
    leg = result.legs[0]
    assert (leg.origin_key, leg.destination_key) == (1, 3)
