"""Immutable publication units: orchestration of the publish protocol.

Every completed audit is published as one *immutable publication unit* under
``out/publications/`` containing the four report artifacts plus a manifest with
per-file SHA-256 digests.  The unit directory name is its ``publication_id``::

    pub-<runs.pk>-<report_digest[:12]>

so the id is derived from the ledger auto-increment primary key *and* a content
digest — a contract fingerprint alone cannot distinguish two audits of changed
sample data.

The three failure boundaries (file write, database commit, latest-view switch)
cannot share one native transaction.  They are separated into idempotent steps
reconciled under a directory-wide ``flock`` (see :mod:`dimcheck.recovery`);
recovery re-hashes actual files and either back-fills a verifiable unit into
the ledger or isolates it.  The user-facing ``out/report.*`` files are fixed
symlinks through one atomically-switched ``out/current`` pointer, so a reader
can only ever observe the old complete unit or the new complete unit.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from . import ledger
from .pubfinalize import finalize_pending
from .pubprotocol import (ARTIFACT_FILES, ARTIFACT_NAMES, CURRENT_LINK,
                          DIGEST_FILES, PUBLICATIONS_DIR, FAULT_DB_COMMIT,
                          FAULT_FILE_WRITE, FAULT_PROMOTE, FAULT_VIEW_SWITCH,
                          FAULT_WINDOWS, PublicationFault, _fault,
                          atomic_write, build_manifest, fsync_dir, out_lock,
                          verify_unit)
from .pubview import relative_symlink
from .recovery import recover
from .report import render_payloads

__all__ = ["PublicationFault", "FAULT_WINDOWS", "publish_run", "recover",
           "out_lock", "verify_unit"]


def publish_run(out_dir: str | Path, report: dict[str, Any],
                run_meta: dict[str, Any]) -> dict[str, Any]:
    """Publish one audit as an immutable unit and make it the latest view."""
    out = Path(out_dir)
    with out_lock(out):
        # Settle any interrupted publication from a previous process first.
        recover(out)
        connection = ledger.connect(out)
        try:
            # --- Step 1: write the unit into a content-addressed staging dir.
            initial = render_payloads(report, run_meta)
            probe = hashlib.sha256()
            for key in DIGEST_FILES:
                probe.update(hashlib.sha256(
                    initial[key].encode("utf-8")).hexdigest().encode("ascii"))
            report_digest = probe.hexdigest()
            # The nonce keeps distinct runs apart even when their report
            # content is byte-identical; it names only the temporary staging
            # directory and never enters the deterministic report bodies.
            staging_id = f"staging-{report_digest[:16]}-{secrets.token_hex(4)}"
            staging_dir = out / PUBLICATIONS_DIR / staging_id
            staging_dir.mkdir(parents=True)
            for key in ARTIFACT_FILES:
                _fault(FAULT_FILE_WRITE)
                atomic_write(staging_dir / ARTIFACT_NAMES[key], initial[key])
            manifest = build_manifest(initial, report_digest)
            atomic_write(
                staging_dir / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           indent=2) + "\n")
            fsync_dir(staging_dir)
            ok, actual, reason = verify_unit(staging_dir, report_digest)
            if not ok:
                raise PublicationFault(
                    f"staged unit failed verification: {reason}")
            assert actual == report_digest

            # --- Step 2: ledger transaction (runs/findings/metrics/publication).
            _fault(FAULT_DB_COMMIT)
            run_pk = ledger.insert_run(connection, report, run_meta,
                                       report_digest)
            ledger.insert_publication_pending(
                connection, run_pk, staging_id, report_digest,
                str(Path(PUBLICATIONS_DIR) / staging_id))
            connection.commit()

            # --- Step 3: finalize identity and atomically promote the unit.
            _fault(FAULT_PROMOTE)
            publication_id = finalize_pending(
                out, connection, staging_dir, run_pk, staging_id, report_digest)
            final_dir = out / PUBLICATIONS_DIR / publication_id

            # --- Step 4: single atomic latest-view switch.
            _fault(FAULT_VIEW_SWITCH)
            current = out / CURRENT_LINK
            relative_symlink(current, final_dir)
            for name in ARTIFACT_NAMES.values():
                link = out / name
                if link.exists() and not link.is_symlink():
                    # Legacy flat file replaced on first protocol publication.
                    link.unlink()
                relative_symlink(link, current / name)
            fsync_dir(out)
            return {"run_pk": run_pk, "publication_id": publication_id,
                    "report_digest": report_digest, "unit_dir": final_dir,
                    "paths": {key: out / name
                              for key, name in ARTIFACT_NAMES.items()}}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
