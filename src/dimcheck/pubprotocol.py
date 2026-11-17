"""Shared primitives of the immutable publication protocol.

This module owns the on-disk layout constants, content digesting, byte-level
unit verification, fault-injection hooks, and the directory-wide inter-process
lock.  It deliberately contains no ledger or report-rendering logic, so the
lower layers stay importable in isolation.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PUBLICATIONS_DIR = "publications"
QUARANTINE_DIR = "quarantine"
CURRENT_LINK = "current"
LOCK_NAME = ".dimcheck.lock"
LEGACY_UNIT = "pub-legacy-000000"
MIGRATION_MARK = ".dimcheck-migrated"

ARTIFACT_FILES = ("json", "markdown", "html", "run")
ARTIFACT_NAMES = {"json": "report.json", "markdown": "report.md",
                  "html": "report.html", "run": "run.json"}

# Publication lifecycle states stored on ``publications.status``.
STATUS_PENDING = "pending"            # ledger row inserted, unit not finalized
STATUS_COMMITTED = "committed"        # unit finalized and digest-verified
STATUS_ISOLATED = "isolated"          # verified once, later found corrupted
STATUS_LEGACY = "legacy_unverified"   # pre-protocol row, never verified
PUBLISH_STATES = (STATUS_PENDING, STATUS_COMMITTED, STATUS_ISOLATED,
                  STATUS_LEGACY)
# The deterministic report bodies define the unit content digest.  run.json is
# deliberately excluded: its timing fields are an explicit determinism waiver.
DIGEST_FILES = ("json", "markdown", "html")

# Fault-injection windows.  Set DIMCHECK_FAULT to one of these values to raise
# PublicationFault at the corresponding step, simulating a process crash before
# that step completes.  DIMCHECK_FAULT_COUNT (default 1) bounds how many times
# the fault fires, which keeps recovery loops deterministic.
FAULT_FILE_WRITE = "file_write"
FAULT_DB_COMMIT = "db_commit"
FAULT_PROMOTE = "promote"
FAULT_VIEW_SWITCH = "view_switch"
FAULT_WINDOWS = (FAULT_FILE_WRITE, FAULT_DB_COMMIT, FAULT_PROMOTE,
                 FAULT_VIEW_SWITCH)


class PublicationFault(RuntimeError):
    """Injected failure standing in for a process crash at a protocol step."""


def _fault(window: str) -> None:
    if os.environ.get("DIMCHECK_FAULT") != window:
        return
    remaining = int(os.environ.get("DIMCHECK_FAULT_COUNT", "1"))
    if remaining <= 0:
        return
    os.environ["DIMCHECK_FAULT_COUNT"] = str(remaining - 1)
    raise PublicationFault(f"injected fault at {window}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def report_digest_of(unit_dir: Path) -> str:
    """Content digest of the three deterministic report bodies on disk."""
    digest = hashlib.sha256()
    for key in DIGEST_FILES:
        digest.update(sha256_file(unit_dir / ARTIFACT_NAMES[key]).encode("ascii"))
    return digest.hexdigest()


def build_manifest(payloads: dict[str, str], report_digest: str) -> dict[str, Any]:
    files = {ARTIFACT_NAMES[key]: hashlib.sha256(
        payloads[key].encode("utf-8")).hexdigest() for key in ARTIFACT_FILES}
    return {"schema_version": 1, "report_digest": report_digest, "files": files}


def fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and os.replace so readers never see a partial."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_unit(unit_dir: Path, expected_digest: str | None = None
                ) -> tuple[bool, str | None, str | None]:
    """Re-hash every artifact against the manifest stored inside the unit.

    Verification always reads actual bytes — never a status field.  Returns
    ``(ok, actual_report_digest, reason)``.
    """
    if not unit_dir.is_dir():
        return False, None, f"unit_missing:{unit_dir.name}"
    for name in ARTIFACT_NAMES.values():
        if not (unit_dir / name).is_file():
            return False, None, f"artifact_missing:{name}"
    manifest = read_json(unit_dir / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        return False, None, "manifest_unreadable"
    for name, want in manifest["files"].items():
        target = unit_dir / name
        if not target.is_file() or sha256_file(target) != want:
            return False, None, f"digest_mismatch:{name}"
    actual = report_digest_of(unit_dir)
    if manifest.get("report_digest") != actual:
        return False, actual, "manifest_report_digest_mismatch"
    if expected_digest is not None and actual != expected_digest:
        return False, actual, "report_digest_mismatch"
    return True, actual, None


def run_metadata_matches(unit_dir: Path) -> bool:
    """Cross-check run.json metadata against the report body for back-fill."""
    report = read_json(unit_dir / "report.json")
    run_meta = read_json(unit_dir / "run.json")
    if not isinstance(report, dict) or not isinstance(run_meta, dict):
        return False
    if any(run_meta.get(key) != report.get(key)
           for key in ("run_id", "exit_code", "verdict")):
        return False
    if run_meta.get("contract_fingerprint") != \
            report.get("contract", {}).get("fingerprint"):
        return False
    if not isinstance(run_meta.get("started_at"), str) or \
            not isinstance(run_meta.get("duration_ms"), int):
        return False
    return True


_process_lock = threading.RLock()
_lock_depth = threading.local()
_FALLBACK_LOCK_DIR = ".dimcheck.lock.d"


@contextmanager
def out_lock(out_dir: str | Path) -> Iterator[None]:
    """Directory-wide exclusive lock serializing all publishing processes.

    ``flock`` serializes separate processes.  Within one process the same lock
    is re-entrant via a thread-local depth counter (a second flock on a new
    descriptor would self-deadlock); an RLock additionally serializes threads.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / LOCK_NAME
    if not lock_path.exists():
        try:
            lock_path.touch(exist_ok=True)
        except OSError:
            pass
    depths = getattr(_lock_depth, "depths", None)
    if depths is None:
        depths = {}
        _lock_depth.depths = depths
    key = str(lock_path)
    depths[key] = depths.get(key, 0) + 1
    nested = depths[key] > 1
    handle = None
    fallback = False
    _process_lock.acquire()
    try:
        if not nested:
            try:
                import fcntl
                handle = lock_path.open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):  # pragma: no cover - non-POSIX
                fallback = True
                _acquire_fallback_lock(lock_path)
        yield
    finally:
        if not nested:
            if fallback:  # pragma: no cover
                _release_fallback_lock(lock_path)
            if handle is not None:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass
                handle.close()
        _process_lock.release()
        depths[key] -= 1
        if depths[key] <= 0:
            del depths[key]


def _acquire_fallback_lock(lock_path: Path) -> None:  # pragma: no cover
    import time
    directory = lock_path.parent / _FALLBACK_LOCK_DIR
    deadline = time.monotonic() + 60
    while True:
        try:
            directory.mkdir()
            return
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"无法获取输出目录锁: {lock_path.parent}")
            time.sleep(0.05)


def _release_fallback_lock(lock_path: Path) -> None:  # pragma: no cover
    directory = lock_path.parent / _FALLBACK_LOCK_DIR
    try:
        directory.rmdir()
    except OSError:
        pass
