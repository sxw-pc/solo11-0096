"""Read-side helpers for the append-only ledger.

Kept separate from the schema/write module so both stay inside the per-file
line budget and readers never import the publishing write path.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .ledger import DB_NAME, ensure_schema


def _read_connection(out_dir: str | Path) -> sqlite3.Connection | None:
    path = Path(out_dir) / DB_NAME
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
        ensure_schema(connection)
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY pk DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_findings(out_dir: str | Path, run_pk: int,
                  limit: int = 200) -> list[dict[str, Any]]:
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


def read_publications(out_dir: str | Path) -> list[dict[str, Any]]:
    """All publication rows, newest first (committed, pending, isolated, ...)."""
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        ensure_schema(connection)
        rows = connection.execute(
            "SELECT * FROM publications ORDER BY run_pk DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_isolated_publications(out_dir: str | Path) -> list[dict[str, Any]]:
    connection = _read_connection(out_dir)
    if connection is None:
        return []
    try:
        ensure_schema(connection)
        rows = connection.execute(
            "SELECT * FROM isolated_publications ORDER BY pk DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_ledger(out_dir: str | Path, run_limit: int = 10,
                finding_limit: int = 200) -> dict[str, Any]:
    runs = read_runs(out_dir, run_limit)
    findings = read_findings(out_dir, runs[0]["pk"], finding_limit) if runs else []
    metrics = read_metrics(out_dir, runs[0]["pk"]) if runs else []
    return {"runs": runs, "latest_findings": findings,
            "latest_metrics": {item["metric_name"]: item["value"]
                               for item in metrics}}
