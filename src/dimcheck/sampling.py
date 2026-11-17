"""Freeze a verifiable local finite-population sampling design."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_design(
  design_id TEXT PRIMARY KEY, manifest_sha TEXT NOT NULL,
  sample_sha TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sample_stratum(
  design_id TEXT NOT NULL REFERENCES sample_design(design_id),
  name TEXT NOT NULL, population_size INTEGER NOT NULL,
  sample_size INTEGER NOT NULL, frame_sha TEXT NOT NULL,
  PRIMARY KEY(design_id,name));
"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rank(seed: str, stratum: str, identity: str) -> str:
    return _sha(f"{seed}:{stratum}:{identity}".encode("utf-8"))


def inspect(manifest_path: str | Path, sample_csv: str | Path) -> dict:
    manifest_raw = Path(manifest_path).read_bytes()
    spec = json.loads(manifest_raw)
    if not isinstance(spec, dict) or not all(
        isinstance(spec.get(key), str) and spec[key]
        for key in ("design_id", "seed")
    ):
        raise ValueError("sample_design_invalid")
    strata = spec.get("strata")
    if not isinstance(strata, list) or not strata:
        raise ValueError("sample_strata_missing")
    observed_ids = set()
    summary = []
    names = set()
    for stratum in strata:
        if not isinstance(stratum, dict) or not isinstance(stratum.get("name"), str):
            raise ValueError("sample_stratum_invalid")
        name = stratum["name"]
        if not name or name in names:
            raise ValueError("sample_stratum_duplicate")
        names.add(name)
        frame = stratum.get("population_ids")
        selected = stratum.get("sample_ids")
        if not isinstance(frame, list) or not isinstance(selected, list) or not frame:
            raise ValueError("sample_frame_invalid")
        if any(not isinstance(item, str) or not item for item in frame + selected):
            raise ValueError("sample_identity_invalid")
        if len(set(frame)) != len(frame) or len(set(selected)) != len(selected):
            raise ValueError("sample_identity_duplicate")
        if set(frame) & observed_ids:
            raise ValueError("sample_frame_overlap")
        observed_ids.update(frame)
        expected = sorted(frame, key=lambda item: (_rank(spec["seed"], name, item), item))[:len(selected)]
        if not selected or selected != expected:
            raise ValueError("sample_selection_unverifiable")
        summary.append({"name": name, "population_size": len(frame),
                        "sample_size": len(selected),
                        "frame_sha": _sha(json.dumps(frame, separators=(",", ":")).encode())})
    sample_raw = Path(sample_csv).read_bytes()
    with Path(sample_csv).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "event_id" not in reader.fieldnames:
            raise ValueError("sample_csv_identity_missing")
        ids = [row["event_id"] for row in reader]
    if len(ids) != len(set(ids)) or set(ids) != {
        item for stratum in strata for item in stratum["sample_ids"]
    }:
        raise ValueError("sample_csv_selection_mismatch")
    return {"design_id": spec["design_id"], "manifest_sha": _sha(manifest_raw),
            "sample_sha": _sha(sample_raw), "state": "registered", "strata": summary}


def register(manifest_path: str | Path, sample_csv: str | Path,
             ledger_path: str | Path) -> dict:
    receipt = inspect(manifest_path, sample_csv)
    target = Path(ledger_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as db:
        db.executescript(SCHEMA)
        prior = db.execute("SELECT manifest_sha,sample_sha,state FROM sample_design WHERE design_id=?",
                           (receipt["design_id"],)).fetchone()
        if prior:
            if prior != (receipt["manifest_sha"], receipt["sample_sha"], "registered"):
                raise ValueError("sample_design_identity_conflict")
            return receipt
        db.execute("INSERT INTO sample_design VALUES (?,?,?,?)",
                   (receipt["design_id"], receipt["manifest_sha"],
                    receipt["sample_sha"], "registered"))
        db.executemany("INSERT INTO sample_stratum VALUES (?,?,?,?,?)", [
            (receipt["design_id"], item["name"], item["population_size"],
             item["sample_size"], item["frame_sha"]) for item in receipt["strata"]
        ])
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a finite-population sample design")
    parser.add_argument("manifest")
    parser.add_argument("sample_csv")
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    print(json.dumps(register(args.manifest, args.sample_csv, args.ledger),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
