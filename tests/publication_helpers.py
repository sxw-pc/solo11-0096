"""Shared helpers for the publication-protocol regression tests."""
from __future__ import annotations

from pathlib import Path

from dimcheck.cli import run_once
from dimcheck.pubprotocol import CURRENT_LINK

FACT = "fct_sales.csv"
FACT_HEADER = ["sale_id", "customer_key", "product_key", "date_key", "amount",
               "quantity", "avg_price", "on_hand"]


def publish(project, out: Path):
    return run_once(project.contract, out, quiet=True)


def unit_dirs(out: Path) -> list[Path]:
    root = out / "publications"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def staging_dirs(out: Path) -> list[Path]:
    return [p for p in unit_dirs(out) if p.name.startswith("staging-")]


def current_unit(out: Path) -> Path:
    return (out / CURRENT_LINK).resolve()
