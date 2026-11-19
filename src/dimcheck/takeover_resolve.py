"""Row-level station identity and line resolution for takeover planning."""
from __future__ import annotations

from datetime import date
from typing import Any

from .intake import read_receipt, verify_receipt
from .refs import lookup_line_code, row_index, unique_row
from .sources import Table, read_table
from .takeover_events import EventDocument, instant_text, service_instant
from .util import finding, value_token


def issue(code: str, name: str, message: str, evidence: dict[str, Any],
          fatal: bool = False) -> dict:
    item = finding(code, "takeover", name, message, evidence).to_dict()
    item["fatal"] = fatal
    return item


def groups(table: Table | None, column: str) -> dict[str, list[dict[str, Any]]]:
    """Group whole rows by one non-null column via the shared refs index."""
    return row_index(table, column)


def single(grouped: dict[str, list[dict[str, Any]]], value: Any) -> dict | None:
    return unique_row(grouped, value)


def calendar_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class Inputs:
    """Verified receipt tables plus indexes; absent sources leave ``None``."""

    def __init__(self, old: Table | None, new: Table | None,
                 old_line: Table | None, new_line: Table | None,
                 dates: Table | None, fact: Table | None):
        self.old, self.new = old, new
        self.old_line, self.new_line = old_line, new_line
        self.old_key = groups(old, "station_key")
        self.new_key = groups(new, "station_key")
        self.old_code = groups(old, "station_code")
        self.new_code = groups(new, "station_code")
        self.old_line_key = groups(old_line, "line_key")
        self.new_line_key = groups(new_line, "line_key")
        self.date_key = groups(dates, "date_key")
        self.fact = fact

    def line_codes(self) -> set[str]:
        result: set[str] = set()
        for table in (self.old_line, self.new_line):
            if table is not None and "line_code" in table.columns:
                result.update(str(row["line_code"]) for row in table.rows
                              if row.get("line_code") is not None)
        return result


def load_side(contract, ledger: str, batch_id: str, source_root: str,
              nulls: set[str], blockers: list[dict]) -> tuple[dict, dict[str, Table]]:
    """Read a frozen receipt back and re-verify it against the raw CSV files."""
    name = f"receipt:{batch_id}"
    summary = {"batch_id": batch_id, "source_root": source_root, "producer": None,
               "manifest_sha": None, "files": []}
    try:
        receipt = read_receipt(ledger, batch_id)
    except ValueError:
        blockers.append(issue("DIM-TO-005", name, "收据不存在或不可读",
                              {"batch_id": batch_id}, fatal=True))
        return summary, {}
    verification = verify_receipt(receipt, source_root)
    for item in verification["issues"]:
        blockers.append(issue("DIM-TO-005", name, item["message"], item["evidence"],
                              fatal=True))
    tables: dict[str, Table] = {}
    for item in verification["files"]:
        if item["dimension"] not in {"dim_line", "dim_station"}:
            continue
        if item["dimension"] in tables:
            blockers.append(issue("DIM-TO-006", name, "同一交付重复提供维度",
                                  {"dimension": item["dimension"]}, fatal=True))
            continue
        try:
            tables[item["dimension"]] = read_table(
                item["dimension"], item["path"], item["relative_path"], nulls,
                contract.max_rows)
        except Exception as exc:  # noqa: BLE001 - map parser errors to receipt issues
            blockers.append(issue("DIM-TO-005", name, "收据来源无法解析",
                                  {"dimension": item["dimension"],
                                   "error": type(exc).__name__}, fatal=True))
    for dimension in ("dim_line", "dim_station"):
        if dimension not in tables:
            blockers.append(issue("DIM-TO-005", name, "收据缺少必要维度",
                                  {"dimension": dimension}, fatal=True))
    summary.update({
        "producer": receipt["producer"], "manifest_sha": receipt["manifest_sha"],
        "files": [{"dimension": item["dimension"],
                   "relative_path": item["relative_path"],
                   "sha256": item["sha256"], "rows": item["rows"]}
                  for item in verification["files"]],
    })
    return summary, tables


def identity_issues(inputs: Inputs, events: EventDocument) -> list[dict]:
    """Plan-wide identity findings.

    Only ``fatal`` findings block every partition. A duplicated station *code*
    is never singled out for global blocking: the row evaluator marks exactly
    the rows that cannot pick a unique new identity, while a duplicate that no
    ridership row references stays a reported (non-fatal) finding.
    """
    blockers: list[dict] = []
    for side, table in (("old", inputs.old), ("new", inputs.new)):
        for column in ("station_key", "station_code"):
            duplicate_rows = [values for values
                              in groups(table, column).values() if len(values) > 1]
            if duplicate_rows:
                # Old-side ambiguity taints every identity and stays fatal. A
                # new-side duplicate code is scoped by the row evaluator so a
                # clean partition can still be adopted under the partitioned
                # policy, matching the Jan 2 / Jan 3 counterexample.
                blockers.append(issue(
                    "DIM-TO-006", f"{side}:dim_station",
                    f"{column} 不唯一，身份不可判定",
                    {"side": side, "column": column,
                     "duplicate_groups": len(duplicate_rows),
                     "duplicate_codes": sorted(
                         str(values[0].get("station_code"))
                         for values in duplicate_rows)},
                    fatal=column == "station_key" or side == "old"))
    for token, values in inputs.old_key.items():
        reused = inputs.new_key.get(token, [])
        if len(values) == 1 and len(reused) == 1 \
                and values[0].get("station_code") != reused[0].get("station_code"):
            blockers.append(issue(
                "DIM-TO-007", f"station_key:{values[0].get('station_key')}",
                "同一代理键在新批次被另一站码复用",
                {"station_key": values[0].get("station_key"),
                 "old_station_code": values[0].get("station_code"),
                 "new_station_code": reused[0].get("station_code")},
                fatal=True))
    known_lines = inputs.line_codes()
    for event in events.events:
        missing = sorted({line for line in (event.from_line_code, event.to_line_code)
                          if line not in known_lines})
        if missing:
            blockers.append(issue(
                "DIM-TO-008", f"event:{event.ordinal}", "转线事件指向不存在的线路",
                {"station_code": event.station_code,
                 "effective_at": instant_text(event.effective_at),
                 "missing_lines": missing}, fatal=True))
    return blockers


def evaluate(row: dict, inputs: Inputs, events: EventDocument) -> dict:
    """Resolve one ridership row to a unique identity and line, or keep evidence."""
    result = {
        "event_id": row.get("event_id"),
        "service_date_key": row.get("service_date_key"),
        "calendar_date": None,
        "station_key": row.get("station_key"),
        "old_station_code": None, "old_line_code": None,
        "new_station_code": None, "new_line_code": None,
        "expected_line_code": None, "status": "pending", "reason": None,
        "affected": False,
    }
    date_row = single(inputs.date_key, row.get("service_date_key"))
    if date_row is None:
        result["reason"] = "missing_service_date"
        return result
    day = calendar_date(date_row.get("calendar_date"))
    result["calendar_date"] = day.isoformat() if day else date_row.get("calendar_date")
    if day is None:
        result["reason"] = "invalid_service_date"
        return result
    old_rows = inputs.old_key.get(value_token(row.get("station_key")), [])
    if len(old_rows) != 1:
        result["reason"] = ("ambiguous_old_station_key" if old_rows
                            else "unknown_old_station_key")
        return result
    old_row = old_rows[0]
    station_code = old_row.get("station_code")
    result["old_station_code"] = station_code
    old_line = lookup_line_code(inputs.old_line, inputs.old_line_key,
                                old_row.get("line_key"))
    result["old_line_code"] = old_line
    result["affected"] = any(event.station_code == station_code
                             for event in events.events)
    if station_code is None:
        result["reason"] = "missing_station_code"
        return result
    if result["old_line_code"] is None:
        # The old batch's line_key does not resolve to exactly one dim_line row,
        # so the station's starting ownership is unknown.
        result["reason"] = "line_unresolved"
        return result
    if result["affected"] and events.prior_line(station_code) != result["old_line_code"]:
        # The event chain claims a different prior owner than the old batch, so
        # the whole interval is unreliable for this station.
        result["reason"] = "old_line_mismatch"
        return result
    expected, reason = events.line_at(station_code, service_instant(day))
    if reason == "conflicting_transfers":
        result["reason"] = reason
        return result
    before_first_event = reason == "before_first_event"
    no_transfer = reason == "no_transfer_events"
    result["expected_line_code"] = expected if expected is not None \
        else result["old_line_code"]
    new_rows = inputs.new_code.get(value_token(station_code), [])
    if len(new_rows) != 1:
        result["reason"] = ("ambiguous_new_station_code" if new_rows
                            else "missing_new_station_code")
        return result
    new_row = new_rows[0]
    result["new_station_code"] = station_code
    reused = inputs.new_key.get(value_token(row.get("station_key")), [])
    if len(reused) == 1 and reused[0].get("station_code") != station_code:
        result["reason"] = "surrogate_key_reused"
        return result
    new_line = lookup_line_code(inputs.new_line, inputs.new_line_key,
                                new_row.get("line_key"))
    result["new_line_code"] = new_line
    if result["expected_line_code"] is None or result["new_line_code"] is None:
        result["reason"] = "line_unresolved"
        return result
    if no_transfer and result["new_line_code"] != result["old_line_code"]:
        # A station without transfer events must keep the same line in both
        # batches; otherwise the new batch moved it without an auditable event.
        result["reason"] = "new_line_mismatch"
        return result
    if not before_first_event and not no_transfer \
            and result["new_line_code"] != expected:
        # A new batch may not silently keep the old line after a transfer.
        result["reason"] = "transfer_not_applied"
        return result
    if before_first_event and result["new_line_code"] != result["old_line_code"]:
        result["reason"] = "new_line_mismatch"
        return result
    result["status"] = "verified"
    result["reason"] = "line_matches_events"
    return result
