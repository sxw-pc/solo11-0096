"""Append-only local SQLite ledger for runs, findings, metrics, and publications.

Schema version 2 adds the publication protocol columns/tables.  Databases created
by schema version 1 are migrated in place: historical ``runs`` rows stay readable
and are explicitly marked ``legacy_unverified`` — they are never silently treated
as publications verified under the new protocol.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .pubprotocol import STATUS_COMMITTED, STATUS_ISOLATED, STATUS_LEGACY, STATUS_PENDING
from .util import canonical_json

DB_NAME = "ledger.sqlite3"

# Re-exported for callers that historically imported states from this module.
__all__ = ["DB_NAME", "STATUS_PENDING", "STATUS_COMMITTED", "STATUS_ISOLATED",
           "STATUS_LEGACY", "connect", "ensure_schema", "insert_run",
           "insert_publication_pending", "mark_publication_committed",
           "mark_publication_isolated", "record_isolated", "append_run",
           "read_runs", "read_findings", "read_metrics", "read_publications",
           "read_isolated_publications", "read_ledger"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    pk INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    contract_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    exit_code INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    errors INTEGER NOT NULL,
    warnings INTEGER NOT NULL,
    infos INTEGER NOT NULL,
    report_epoch INTEGER NOT NULL,
    publication_id TEXT,
    report_digest TEXT,
    publication_status TEXT NOT NULL DEFAULT 'legacy_unverified'
);
CREATE TABLE IF NOT EXISTS findings (
    pk INTEGER PRIMARY KEY AUTOINCREMENT,
    run_pk INTEGER NOT NULL REFERENCES runs(pk),
    ordinal INTEGER NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_name TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_snapshots (
    pk INTEGER PRIMARY KEY AUTOINCREMENT,
    run_pk INTEGER NOT NULL REFERENCES runs(pk),
    metric_name TEXT NOT NULL,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    run_pk INTEGER PRIMARY KEY REFERENCES runs(pk),
    publication_id TEXT UNIQUE,
    staging_id TEXT UNIQUE,
    report_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    isolated_reason TEXT,
    unit_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS isolated_publications (
    pk INTEGER PRIMARY KEY AUTOINCREMENT,
    staging_id TEXT,
    report_digest TEXT,
    reason TEXT NOT NULL,
    unit_path TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_pk, ordinal);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metric_snapshots(run_pk, metric_name);
CREATE INDEX IF NOT EXISTS idx_pub_status ON publications(status);
"""

# Columns added when migrating a version-1 database.  Every pre-existing row
# keeps NULL publication id/digest and the legacy default status.
_MIGRATION_COLUMNS = (
    ("publication_id", "TEXT"),
    ("report_digest", "TEXT"),
    ("publication_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
)


def _db_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / DB_NAME


def connect(out_dir: str | Path) -> sqlite3.Connection:
    path = _db_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    ensure_schema(connection)
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create protocol tables and migrate version-1 databases in place."""
    connection.executescript(SCHEMA)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    for column, declaration in _MIGRATION_COLUMNS:
        if column not in existing:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {declaration}")
    connection.commit()


def insert_run(connection: sqlite3.Connection, report: dict[str, Any],
               run_meta: dict[str, Any],
               report_digest: str | None = None) -> int:
    """Insert one run plus findings/metrics on the given connection.

    Caller owns the transaction so publication bookkeeping can commit together
    with the run records.
    """
    summary = report["summary"]
    cursor = connection.execute(
        """INSERT INTO runs
        (run_id, tool_version, contract_fingerprint, started_at, duration_ms,
         exit_code, verdict, errors, warnings, infos, report_epoch,
         publication_id, report_digest, publication_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (report["run_id"], report["tool"]["version"],
         report["contract"]["fingerprint"], run_meta["started_at"],
         int(run_meta["duration_ms"]), report["exit_code"], report["verdict"],
         summary["errors"], summary["warnings"], summary["infos"],
         report["generated_epoch"], report_digest, STATUS_PENDING),
    )
    run_pk = int(cursor.lastrowid)
    connection.executemany(
        """INSERT INTO findings
        (run_pk, ordinal, code, severity, object_type, object_name, message,
         evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(run_pk, index, item["code"], item["severity"], item["object_type"],
          item["object_name"], item["message"], canonical_json(item["evidence"]))
         for index, item in enumerate(report["findings"])],
    )
    connection.executemany(
        "INSERT INTO metric_snapshots (run_pk, metric_name, value_json) "
        "VALUES (?, ?, ?)",
        [(run_pk, name, canonical_json(value))
         for name, value in sorted(report["metrics"].items())],
    )
    return run_pk


def insert_publication_pending(connection: sqlite3.Connection, run_pk: int,
                               staging_id: str, report_digest: str,
                               unit_path: str) -> None:
    connection.execute(
        """INSERT INTO publications
        (run_pk, publication_id, staging_id, report_digest, status, unit_path)
        VALUES (?, NULL, ?, ?, ?, ?)""",
        (run_pk, staging_id, report_digest, STATUS_PENDING, unit_path),
    )


def mark_publication_committed(connection: sqlite3.Connection, run_pk: int,
                               publication_id: str, unit_path: str,
                               report_digest: str | None = None) -> None:
    connection.execute(
        "UPDATE publications SET publication_id = ?, status = ?, unit_path = ? "
        "WHERE run_pk = ?",
        (publication_id, STATUS_COMMITTED, unit_path, run_pk),
    )
    if report_digest is None:
        connection.execute(
            "UPDATE runs SET publication_id = ?, publication_status = ? WHERE pk = ?",
            (publication_id, STATUS_COMMITTED, run_pk),
        )
    else:
        connection.execute(
            "UPDATE runs SET publication_id = ?, report_digest = ?, "
            "publication_status = ? WHERE pk = ?",
            (publication_id, report_digest, STATUS_COMMITTED, run_pk),
        )


def mark_publication_isolated(connection: sqlite3.Connection, run_pk: int | None,
                              reason: str) -> None:
    """Flip publication state to isolated; run history itself is never deleted."""
    if run_pk is not None:
        connection.execute(
            "UPDATE publications SET status = ?, isolated_reason = ? WHERE run_pk = ?",
            (STATUS_ISOLATED, reason, run_pk),
        )
        connection.execute(
            "UPDATE runs SET publication_status = ? WHERE pk = ?",
            (STATUS_ISOLATED, run_pk),
        )


def record_isolated(connection: sqlite3.Connection, reason: str, unit_path: str,
                    staging_id: str | None, report_digest: str | None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO isolated_publications "
        "(staging_id, report_digest, reason, unit_path) VALUES (?, ?, ?, ?)",
        (staging_id, report_digest, reason, unit_path),
    )


def append_run(out_dir: str | Path, report: dict[str, Any],
               run_meta: dict[str, Any]) -> int:
    """Backward-compatible entry point: publish under the full protocol."""
    from .publishing import publish_run
    return int(publish_run(out_dir, report, run_meta)["run_pk"])


# Read helpers live in ledger_read to keep both modules within line budget;
# re-exported here so ``from dimcheck.ledger import read_runs`` keeps working.
from .ledger_read import (  # noqa: E402
    read_findings, read_isolated_publications, read_ledger, read_metrics,
    read_publications, read_runs,
)
