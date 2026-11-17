"""Unit finalization steps shared by normal publishing and crash recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import ledger
from .pubprotocol import (ARTIFACT_FILES, ARTIFACT_NAMES, PUBLICATIONS_DIR,
                          PublicationFault, atomic_write, build_manifest,
                          fsync_dir, read_json, verify_unit)
from .pubview import quarantine
from .report import render_payloads


def write_run_and_manifest(unit_dir: Path, run_pk: int, publication_id: str,
                           report_digest: str) -> None:
    """Re-render run.json (now carrying the publication id) and the manifest."""
    report = read_json(unit_dir / "report.json")
    run_meta_doc = read_json(unit_dir / "run.json")
    assert report is not None and run_meta_doc is not None
    run_meta = {"started_at": run_meta_doc["started_at"],
                "duration_ms": int(run_meta_doc["duration_ms"])}
    payloads = render_payloads(report, run_meta, publication_id, report_digest)
    for key in ARTIFACT_FILES:
        atomic_write(unit_dir / ARTIFACT_NAMES[key], payloads[key])
    manifest = build_manifest(payloads, report_digest)
    atomic_write(
        unit_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    fsync_dir(unit_dir)


def finalize_pending(out: Path, connection, unit_dir: Path, run_pk: int,
                     staging_id: str, report_digest: str) -> str:
    """Promote a verified staged unit to its final id and commit the ledger."""
    publication_id = f"pub-{run_pk:06d}-{report_digest[:12]}"
    final_dir = out / PUBLICATIONS_DIR / publication_id
    if unit_dir != final_dir:
        if final_dir.exists():
            # Idempotent re-entry: the rename already happened.
            if verify_unit(final_dir, report_digest)[0]:
                unit_dir = final_dir
            else:
                raise PublicationFault("duplicate final unit is corrupt")
        else:
            os.rename(unit_dir, final_dir)
            unit_dir = final_dir
    write_run_and_manifest(final_dir, run_pk, publication_id, report_digest)
    ok, _digest, reason = verify_unit(final_dir, report_digest)
    if not ok:
        raise PublicationFault(f"finalized unit failed verification: {reason}")
    ledger.mark_publication_committed(
        connection, run_pk, publication_id,
        str(Path(PUBLICATIONS_DIR) / publication_id), report_digest)
    connection.commit()
    return publication_id


def repair_committed_unit(out: Path, connection, unit_dir: Path, run_pk: int,
                          publication_id: str, report_digest: str) -> None:
    """Finish a committed row whose rename/rewrite steps were interrupted.

    The ledger commit is the durable authority; the on-disk unit is rebuilt to
    match it.  Repeated repairs are idempotent and add no ledger rows.
    """
    final_dir = out / PUBLICATIONS_DIR / publication_id
    if unit_dir != final_dir:
        if final_dir.exists():
            if verify_unit(final_dir, report_digest)[0]:
                if unit_dir.exists():
                    quarantine(out, unit_dir, "superseded_duplicate")
                return
            raise PublicationFault("duplicate final unit is corrupt")
        os.rename(unit_dir, final_dir)
    write_run_and_manifest(final_dir, run_pk, publication_id, report_digest)
    ok, _digest, reason = verify_unit(final_dir, report_digest)
    if not ok:
        raise PublicationFault(f"repaired unit failed verification: {reason}")
    connection.execute(
        "UPDATE publications SET unit_path = ? WHERE run_pk = ?",
        (str(Path(PUBLICATIONS_DIR) / publication_id), run_pk))
    connection.commit()


def backfill_unit(out: Path, connection, unit_dir: Path, staging_id: str,
                  report_digest: str) -> str:
    """Legal recovery route 1: reconstruct the ledger row from verified files."""
    report = read_json(unit_dir / "report.json")
    run_meta_doc = read_json(unit_dir / "run.json")
    assert report is not None and run_meta_doc is not None
    run_meta = {"started_at": run_meta_doc["started_at"],
                "duration_ms": int(run_meta_doc["duration_ms"])}
    run_pk = ledger.insert_run(connection, report, run_meta, report_digest)
    ledger.insert_publication_pending(
        connection, run_pk, staging_id, report_digest,
        str(Path(PUBLICATIONS_DIR) / staging_id))
    return finalize_pending(out, connection, unit_dir, run_pk, staging_id,
                            report_digest)
