"""Append-only SQLite persistence for takeover plans.

A plan is written exactly once per ``plan_id``. The ``plan_id`` is derived from
the contract fingerprint, policy, receipt digests, event digest, blocking
findings, and partition conclusions, so any byte change in a source or event
file produces a different plan instead of mutating a confirmed one. There is no
UPDATE or DELETE on conclusions. Independent re-verification lives in
``takeover_verify`` so the reader and the checker are not the same code path.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import canonical_json

DB_NAME = "takeover.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS takeover_plan(
  plan_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  policy TEXT NOT NULL,
  state TEXT NOT NULL,
  contract_fingerprint TEXT NOT NULL,
  events_sha256 TEXT NOT NULL,
  events_path TEXT NOT NULL,
  generated_epoch INTEGER NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS takeover_receipt(
  plan_id TEXT NOT NULL REFERENCES takeover_plan(plan_id),
  side TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  producer TEXT,
  manifest_sha TEXT,
  source_root TEXT NOT NULL,
  PRIMARY KEY(plan_id, side)
);
CREATE TABLE IF NOT EXISTS takeover_receipt_file(
  plan_id TEXT NOT NULL REFERENCES takeover_plan(plan_id),
  side TEXT NOT NULL,
  dimension TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  rows INTEGER NOT NULL,
  PRIMARY KEY(plan_id, side, dimension)
);
CREATE TABLE IF NOT EXISTS takeover_partition(
  plan_id TEXT NOT NULL REFERENCES takeover_plan(plan_id),
  ordinal INTEGER NOT NULL,
  service_date_key TEXT NOT NULL,
  calendar_date TEXT,
  conclusion TEXT NOT NULL,
  reason TEXT NOT NULL,
  rows INTEGER NOT NULL,
  adopted_new_rows INTEGER NOT NULL,
  retained_old_rows INTEGER NOT NULL,
  affected_rows INTEGER NOT NULL,
  verified_rows INTEGER NOT NULL,
  pending_rows INTEGER NOT NULL,
  stations_json TEXT NOT NULL,
  transfers_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY(plan_id, ordinal)
);
CREATE TABLE IF NOT EXISTS takeover_event(
  plan_id TEXT NOT NULL REFERENCES takeover_plan(plan_id),
  ordinal INTEGER NOT NULL,
  station_code TEXT NOT NULL,
  from_line_code TEXT NOT NULL,
  to_line_code TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  PRIMARY KEY(plan_id, ordinal)
);
"""


def save_plan(database: str | Path, plan: dict[str, Any]) -> bool:
    """Persist a plan. Returns ``True`` for a new plan, ``False`` for a replay.

    A replay with the same ``plan_id`` and identical fingerprint/content is a
    no-op. The same ``plan_id`` with different content is a hard conflict and is
    never allowed to overwrite the original conclusions.
    """
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        with connection:
            prior = connection.execute(
                "SELECT fingerprint, summary_json FROM takeover_plan WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()
            if prior is not None:
                if prior[0] == plan["fingerprint"] and \
                        json.loads(prior[1]) == plan["summary"]:
                    return False
                raise ValueError("takeover_plan_identity_conflict")
            connection.execute(
                "INSERT INTO takeover_plan VALUES (?,?,?,?,?,?,?,?,?)",
                (plan["plan_id"], plan["fingerprint"], plan["policy"],
                 plan["state"], plan["contract"]["fingerprint"],
                 plan["events"]["sha256"], plan["events_path"],
                 int(plan["generated_epoch"]), canonical_json(plan["summary"])),
            )
            for side in ("old", "new"):
                receipt = plan["receipts"][side]
                connection.execute(
                    "INSERT INTO takeover_receipt VALUES (?,?,?,?,?,?)",
                    (plan["plan_id"], side, receipt["batch_id"],
                     receipt.get("producer"), receipt.get("manifest_sha"),
                     receipt.get("source_root", "")),
                )
                for item in receipt.get("files", []):
                    connection.execute(
                        "INSERT INTO takeover_receipt_file VALUES (?,?,?,?,?,?)",
                        (plan["plan_id"], side, item["dimension"],
                         item["relative_path"], item["sha256"], int(item["rows"])),
                    )
            for ordinal, item in enumerate(plan["partitions"]):
                connection.execute(
                    "INSERT INTO takeover_partition VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan["plan_id"], ordinal,
                     canonical_json(item["service_date_key"]),
                     item.get("calendar_date"), item["conclusion"], item["reason"],
                     item["rows"], item["adopted_new_rows"],
                     item["retained_old_rows"], item["affected_rows"],
                     item["verified_rows"], item["pending_rows"],
                     canonical_json(item["stations"]),
                     canonical_json(item["transfers"]),
                     canonical_json(item["pending_evidence"])),
                )
            for item in _plan_events(plan):
                connection.execute(
                    "INSERT INTO takeover_event VALUES (?,?,?,?,?,?)",
                    (plan["plan_id"], item["ordinal"], item["station_code"],
                     item["from_line_code"], item["to_line_code"],
                     item["effective_at"]),
                )
        return True
    finally:
        connection.close()


def _plan_events(plan: dict[str, Any]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for partition in plan["partitions"]:
        for item in partition["transfers"]:
            seen.setdefault(int(item["ordinal"]), item)
    return [seen[key] for key in sorted(seen)]


def _db(database: str | Path) -> sqlite3.Connection | None:
    path = Path(database)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def read_plan(database: str | Path, plan_id: str) -> dict[str, Any] | None:
    connection = _db(database)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT * FROM takeover_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        partitions = [dict(item) for item in connection.execute(
            "SELECT * FROM takeover_partition WHERE plan_id=? ORDER BY ordinal",
            (plan_id,)).fetchall()]
        receipts = [dict(item) for item in connection.execute(
            "SELECT * FROM takeover_receipt WHERE plan_id=? ORDER BY side",
            (plan_id,)).fetchall()]
        files = [dict(item) for item in connection.execute(
            "SELECT * FROM takeover_receipt_file WHERE plan_id=? ORDER BY side, dimension",
            (plan_id,)).fetchall()]
        events = [dict(item) for item in connection.execute(
            "SELECT * FROM takeover_event WHERE plan_id=? ORDER BY ordinal",
            (plan_id,)).fetchall()]
    finally:
        connection.close()
    for item in partitions:
        for key in ("stations", "transfers", "pending_evidence"):
            item[key] = json.loads(item.pop(f"{key}_json"
                                            if key != "pending_evidence"
                                            else "evidence_json"))
        item["service_date_key"] = json.loads(item["service_date_key"])
    return {"plan": dict(row), "summary": json.loads(row["summary_json"]),
            "partitions": partitions, "receipts": receipts, "files": files,
            "events": events}


def latest_plan_id(database: str | Path) -> str | None:
    connection = _db(database)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT plan_id FROM takeover_plan ORDER BY rowid DESC LIMIT 1").fetchone()
        return row["plan_id"] if row else None
    finally:
        connection.close()


def verify_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Backward-compatible entry point; the implementation lives in takeover_verify."""
    from .takeover_verify import verify_plan as implementation
    return implementation(*args, **kwargs)
