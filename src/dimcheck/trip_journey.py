"""Fare-contract journey assembly over proven ride segments."""
from __future__ import annotations

from dataclasses import dataclass

from .tap_index import FareContract
from .trip_build import BuildResult, Leg, gap_window


@dataclass(frozen=True)
class Journey:
    journey_id: str
    card_token: str
    service_day: str | None
    legs: tuple[Leg, ...]
    transfers: int
    fare_contract_id: str


def transfer_decision(previous: Leg, leg: Leg, contract: FareContract,
                      links: dict[tuple[str, str], int]) -> tuple[str, dict]:
    walk = links.get((previous.exit.station_code, leg.entry.station_code))
    gap_lo, gap_hi = gap_window(previous.exit, leg.entry)
    detail = {"transfer_gap_seconds_window": [gap_lo, gap_hi],
              "transfer_limit_seconds": contract.transfer_limit_seconds,
              "walk_link_seconds": walk,
              "taps": [previous.exit.evidence(), leg.entry.evidence()]}
    if walk is None or gap_hi < 0:
        return "no", detail
    bound = min(contract.transfer_limit_seconds, walk)
    if gap_hi <= bound and previous.exit.true_hi < leg.entry.true_lo:
        return "yes", detail
    if gap_lo > bound or previous.exit.true_lo >= leg.entry.true_hi:
        return "no", detail
    return "uncertain", detail


def _service_day(legs: tuple[Leg, ...], policy: str) -> str | None:
    if any(leg.crosses_midnight() for leg in legs) and policy == "unspecified":
        return None
    anchor = legs[0].entry if policy == "entry_business_date" else legs[0].exit
    return anchor.at.date().isoformat()


def build_journeys(result: BuildResult, contract: FareContract,
                   links: dict[tuple[str, str], int], id_factory) -> list[Journey]:
    contract.validate()
    by_card: dict[str, list[Leg]] = {}
    for leg in sorted(result.legs, key=lambda item: (item.entry.true_lo,
                                                     item.entry.event_id)):
        by_card.setdefault(leg.entry.card_token, []).append(leg)
    journeys: list[Journey] = []
    for card in sorted(by_card):
        groups = _group(by_card[card], contract, links, result, card)
        for group in groups:
            bundle = tuple(group)
            day = _service_day(bundle, contract.service_day_policy)
            if day is None:
                result.add_pending(
                    "service_day_unresolved", card,
                    [tap.event_id for leg in bundle for tap in (leg.entry, leg.exit)],
                    {"legs": [leg.evidence() for leg in bundle]})
                continue
            journeys.append(Journey(id_factory(contract.contract_id, card, day, bundle),
                                    card, day, bundle, len(bundle) - 1,
                                    contract.contract_id))
    return journeys


def _group(legs: list[Leg], contract: FareContract, links, result: BuildResult,
           card: str) -> list[list[Leg]]:
    if contract.kind == "gate_exit_breaks":
        return [[leg] for leg in legs]
    groups: list[list[Leg]] = []
    current: list[Leg] = []
    for leg in legs:
        if not current:
            current = [leg]
            continue
        decision, detail = transfer_decision(current[-1], leg, contract, links)
        if decision == "yes":
            current.append(leg)
        elif decision == "no":
            groups.append(current)
            current = [leg]
        else:
            result.add_pending("transfer_uncertain", card,
                               [current[-1].exit.event_id, leg.entry.event_id], detail)
            return groups
    if current:
        groups.append(current)
    return groups
