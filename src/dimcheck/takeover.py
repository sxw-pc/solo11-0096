"""Station-line ownership takeover planning for dimension deliveries.

Two verified ``dim_line``/``dim_station`` deliveries are compared against an
immutable transfer-event list. Each ``fct_ridership`` row is attributed to a
service date and a stable station identity, the transfer intervals determine the
line for that date, and the new batch must agree on both identity and line. A
row that cannot be uniquely resolved is kept as per-row pending evidence; it is
never attributed to the first matching interval or the first matching line.

Two policies are supported:

``atomic_cutover``
    Switch the whole dimension only if every affected ridership row can be
    uniquely attributed in the new batch; otherwise every partition keeps the
    old starting point. A plan-level defect (bad receipt, changed bytes,
    surrogate-key reuse, unknown line) also blocks the whole plan.
``partitioned_cutover``
    Adopt the new dimension per service-date partition where every row is
    verified; conflicting partitions keep the old starting point and are
    reported side by side without pretending the new batch was verified.
"""
from __future__ import annotations

from .contract import Contract
from .sources import Dataset, load_dataset
from .takeover_events import EventDocument, load_events
from .takeover_resolve import (Inputs, evaluate, identity_issues, issue,
                               load_side)
from .util import canonical_json, fingerprint

POLICIES = ("atomic_cutover", "partitioned_cutover")


def require_columns(contract: Contract, dataset: Dataset) -> list[dict]:
    blockers: list[dict] = []
    needed = {"dim_station": ("station_key", "station_code", "line_key"),
              "dim_line": ("line_key", "line_code"),
              "dim_date": ("date_key", "calendar_date"),
              "fct_ridership": ("event_id", "service_date_key", "station_key")}
    dimension_map = contract.dimensions_by_name()
    fact_map = contract.facts_by_name()
    for name, columns in needed.items():
        entity = dimension_map.get(name) or fact_map.get(name)
        if entity is None or name not in dataset.tables:
            blockers.append(issue("DIM-TO-009", name, "契约缺少接管所需对象",
                                  {"object": name}, fatal=True))
            continue
        missing = [column for column in columns
                   if column not in dataset.table(name).columns]
        if missing:
            blockers.append(issue("DIM-TO-009", name, "契约数据缺少接管所需列",
                                  {"object": name, "missing": missing}, fatal=True))
    return blockers


def _partition_key(row: dict) -> str:
    return canonical_json(row.get("service_date_key"))


def _station_view(batch: list[dict]) -> list[dict]:
    stations: dict[str, dict] = {}
    for row in batch:
        code = row["old_station_code"]
        if code is None:
            continue
        item = stations.setdefault(str(code), {
            "old_station_code": code, "old_line_code": row["old_line_code"],
            "new_station_code": row["new_station_code"],
            "new_line_code": row["new_line_code"],
            "expected_line_code": row["expected_line_code"],
            "rows": 0, "verified_rows": 0})
        item["rows"] += 1
        if row["status"] == "verified":
            item["verified_rows"] += 1
    return [stations[key] for key in sorted(stations)]


def _transfer_view(batch: list[dict], events: EventDocument) -> list[dict]:
    """Transfer events that touched stations appearing in this partition."""
    codes = {row["old_station_code"] for row in batch
             if row["old_station_code"] is not None}
    ordered = [event for event in events.events if event.station_code in codes]
    ordered.sort(key=lambda event: (event.station_code, event.effective_at,
                                    event.ordinal))
    return [event.to_dict() for event in ordered]


def build_partitions(rows: list[dict], events: EventDocument, policy: str,
                     plan_pending: bool) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(_partition_key(row), []).append(row)
    all_verified = all(row["status"] == "verified" for row in rows)
    partitions = []
    for key in sorted(grouped):
        batch = grouped[key]
        pending = [row for row in batch if row["status"] != "verified"]
        if plan_pending:
            adopted, reason = False, "plan_pending"
        elif policy == "atomic_cutover":
            adopted = all_verified
            reason = "all_rows_verified" if adopted else "atomic_blocked"
        elif pending:
            adopted, reason = False, "row_conflicts"
        else:
            adopted, reason = True, "all_rows_verified"
        verified_rows = sum(1 for row in batch if row["status"] == "verified")
        evidence = sorted(
            ({"event_id": row["event_id"], "reason": row["reason"],
              "station_key": row["station_key"],
              "service_date_key": row["service_date_key"],
              "calendar_date": row["calendar_date"],
              "old_station_code": row["old_station_code"],
              "new_station_code": row["new_station_code"],
              "expected_line_code": row["expected_line_code"],
              "new_line_code": row["new_line_code"]} for row in pending),
            key=lambda item: canonical_json(item))
        partitions.append({
            "service_date_key": batch[0].get("service_date_key"),
            "calendar_date": batch[0].get("calendar_date"),
            "conclusion": "adopted_new" if adopted else "retained_old",
            "reason": reason,
            "rows": len(batch),
            "adopted_new_rows": len(batch) if adopted else 0,
            "retained_old_rows": len(batch) if not adopted else 0,
            "affected_rows": sum(1 for row in batch if row["affected"]),
            "verified_rows": verified_rows,
            "pending_rows": len(pending),
            "stations": _station_view(batch),
            "transfers": _transfer_view(batch, events),
            "pending_evidence": evidence[:50],
        })
    return partitions


def _receipt_digest(summary: dict) -> dict:
    return {"batch_id": summary.get("batch_id"),
            "manifest_sha": summary.get("manifest_sha"),
            "files": sorted(({"dimension": item["dimension"],
                              "sha256": item["sha256"], "rows": item["rows"]}
                             for item in summary.get("files", [])),
                            key=lambda item: item["dimension"])}


_EVENT_REASONS = ("conflicting_transfers", "new_line_mismatch",
                  "old_line_mismatch", "transfer_not_applied")


def _with_event_conflicts(blockers: list[dict], rows: list[dict]) -> list[dict]:
    """Surface per-row transfer problems as findings without freezing the plan.

    A duplicate station code that no ridership row references stays a reported
    finding. A row-level event conflict (ambiguous identity or a new line that
    contradicts the interval) is evidence inside its partition; only a
    plan-level defect such as a changed receipt marks the whole plan pending.
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row["reason"] in _EVENT_REASONS:
            grouped.setdefault(row["reason"], []).append(row)
    result = list(blockers)
    for reason in sorted(grouped):
        batch = grouped[reason]
        result.append(issue(
            "DIM-TO-003", f"rows:{reason}",
            "客流行无法唯一归属" if reason == "conflicting_transfers"
            else "新维表线路与转线事件不一致",
            {"rows": len(batch),
             "event_ids": sorted(str(row["event_id"]) for row in batch)[:20]}))
    return result


def build_plan(contract: Contract, ledger: str, receipts: list[dict],
               events_path: str, policy: str, epoch: int) -> dict:
    """Compute one deterministic takeover plan; never writes source files."""
    if policy not in POLICIES:
        raise ValueError("takeover_policy_invalid")
    if len(receipts) != 2:
        raise ValueError("takeover_requires_two_receipts")
    blockers = require_columns(contract, load_dataset(contract))
    nulls = {token.strip().lower() for token in contract.null_tokens}
    nulls.add("")
    summaries: dict[str, dict] = {}
    sides: dict[str, dict] = {}
    for side, item in zip(("old", "new"), receipts):
        summaries[side], sides[side] = load_side(
            contract, ledger, item["batch_id"], item["source_root"], nulls, blockers)
    events = load_events(events_path)
    for item in events.issues:
        blockers.append(issue(item["code"], "events", item["message"],
                              item["evidence"], fatal=True))
    dataset = load_dataset(contract)
    inputs = Inputs(sides["old"].get("dim_station"), sides["new"].get("dim_station"),
                    sides["old"].get("dim_line"), sides["new"].get("dim_line"),
                    dataset.tables.get("dim_date"),
                    dataset.tables.get("fct_ridership"))
    for item in identity_issues(inputs, events):
        blockers.append(item)
    rows = [evaluate(row, inputs, events) for row in (inputs.fact.rows
                                                      if inputs.fact else [])]
    blockers = _with_event_conflicts(blockers, rows)
    blocked = any(item.get("fatal") for item in blockers)
    partitions = build_partitions(rows, events, policy, blocked)
    plan_pending = blocked or any(row["status"] != "verified" for row in rows)
    adopted_rows = sum(item["adopted_new_rows"] for item in partitions)
    total_rows = sum(item["rows"] for item in partitions)
    summary = {
        "partitions": len(partitions),
        "adopted_new_partitions": sum(1 for item in partitions
                                      if item["conclusion"] == "adopted_new"),
        "retained_old_partitions": sum(1 for item in partitions
                                       if item["conclusion"] == "retained_old"),
        "rows": total_rows,
        "affected_rows": sum(item["affected_rows"] for item in partitions),
        "adopted_new_rows": adopted_rows,
        "retained_old_rows": total_rows - adopted_rows,
        "verified_rows": sum(item["verified_rows"] for item in partitions),
        "pending_rows": sum(item["pending_rows"] for item in partitions),
        "affected_rows_by_receipt": {
            "old": sum(item["retained_old_rows"] for item in partitions),
            "new": adopted_rows,
        },
    }
    identity = {
        "contract": contract.fingerprint, "policy": policy,
        "receipts": {side: _receipt_digest(summaries[side]) for side in ("old", "new")},
        "events": events.sha256,
        "blockers": sorted(canonical_json(item) for item in blockers),
        "partitions": [{"service_date_key": item["service_date_key"],
                        "conclusion": item["conclusion"], "reason": item["reason"]}
                       for item in partitions],
    }
    confirmed = not plan_pending
    return {
        "plan_id": "to-" + fingerprint(identity)[:16],
        "fingerprint": fingerprint(identity),
        "policy": policy,
        "state": "confirmed" if confirmed else "pending",
        "blocked": blocked,
        "contract": {"name": contract.name, "fingerprint": contract.fingerprint},
        "receipts": summaries,
        "events": {"sha256": events.sha256, "count": len(events.events),
                   "conflicted_stations": sorted(events.conflicted_stations)},
        "events_path": str(events_path),
        "summary": summary,
        "partitions": partitions,
        "findings": sorted(blockers, key=lambda item: canonical_json(item)),
        "generated_epoch": epoch,
    }
