"""Append-only local SQLite ledger for runs, findings, and metric snapshots."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import canonical_json

DB_NAME = "ledger.sqlite3"
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
    report_epoch INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_pk, ordinal);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metric_snapshots(run_pk, metric_name);
CREATE TABLE IF NOT EXISTS aggregation_path_runs (
    pk INTEGER PRIMARY KEY AUTOINCREMENT,
    run_pk INTEGER NOT NULL REFERENCES runs(pk),
    run_id TEXT NOT NULL,
    path_key TEXT NOT NULL,
    fact TEXT NOT NULL,
    path_name TEXT NOT NULL,
    measure TEXT NOT NULL,
    retain TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    fact_rows INTEGER,
    participating_rows INTEGER,
    lost_rows INTEGER,
    amplified_rows INTEGER,
    total_contributions INTEGER,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agg_paths_run
    ON aggregation_path_runs(run_pk, path_name);
CREATE INDEX IF NOT EXISTS idx_agg_paths_run_id
    ON aggregation_path_runs(run_id, path_name);
"""


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Add new tables to legacy ledgers in place without touching history."""
    connection.executescript(SCHEMA)


def _path_rows(report: dict[str, Any]) -> list[tuple]:
    metric = report.get("metrics", {}).get("aggregation_paths") or {}
    rows = []
    for key, entry in sorted((metric.get("paths") or {}).items()):
        counts = entry.get("counts") or {}
        rows.append((
            key, entry.get("fact", ""), entry.get("path", key.rsplit(".", 1)[-1]),
            entry.get("measure", ""), entry.get("retain", ""), entry.get("verdict", ""),
            entry.get("reason", ""), counts.get("fact_rows"),
            counts.get("participating_rows"), counts.get("lost_rows"),
            counts.get("amplified_rows"), counts.get("total_contributions"),
            canonical_json(entry),
        ))
    return rows


def _db_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / DB_NAME


def append_run(out_dir: str | Path, report: dict[str, Any],
               run_meta: dict[str, Any]) -> int:
    path = _db_path(out_dir)
    connection = sqlite3.connect(path)
    try:
        _ensure_schema(connection)
        summary = report["summary"]
        with connection:
            cursor = connection.execute(
                """INSERT INTO runs
                (run_id, tool_version, contract_fingerprint, started_at, duration_ms,
                 exit_code, verdict, errors, warnings, infos, report_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (report["run_id"], report["tool"]["version"],
                 report["contract"]["fingerprint"], run_meta["started_at"],
                 int(run_meta["duration_ms"]), report["exit_code"], report["verdict"],
                 summary["errors"], summary["warnings"], summary["infos"],
                 report["generated_epoch"]),
            )
            run_pk = int(cursor.lastrowid)
            findings = report["findings"]
            connection.executemany(
                """INSERT INTO findings
                (run_pk, ordinal, code, severity, object_type, object_name, message,
                 evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_pk, index, item["code"], item["severity"], item["object_type"],
                  item["object_name"], item["message"], canonical_json(item["evidence"]))
                 for index, item in enumerate(findings)],
            )
            connection.executemany(
                "INSERT INTO metric_snapshots (run_pk, metric_name, value_json) VALUES (?, ?, ?)",
                [(run_pk, name, canonical_json(value))
                 for name, value in sorted(report["metrics"].items())],
            )
            connection.executemany(
                """INSERT INTO aggregation_path_runs
                (run_pk, run_id, path_key, fact, path_name, measure, retain, verdict,
                 reason, fact_rows, participating_rows, lost_rows, amplified_rows,
                 total_contributions, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_pk, report["run_id"], *row) for row in _path_rows(report)],
            )
        return run_pk
    finally:
        connection.close()


def _read_connection(out_dir: str | Path) -> sqlite3.Connection | None:
    path = _db_path(out_dir)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def read_runs(out_dir: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY pk DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_findings(out_dir: str | Path, run_pk: int, limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 5000))
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM findings WHERE run_pk = ? ORDER BY ordinal LIMIT ?",
            (int(run_pk), limit),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result
    finally:
        connection.close()


def read_metrics(out_dir: str | Path, run_pk: int) -> list[dict[str, Any]]:
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT metric_name, value_json FROM metric_snapshots "
            "WHERE run_pk = ? ORDER BY metric_name", (int(run_pk),)
        ).fetchall()
        return [{"metric_name": row["metric_name"],
                 "value": json.loads(row["value_json"])} for row in rows]
    finally:
        connection.close()


def read_aggregation_paths(out_dir: str | Path, run_pk: int) -> list[dict[str, Any]]:
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT * FROM aggregation_path_runs WHERE run_pk = ? ORDER BY path_name",
                (int(run_pk),)).fetchall()
        except sqlite3.OperationalError:  # unmigrated legacy ledger
            return []
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
    finally:
        connection.close()


def read_path_verdicts(out_dir: str | Path, run_id: str) -> list[dict[str, Any]]:
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT run_pk, run_id, path_key, path_name, verdict, reason, "
                "total_contributions FROM aggregation_path_runs WHERE run_id = ? "
                "ORDER BY run_pk, path_name", (run_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_ledger(out_dir: str | Path, run_limit: int = 10,
                finding_limit: int = 200) -> dict[str, Any]:
    runs = read_runs(out_dir, run_limit)
    findings = read_findings(out_dir, runs[0]["pk"], finding_limit) if runs else []
    metrics = read_metrics(out_dir, runs[0]["pk"]) if runs else []
    paths = read_aggregation_paths(out_dir, runs[0]["pk"]) if runs else []
    return {"runs": runs, "latest_findings": findings,
            "latest_metrics": {item["metric_name"]: item["value"] for item in metrics},
            "latest_aggregation_paths": paths}
