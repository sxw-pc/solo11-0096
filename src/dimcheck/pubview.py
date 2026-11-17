"""Latest-view switching, quarantine, and legacy output-directory migration."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import ledger
from .pubprotocol import (ARTIFACT_NAMES, CURRENT_LINK, LEGACY_UNIT,
                          MIGRATION_MARK, PUBLICATIONS_DIR, QUARANTINE_DIR,
                          atomic_write, build_manifest, fsync_dir,
                          report_digest_of)


def relative_symlink(link: Path, target: Path) -> None:
    """Atomically (re)point a symlink at a target via a temp link + rename."""
    tmp = link.with_name(link.name + ".linktmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(os.path.relpath(target, link.parent), tmp)
    os.replace(tmp, link)


def quarantine(out: Path, unit_dir: Path, reason: str) -> Path:
    """Move an unverifiable unit out of the publications area.

    A missing directory (only the ledger row survived) is recorded as an empty
    quarantine slot instead of raising.
    """
    quarantine_root = out / QUARANTINE_DIR
    quarantine_root.mkdir(parents=True, exist_ok=True)
    destination = quarantine_root / unit_dir.name
    counter = 1
    while destination.exists():
        destination = quarantine_root / f"{unit_dir.name}-{counter}"
        counter += 1
    if unit_dir.exists():
        os.replace(unit_dir, destination)
        fsync_dir(quarantine_root)
    else:
        destination.mkdir(parents=True, exist_ok=True)
    return destination


def _committed_targets(out: Path, connection) -> list[tuple[int, str, Path]]:
    rows = connection.execute(
        "SELECT run_pk, publication_id, unit_path FROM publications "
        "WHERE status = ? ORDER BY run_pk DESC", (ledger.STATUS_COMMITTED,)
    ).fetchall()
    result = []
    for run_pk, publication_id, unit_path in rows:
        unit_dir = out / unit_path
        if unit_dir.is_dir():
            result.append((run_pk, publication_id, unit_dir))
    return result


def reconcile_view(out: Path, connection) -> str | None:
    """Point ``current`` and the top-level report files at one committed unit.

    Returns the active publication id, or ``None`` when nothing is verified.
    The single ``current`` rename is the only switch: every top-level report
    link resolves through it, so concurrent readers observe either the previous
    complete unit or the new complete unit — never a mixture.
    """
    targets = _committed_targets(out, connection)
    if not targets:
        # No protocol-verified unit: keep a readable legacy view if one exists,
        # but never leave a dangling pointer at a quarantined unit.
        current = out / CURRENT_LINK
        if current.is_symlink() and not current.exists():
            current.unlink()
        return None
    _run_pk, publication_id, unit_dir = targets[0]
    current = out / CURRENT_LINK
    relative_symlink(current, unit_dir)
    for name in ARTIFACT_NAMES.values():
        relative_symlink(out / name, current / name)
    return publication_id


def migrate_legacy_view(out: Path, connection) -> None:
    """Snapshot flat report.* files into a legacy unit, never marked verified.

    Pre-protocol directories keep their history readable, but the snapshot is
    registered with reason ``legacy_unverified`` and has no committed
    publication row — nothing is silently treated as protocol-verified.
    """
    legacy = [out / name for name in ARTIFACT_NAMES.values()]
    if not all(path.is_file() and not path.is_symlink() for path in legacy):
        return
    # Idempotency is keyed on the on-disk unit, never on the ledger row: a
    # previous interrupted migration may have registered the row before dying.
    unit_dir = out / PUBLICATIONS_DIR / LEGACY_UNIT
    if unit_dir.exists() or (out / MIGRATION_MARK).exists():
        return
    unit_dir.mkdir(parents=True, exist_ok=True)
    for path in legacy:
        data = path.read_bytes()
        target = unit_dir / path.name
        with target.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    payloads = {key: (unit_dir / name).read_text(encoding="utf-8")
                for key, name in ARTIFACT_NAMES.items()}
    manifest = build_manifest(payloads, report_digest_of(unit_dir))
    atomic_write(
        unit_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    fsync_dir(unit_dir)
    connection.execute(
        "INSERT OR IGNORE INTO isolated_publications "
        "(staging_id, report_digest, reason, unit_path) VALUES (NULL, ?, ?, ?)",
        (manifest["report_digest"], "legacy_unverified",
         str(Path(PUBLICATIONS_DIR) / LEGACY_UNIT)),
    )
    connection.commit()
    current = out / CURRENT_LINK
    relative_symlink(current, unit_dir)
    for name in ARTIFACT_NAMES.values():
        old = out / name
        old.unlink()
        relative_symlink(old, current / name)
    atomic_write(out / MIGRATION_MARK,
                 json.dumps({"status": "legacy_unverified"}, sort_keys=True) + "\n")
    fsync_dir(out)
