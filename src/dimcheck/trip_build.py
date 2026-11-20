"""Proof-based entry/exit pairing from gate events.

No greedy nearest-gate matching: a ride segment is published only when the
entry-before-exit order, the maximum ride duration and the station mapping are
all provable inside the clock-error true-time windows. Ambiguity is enumerated
over the full feasible bipartite graph instead of resolved heuristically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .tap_index import StationIndex, Tap, TripError, instant


@dataclass(frozen=True)
class Leg:
    entry: Tap
    exit: Tap
    origin_key: int
    destination_key: int

    @property
    def duration_window(self) -> tuple[int, int]:
        return (int((self.exit.true_lo - self.entry.true_hi).total_seconds()),
                int((self.exit.true_hi - self.entry.true_lo).total_seconds()))

    def crosses_midnight(self) -> bool:
        return self.entry.at.date() != self.exit.at.date()

    def evidence(self) -> dict:
        lo, hi = self.duration_window
        return {
            "entry_tap_id": self.entry.event_id, "exit_tap_id": self.exit.event_id,
            "origin_station_code": self.entry.station_code,
            "destination_station_code": self.exit.station_code,
            "origin_station_key": self.origin_key,
            "destination_station_key": self.destination_key,
            "ride_duration_seconds_window": [lo, hi],
        }


@dataclass
class BuildResult:
    taps: list[Tap] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    journeys: list = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    dropped_event_ids: list[str] = field(default_factory=list)

    def add_pending(self, reason: str, card_token: str, event_ids: list[str],
                    detail: dict) -> None:
        self.pending.append({"reason": reason, "card_token": card_token,
                             "event_ids": sorted(event_ids), "evidence": detail})


def load_taps(path: str | Path, stations: StationIndex) -> tuple[list[Tap], list[dict]]:
    """Read JSONL taps; exact duplicates collapse, same-id divergent payloads conflict."""
    taps: list[Tap] = []
    conflicts: list[dict] = []
    known: dict[str, dict] = {}
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        event_id = row.get("event_id")
        if not event_id or not row.get("card_token") or row.get("kind") not in ("entry", "exit"):
            raise TripError("DIM-TAP-001", "bad_tap_observation")
        error = row.get("clock_error_seconds")
        if not isinstance(error, int) or isinstance(error, bool) or error < 0:
            raise TripError("DIM-TAP-001", "bad_tap_observation")
        moment = instant(row["at"])
        if row["station_code"] not in stations.by_code:
            raise TripError("DIM-TAP-001", "tap_station_unknown")
        payload = {key: row[key] for key in sorted(row)}
        if event_id in known:
            if known[event_id] == payload:
                continue
            conflicts.append({"reason": "same_identity_divergent_payload",
                              "event_id": event_id,
                              "evidence": [known[event_id], payload]})
            continue
        known[event_id] = payload
        taps.append(Tap(event_id, row["card_token"], row["kind"], row["station_code"],
                        moment, error, index))
    return taps, conflicts


def gap_window(exit_tap: Tap, entry_tap: Tap) -> tuple[int, int]:
    return (int((entry_tap.true_lo - exit_tap.true_hi).total_seconds()),
            int((entry_tap.true_hi - exit_tap.true_lo).total_seconds()))


def _edge_status(entry: Tap, exit_tap: Tap, max_ride_seconds: int) -> str | None:
    """Provable / possible / None(impossible) for one entry-exit pair."""
    if exit_tap.true_hi < entry.true_lo:
        return None
    order_provable = exit_tap.true_lo > entry.true_hi
    longest = int((exit_tap.true_hi - entry.true_lo).total_seconds())
    if order_provable and 0 <= longest <= max_ride_seconds:
        return "provable"
    return "possible"


def _maximum_matchings(entries: list[Tap], exits: list[Tap],
                       edges: dict[tuple[str, str], str]) -> list[list[tuple[str, str]]]:
    adjacency: dict[str, list[str]] = {e.event_id: [] for e in entries}
    for (eid, xid) in edges:
        adjacency[eid].append(xid)
    for eid in adjacency:
        adjacency[eid].sort()
    matchings: list[list[tuple[str, str]]] = []
    occupied: set[str] = set()

    def walk(position: int, chosen: list[tuple[str, str]]) -> None:
        if position == len(entries):
            if not matchings or len(chosen) == len(matchings[0]):
                matchings.append(list(chosen))
            elif len(chosen) > len(matchings[0]):
                matchings[:] = [list(chosen)]
            return
        entry_id = entries[position].event_id
        for exit_id in adjacency[entry_id]:
            if exit_id in occupied:
                continue
            occupied.add(exit_id)
            chosen.append((entry_id, exit_id))
            walk(position + 1, chosen)
            chosen.pop()
            occupied.remove(exit_id)
        walk(position + 1, chosen)  # leave this entry unmatched

    walk(0, [])
    best = max((len(item) for item in matchings), default=0)
    return [item for item in matchings if len(item) == best]


def pair_legs(taps: list[Tap], stations: StationIndex, max_ride_seconds: int,
              result: BuildResult) -> None:
    used: set[str] = set()
    for card in sorted({tap.card_token for tap in taps}):
        card_taps = [tap for tap in taps if tap.card_token == card]
        entries = sorted((tap for tap in card_taps if tap.kind == "entry"),
                         key=lambda item: item.event_id)
        exits = sorted((tap for tap in card_taps if tap.kind == "exit"),
                       key=lambda item: item.event_id)
        edges: dict[tuple[str, str], str] = {}
        for entry in entries:
            for exit_tap in exits:
                status = _edge_status(entry, exit_tap, max_ride_seconds)
                if status is not None:
                    edges[(entry.event_id, exit_tap.event_id)] = status
        matchings = _maximum_matchings(entries, exits, edges)
        unique = len(matchings) == 1
        chosen = matchings[0] if matchings else []
        all_provable = unique and all(edges[pair] == "provable" for pair in chosen)
        if unique and all_provable and chosen:
            for entry_id, exit_id in chosen:
                entry = next(tap for tap in entries if tap.event_id == entry_id)
                exit_tap = next(tap for tap in exits if tap.event_id == exit_id)
                origin = stations.resolve(entry.station_code)
                destination = stations.resolve(exit_tap.station_code)
                if origin is None or destination is None:
                    result.add_pending("station_code_unmapped", card, [entry_id, exit_id],
                                       {"taps": [entry.evidence(), exit_tap.evidence()]})
                    continue
                result.legs.append(Leg(entry, exit_tap, origin[0], destination[0]))
                used.update((entry_id, exit_id))
        elif edges:
            involved = sorted({event for pair in edges for event in pair})
            reason = _block_reason(entries, exits, edges, matchings, max_ride_seconds)
            result.add_pending(reason, card, involved,
                               {"candidate_pairs": _candidate_evidence(entries, exits, edges,
                                                                       max_ride_seconds),
                                "feasible_maximum_matchings": len(matchings)})
            used.update(involved)
        for tap in card_taps:
            if tap.event_id in used:
                continue
            result.add_pending(*_unpaired(tap, entries, exits, max_ride_seconds, card))
            used.add(tap.event_id)


def _block_reason(entries: list[Tap], exits: list[Tap],
                  edges: dict[tuple[str, str], str], matchings: list,
                  max_ride_seconds: int) -> str:
    """Classify why no leg can be published, without greedy resolution."""
    if len(matchings) > 1:
        return "ambiguous_pairing"
    by_id = {tap.event_id: tap for tap in entries + exits}
    if any(status != "provable" for status in edges.values()):
        candidate = next(pair for pair, status in edges.items() if status != "provable")
        entry, exit_tap = by_id[candidate[0]], by_id[candidate[1]]
        if not exit_tap.true_lo > entry.true_hi:
            return "order_unprovable"
        longest = int((exit_tap.true_hi - entry.true_lo).total_seconds())
        if longest > max_ride_seconds:
            return "ride_threshold_crossed"
    return "ambiguous_pairing"


def _unpaired(tap: Tap, entries: list[Tap], exits: list[Tap], max_ride_seconds: int,
              card: str) -> tuple[str, str, list[str], dict]:
    others = exits if tap.kind == "entry" else entries
    if not others:
        return "missing_partner", card, [tap.event_id], {"tap": tap.evidence()}
    nearest = min(others, key=lambda item: abs((item.at - tap.at).total_seconds()))
    window = (gap_window(tap, nearest) if tap.kind == "entry"
              else gap_window(nearest, tap))
    return "ride_threshold_crossed_or_uncertain_order", card, [tap.event_id], {
        "tap": tap.evidence(), "nearest_tap": nearest.evidence(),
        "true_gap_seconds_window": list(window), "max_ride_seconds": max_ride_seconds}


def _candidate_evidence(entries: list[Tap], exits: list[Tap],
                        edges: dict[tuple[str, str], str], max_ride_seconds: int) -> list[dict]:
    by_id = {tap.event_id: tap for tap in entries + exits}
    evidence = []
    for (entry_id, exit_id), status in sorted(edges.items()):
        entry, exit_tap = by_id[entry_id], by_id[exit_id]
        evidence.append({
            "entry_tap_id": entry_id, "exit_tap_id": exit_id, "status": status,
            "order_provable": exit_tap.true_lo > entry.true_hi,
            "ride_duration_seconds_window": [
                int((exit_tap.true_lo - entry.true_hi).total_seconds()),
                int((exit_tap.true_hi - entry.true_lo).total_seconds())],
            "max_ride_seconds": max_ride_seconds})
    return evidence



def build(taps_file, stations: StationIndex, links_file, contract, id_factory):
    """Pair legs, then assemble journeys under the frozen fare contract."""
    from .tap_index import load_links
    from .trip_journey import build_journeys
    links = load_links(links_file, stations)
    result = BuildResult()
    taps, conflicts = load_taps(taps_file, stations)
    result.taps = taps
    result.conflicts = conflicts
    result.dropped_event_ids = sorted(item["event_id"] for item in conflicts)
    dropped = set(result.dropped_event_ids)
    active = [tap for tap in taps if tap.event_id not in dropped]
    pair_legs(active, stations, contract.max_ride_seconds, result)
    result.journeys = build_journeys(result, contract, links, id_factory)
    return result
