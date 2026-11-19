"""Station transfer events: UTC instants, half-open intervals, and conflicts.

The event file is a local, immutable JSON document. Each event moves one stable
station identity (``station_code``) from one line to another starting at a UTC
instant. The line of a station is a step function over service days:

    (-inf, events[0].effective)                  -> events[0].from_line_code
    [events[i].effective, events[i+1].effective) -> events[i].to_line_code

A service date is a business day at 00:00 UTC, so a record whose date equals an
effective instant belongs to the new interval (start included, end excluded).
Intervals must be half-open and conflict-free; a station whose events cannot be
ordered into such a chain is marked unresolved instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

EVENT_FIELDS = ("station_code", "from_line_code", "to_line_code", "effective_at")


@dataclass(frozen=True)
class TransferEvent:
    station_code: str
    from_line_code: str
    to_line_code: str
    effective_at: datetime
    ordinal: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_code": self.station_code,
            "from_line_code": self.from_line_code,
            "to_line_code": self.to_line_code,
            "effective_at": instant_text(self.effective_at),
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class EventDocument:
    events: tuple[TransferEvent, ...]
    sha256: str
    issues: tuple[dict[str, Any], ...] = ()
    conflicted_stations: frozenset[str] = field(default_factory=frozenset)

    @property
    def valid(self) -> bool:
        return not self.issues

    def line_at(self, station_code: str,
                moment: datetime) -> tuple[str | None, str | None]:
        """Return ``(line_code, unresolved_reason)`` for one station at an instant.

        ``None`` plus a reason means the identity cannot be uniquely assigned.
        The upper bound of each interval is exclusive, so an event effective at
        the queried instant already selects the new line. Before the first
        event the answer is the event's ``from_line_code``; the caller cross
        checks it against the old batch's ownership.
        """
        if station_code in self.conflicted_stations:
            return None, "conflicting_transfers"
        relevant = [event for event in self.events
                    if event.station_code == station_code]
        if not relevant:
            return None, "no_transfer_events"
        relevant.sort(key=lambda event: (event.effective_at, event.ordinal))
        starts = [event for event in relevant if event.effective_at <= moment]
        if not starts:
            return relevant[0].from_line_code, "before_first_event"
        return starts[-1].to_line_code, None

    def prior_line(self, station_code: str) -> str | None:
        """The line the event chain claims the station had before its first move."""
        relevant = [event for event in self.events
                    if event.station_code == station_code]
        if not relevant:
            return None
        return min(relevant, key=lambda event: (event.effective_at,
                                                event.ordinal)).from_line_code


def instant_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def service_instant(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _parse_instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    if moment.utcoffset() != timezone.utc.utcoffset(moment):
        return None
    return moment.astimezone(timezone.utc)


def _chain_issues(events: list[TransferEvent]) -> tuple[list[dict[str, Any]], set[str]]:
    """Verify each station's events form ordered, conflict-free half-open intervals."""
    issues: list[dict[str, Any]] = []
    conflicted: set[str] = set()
    by_station: dict[str, list[TransferEvent]] = {}
    for event in events:
        by_station.setdefault(event.station_code, []).append(event)
    for code in sorted(by_station):
        ordered = sorted(by_station[code],
                         key=lambda event: (event.effective_at, event.ordinal))
        previous: TransferEvent | None = None
        for event in ordered:
            if previous is not None:
                if event.effective_at == previous.effective_at:
                    conflicted.add(code)
                    issues.append({
                        "code": "DIM-TO-003",
                        "message": "同一车站存在相同生效时点的转线事件",
                        "evidence": {"station_code": code,
                                     "effective_at": instant_text(event.effective_at)}})
                elif event.from_line_code != previous.to_line_code:
                    conflicted.add(code)
                    issues.append({
                        "code": "DIM-TO-003",
                        "message": "相邻转线事件的线路链断裂",
                        "evidence": {"station_code": code,
                                     "prior_to": previous.to_line_code,
                                     "next_from": event.from_line_code,
                                     "effective_at": instant_text(event.effective_at)}})
            previous = event
    return issues, conflicted


def load_events(path: str | Path) -> EventDocument:
    """Parse a transfer event file without touching any other input."""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return EventDocument((), digest, ({"code": "DIM-TO-001",
            "message": "转线事件文件不是合法 UTF-8 JSON",
            "evidence": {"error": type(exc).__name__}},))
    items = document.get("events") if isinstance(document, dict) else document
    if not isinstance(items, list):
        return EventDocument((), digest, ({"code": "DIM-TO-001",
            "message": "转线事件清单缺少 events 列表",
            "evidence": {"type": type(items).__name__}},))
    issues: list[dict[str, Any]] = []
    events: list[TransferEvent] = []
    for ordinal, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append({"code": "DIM-TO-001", "message": "转线事件必须是对象",
                           "evidence": {"ordinal": ordinal}})
            continue
        missing = [name for name in EVENT_FIELDS
                   if not isinstance(item.get(name), str) or not item[name].strip()]
        if missing:
            issues.append({"code": "DIM-TO-001", "message": "转线事件字段缺失",
                           "evidence": {"ordinal": ordinal, "missing": sorted(missing)}})
            continue
        from_line = item["from_line_code"].strip()
        to_line = item["to_line_code"].strip()
        effective = _parse_instant(item["effective_at"])
        if effective is None:
            issues.append({"code": "DIM-TO-001", "message": "生效时点不是 UTC 时间戳",
                           "evidence": {"ordinal": ordinal,
                                        "effective_at": item["effective_at"]}})
            continue
        if from_line == to_line:
            issues.append({"code": "DIM-TO-002", "message": "转线事件原线路与新线路相同",
                           "evidence": {"ordinal": ordinal, "line": from_line}})
            continue
        events.append(TransferEvent(item["station_code"].strip(), from_line,
                                    to_line, effective, ordinal))
    chain_issues, conflicted = _chain_issues(events)
    issues.extend(chain_issues)
    ordered = tuple(sorted(events, key=lambda event: (event.station_code,
                                                      event.effective_at,
                                                      event.ordinal)))
    return EventDocument(ordered, digest, tuple(issues), frozenset(conflicted))
