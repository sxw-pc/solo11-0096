"""Shared helpers and fixtures for takeover regression tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dimcheck.contract import load_contract
from dimcheck.intake import register
from dimcheck.takeover import build_plan

ROOT = Path(__file__).resolve().parents[1]
TAKEOVER = ROOT / "fixtures" / "takeover"
CONTRACT = TAKEOVER / "model.yaml"

BATCHES = {
    "old": ("batch-old", "producer-a", "old"),
    "new": ("batch-new", "producer-b", "new-clean"),
    "conflict": ("batch-conflict", "producer-c", "new-conflict"),
    "reuse": ("batch-reuse", "producer-d", "new-surrogate-reuse"),
}


def write_manifest(path: Path, batch: str, producer: str,
                   folder: str) -> Path:
    files = []
    for dimension in ("dim_line", "dim_station"):
        source = TAKEOVER / folder / f"{dimension}.csv"
        files.append({"name": dimension, "source": f"{folder}/{dimension}.csv",
                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    path.write_text(json.dumps({"batch_id": batch, "producer": producer,
                                "dimensions": files}), encoding="utf-8")
    return path


def register_batches(tmp_path: Path, ledger: Path,
                     names: tuple[str, ...] = ("old", "new")) -> dict[str, str]:
    result = {}
    for name in names:
        batch, producer, folder = BATCHES[name]
        manifest = write_manifest(tmp_path / f"{batch}.json", batch, producer, folder)
        register(manifest, TAKEOVER, ledger)
        result[name] = batch
    return result


def receipt(batch: str, root: Path = TAKEOVER) -> dict:
    return {"batch_id": batch, "source_root": str(root)}


def plan(ledger: Path, old: str, new: str, policy: str,
         events: Path | None = None, contract: Path = CONTRACT) -> dict:
    return build_plan(load_contract(contract), str(ledger),
                      [receipt(old), receipt(new)],
                      str(events or TAKEOVER / "transfers.json"), policy, 1767225600)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "intake.sqlite3"
