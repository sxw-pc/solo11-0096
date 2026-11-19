"""Local dimension delivery intake, separate from model audit runs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batch(
  batch_id TEXT PRIMARY KEY, producer TEXT NOT NULL,
  manifest_sha TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_file(
  batch_id TEXT NOT NULL REFERENCES intake_batch(batch_id),
  dimension TEXT NOT NULL, relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL, rows INTEGER NOT NULL,
  PRIMARY KEY(batch_id, dimension));
"""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _inside(root: Path, relative: str) -> Path:
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts or not name.parts:
        raise ValueError("intake_path_outside_root")
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("intake_path_outside_root")
    return target


def inspect_manifest(manifest_path: str | Path, source_root: str | Path) -> dict:
    """Validate a delivery without mutating source files or the ledger."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not all(
        isinstance(manifest.get(key), str) and manifest[key]
        for key in ("batch_id", "producer")
    ):
        raise ValueError("intake_identity_missing")
    specs = manifest.get("dimensions")
    if not isinstance(specs, list) or not specs:
        raise ValueError("intake_dimensions_missing")
    root = Path(source_root).resolve()
    seen = set()
    files = []
    for spec in specs:
        if not isinstance(spec, dict) or not all(
            isinstance(spec.get(key), str) and spec[key]
            for key in ("name", "source", "sha256")
        ):
            raise ValueError("intake_file_invalid")
        if spec["name"] in seen:
            raise ValueError("intake_dimension_duplicate")
        seen.add(spec["name"])
        source = _inside(root, spec["source"])
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != spec["sha256"]:
            raise ValueError(f"intake_hash_mismatch:{spec['name']}")
        if source.suffix.lower() != ".csv":
            raise ValueError("intake_csv_required")
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        files.append({"dimension": spec["name"], "source": spec["source"],
                      "sha256": digest, "rows": len(rows)})
    return {"batch_id": manifest["batch_id"], "producer": manifest["producer"],
            "manifest_sha": hashlib.sha256(_canonical(manifest)).hexdigest(),
            "files": sorted(files, key=lambda item: item["dimension"])}


def read_receipt(ledger_path: str | Path, batch_id: str) -> dict:
    """Read one registered receipt back from the append-only intake ledger.

    The receipt contains the frozen per-file digests; it deliberately does not
    contain a re-parsed copy of the source rows. Callers must re-verify the
    files against ``source_root`` before trusting them.
    """
    database = Path(ledger_path)
    if not database.is_file():
        raise ValueError("intake_receipt_missing")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        try:
            batch = connection.execute(
                "SELECT batch_id, producer, manifest_sha, state "
                "FROM intake_batch WHERE batch_id=?", (batch_id,),
            ).fetchone()
            files = connection.execute(
                "SELECT dimension, relative_path, sha256, rows FROM intake_file "
                "WHERE batch_id=? ORDER BY dimension", (batch_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("intake_receipt_missing") from exc
    if batch is None:
        raise ValueError("intake_receipt_missing")
    return {
        "batch_id": batch["batch_id"], "producer": batch["producer"],
        "manifest_sha": batch["manifest_sha"], "state": batch["state"],
        "files": [dict(item) for item in files],
    }


def verify_receipt(receipt: dict, source_root: str | Path) -> dict:
    """Re-hash the registered delivery files and recount their CSV rows.

    This is the independent verification entry: it never trusts a stored digest
    or row count by itself. A changed source byte or row count is reported as an
    issue, which makes any plan that cites the receipt pending instead of
    confirmed.
    """
    root = Path(source_root).resolve()
    issues: list[dict] = []
    verified: list[dict] = []
    for item in receipt.get("files") or []:
        dimension = item.get("dimension")
        relative = item.get("relative_path")
        try:
            target = _inside(root, str(relative))
        except ValueError:
            issues.append({"code": "DIM-TO-005", "dimension": dimension,
                           "message": "收据路径越界",
                           "evidence": {"relative_path": relative}})
            continue
        if target.suffix.lower() != ".csv":
            issues.append({"code": "DIM-TO-005", "dimension": dimension,
                           "message": "收据不是 CSV 交付",
                           "evidence": {"relative_path": relative}})
            continue
        try:
            raw = target.read_bytes()
        except OSError as exc:
            issues.append({"code": "DIM-TO-005", "dimension": dimension,
                           "message": "收据来源文件不可读",
                           "evidence": {"relative_path": relative,
                                        "error": type(exc).__name__}})
            continue
        digest = hashlib.sha256(raw).hexdigest()
        with target.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = sum(1 for _ in csv.DictReader(stream))
        if digest != item.get("sha256"):
            issues.append({"code": "DIM-TO-005", "dimension": dimension,
                           "message": "收据来源字节已变化",
                           "evidence": {"relative_path": relative,
                                        "expected": item.get("sha256"),
                                        "actual": digest}})
            continue
        if rows != item.get("rows"):
            issues.append({"code": "DIM-TO-005", "dimension": dimension,
                           "message": "收据来源行数已变化",
                           "evidence": {"relative_path": relative,
                                        "expected": item.get("rows"),
                                        "actual": rows}})
            continue
        verified.append({"dimension": dimension, "relative_path": relative,
                         "path": str(target), "sha256": digest, "rows": rows})
    return {"ok": not issues, "issues": issues, "files": verified}


def register(manifest_path: str | Path, source_root: str | Path,
             ledger_path: str | Path) -> dict:
    """Freeze a verified local delivery as an immutable intake receipt."""
    receipt = inspect_manifest(manifest_path, source_root)
    database = Path(ledger_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        prior = connection.execute(
            "SELECT producer, manifest_sha, state FROM intake_batch WHERE batch_id=?",
            (receipt["batch_id"],),
        ).fetchone()
        if prior:
            if prior != (receipt["producer"], receipt["manifest_sha"], "registered"):
                raise ValueError("intake_batch_identity_conflict")
            return receipt
        connection.execute("INSERT INTO intake_batch VALUES (?,?,?,?)",
                           (receipt["batch_id"], receipt["producer"],
                            receipt["manifest_sha"], "registered"))
        connection.executemany("INSERT INTO intake_file VALUES (?,?,?,?,?)", [
            (receipt["batch_id"], item["dimension"], item["source"],
             item["sha256"], item["rows"]) for item in receipt["files"]
        ])
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a local dimension delivery")
    parser.add_argument("manifest")
    parser.add_argument("--root", required=True)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    print(json.dumps(register(args.manifest, args.root, args.ledger),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
