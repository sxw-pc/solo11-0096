"""Crash recovery: settle unfinished publications before run or serve.

Recovery always re-hashes actual files instead of trusting status fields.  Two
legal decisions exist for a fully-written unit whose ledger row is missing:

* verifiable run metadata cross-checking the report body → back-fill the ledger
  and make the unit the newest published result;
* unverifiable metadata or digests → quarantine the unit and keep the previous
  successful publication as the latest view.

Every transition is idempotent and keyed by ``staging_id``/primary key, so
repeated recovery never duplicates ledger rows.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import ledger
from .pubfinalize import backfill_unit, finalize_pending, repair_committed_unit
from .pubprotocol import (LEGACY_UNIT, PUBLICATIONS_DIR, QUARANTINE_DIR,
                          out_lock, run_metadata_matches, verify_unit)
from .pubview import migrate_legacy_view, quarantine, reconcile_view


def _isolate(out: Path, connection, unit_dir: Path, reason: str,
             run_pk: int | None, staging_id: str | None,
             digest: str | None) -> str:
    destination = quarantine(out, unit_dir, reason)
    ledger.record_isolated(connection, reason,
                           str(Path(QUARANTINE_DIR) / destination.name),
                           staging_id, digest)
    ledger.mark_publication_isolated(connection, run_pk, reason)
    connection.commit()
    return str(destination)


def _recover_staging_dirs(out: Path, pubs_root: Path, connection,
                          result: dict) -> None:
    staging = sorted(
        entry for entry in pubs_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(LEGACY_UNIT)
        and not entry.name.startswith("pub-")) if pubs_root.is_dir() else []
    for unit_dir in staging:
        staging_id = unit_dir.name
        row = connection.execute(
            "SELECT run_pk, publication_id, report_digest, status "
            "FROM publications WHERE staging_id = ?", (staging_id,)
        ).fetchone()
        ok, actual, reason = verify_unit(unit_dir)
        if not ok:
            run_pk = row[0] if row else None
            wanted = row[2] if row else None
            destination = _isolate(out, connection, unit_dir,
                                   reason or "verification_failed",
                                   run_pk, staging_id, wanted or actual)
            result["isolated"].append(
                {"staging_id": staging_id, "reason": reason,
                 "unit_path": destination})
            continue
        assert actual is not None
        if row is None:
            # Files fully on disk but the ledger never saw the run: back-fill
            # only when the metadata cross-checks, otherwise isolate.
            if not run_metadata_matches(unit_dir):
                destination = _isolate(out, connection, unit_dir,
                                       "metadata_unverifiable", None,
                                       staging_id, actual)
                result["isolated"].append(
                    {"staging_id": staging_id, "reason": "metadata_unverifiable",
                     "unit_path": destination})
                continue
            result["backfilled"].append(
                backfill_unit(out, connection, unit_dir, staging_id, actual))
        elif row[3] == ledger.STATUS_PENDING:
            if not run_metadata_matches(unit_dir):
                destination = _isolate(out, connection, unit_dir,
                                       "metadata_unverifiable", row[0],
                                       staging_id, actual)
                result["isolated"].append(
                    {"staging_id": staging_id, "reason": "metadata_unverifiable",
                     "unit_path": destination})
                continue
            result["backfilled"].append(
                finalize_pending(out, connection, unit_dir, row[0],
                                 staging_id, actual))
        elif row[3] == ledger.STATUS_COMMITTED:
            publication_id = row[1]
            if publication_id is None:
                destination = _isolate(out, connection, unit_dir,
                                       "committed_without_publication_id",
                                       row[0], staging_id, actual)
                result["isolated"].append(
                    {"staging_id": staging_id,
                     "reason": "committed_without_publication_id",
                     "unit_path": destination})
                continue
            repair_committed_unit(out, connection, unit_dir, row[0],
                                  publication_id, actual)
        else:
            destination = _isolate(out, connection, unit_dir,
                                   f"row_status_{row[3]}", row[0],
                                   staging_id, actual)
            result["isolated"].append(
                {"staging_id": staging_id, "reason": f"row_status_{row[3]}",
                 "unit_path": destination})


def _recover_pending_rows(out: Path, connection, result: dict) -> None:
    for row in connection.execute(
            "SELECT run_pk, staging_id, publication_id, report_digest, "
            "unit_path FROM publications WHERE status = ?",
            (ledger.STATUS_PENDING,)).fetchall():
        run_pk, staging_id, publication_id, wanted_digest, unit_path = row
        candidate = out / unit_path
        final_candidate = (out / PUBLICATIONS_DIR / publication_id) \
            if publication_id else None
        unit_dir = candidate if candidate.is_dir() else (
            final_candidate if final_candidate and final_candidate.is_dir()
            else None)
        if unit_dir is None:
            ledger.record_isolated(
                connection, "unit_missing",
                str(Path(QUARANTINE_DIR) / (staging_id or "missing")),
                staging_id, wanted_digest)
            ledger.mark_publication_isolated(connection, run_pk, "unit_missing")
            connection.commit()
            result["isolated"].append(
                {"staging_id": staging_id, "reason": "unit_missing",
                 "unit_path": unit_path})
            continue
        ok, actual, reason = verify_unit(unit_dir, wanted_digest)
        if not ok or not run_metadata_matches(unit_dir):
            why = reason or "metadata_unverifiable"
            destination = _isolate(out, connection, unit_dir, why,
                                   run_pk, staging_id, actual or wanted_digest)
            result["isolated"].append(
                {"staging_id": staging_id, "reason": why,
                 "unit_path": destination})
            continue
        result["backfilled"].append(
            finalize_pending(out, connection, unit_dir, run_pk,
                             staging_id, actual))


def _recover_orphan_units(out: Path, pubs_root: Path, connection,
                          result: dict) -> None:
    if not pubs_root.is_dir():
        return
    known = {out / row[0] for row in connection.execute(
        "SELECT unit_path FROM publications").fetchall()}
    for unit_dir in sorted(pubs_root.iterdir()):
        if not unit_dir.is_dir() or unit_dir.name.startswith(LEGACY_UNIT) \
                or unit_dir in known:
            continue
        ok, actual, reason = verify_unit(unit_dir)
        if not ok or not run_metadata_matches(unit_dir):
            destination = _isolate(out, connection, unit_dir,
                                   reason or "orphan_unverifiable", None,
                                   unit_dir.name, actual)
            result["isolated"].append(
                {"staging_id": unit_dir.name,
                 "reason": reason or "orphan_unverifiable",
                 "unit_path": destination})
            continue
        result["backfilled"].append(
            backfill_unit(out, connection, unit_dir, unit_dir.name, actual))


def _recover_corrupt_committed(out: Path, connection, result: dict) -> None:
    for run_pk, publication_id, unit_path in connection.execute(
            "SELECT run_pk, publication_id, unit_path FROM publications "
            "WHERE status = ?", (ledger.STATUS_COMMITTED,)).fetchall():
        unit_dir = out / unit_path
        ok, _actual, reason = verify_unit(unit_dir)
        if ok:
            continue
        destination = quarantine(out, unit_dir, reason or "verification_failed")
        ledger.record_isolated(
            connection, reason or "verification_failed",
            str(Path(QUARANTINE_DIR) / destination.name), None, None)
        ledger.mark_publication_isolated(
            connection, run_pk, reason or "verification_failed")
        connection.commit()
        result["isolated"].append(
            {"staging_id": publication_id, "reason": reason,
             "unit_path": str(destination)})


def _repair_dangling_view(out: Path, connection) -> None:
    """Keep ``current`` from pointing at a quarantined/missing unit."""
    current = out / "current"
    if current.is_symlink() and not current.exists():
        current.unlink()
    reconcile_view(out, connection)


def recover(out_dir: str | Path) -> dict[str, Any]:
    """Reconcile the output directory before a run or serve."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = {"backfilled": [], "isolated": [], "active_publication_id": None}
    with out_lock(out):
        connection = ledger.connect(out)
        try:
            # Schema now exists, so legacy migration can register its row.
            migrate_legacy_view(out, connection)
            pubs_root = out / PUBLICATIONS_DIR
            _recover_staging_dirs(out, pubs_root, connection, result)
            _recover_pending_rows(out, connection, result)
            _recover_orphan_units(out, pubs_root, connection, result)
            _recover_corrupt_committed(out, connection, result)
            _repair_dangling_view(out, connection)
            result["active_publication_id"] = reconcile_view(out, connection)
            return result
        finally:
            connection.close()
