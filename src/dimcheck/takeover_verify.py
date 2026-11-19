"""Independent re-verification of a stored takeover plan.

Verification never trusts the stored summary or the rendered report. It reloads
the receipts named by the plan, re-hashes and re-parses the raw CSVs, replays
the transfer intervals and station identity resolution, and compares the
recomputed conclusions with what the ledger claims. Editing only a report or a
stored summary therefore cannot make a wrong takeover pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import Contract
from .takeover import build_plan
from .takeover_store import latest_plan_id, read_plan
from .util import canonical_json, finding


def _independent_receipt_check(ledger: str | Path,
                               stored: dict[str, Any]) -> list[dict]:
    """Re-hash and re-count each recorded receipt file from disk.

    This check is deliberately independent of the plan builder: it uses the
    receipt identification stored with the plan, reads the CSVs again, and
    compares the bytes and row counts to the recorded values. Editing the
    report or the stored summary cannot make a changed delivery look valid.
    """
    from .intake import read_receipt, verify_receipt
    findings = []
    plan_id = stored["plan"]["plan_id"]
    recorded = {(item["side"], item["dimension"]):
                (item["sha256"], int(item["rows"])) for item in stored["files"]}
    for side_row in stored["receipts"]:
        side = side_row["side"]
        try:
            receipt = read_receipt(ledger, side_row["batch_id"])
        except ValueError:
            findings.append(finding("DIM-TO-005", "takeover", side_row["batch_id"],
                                    "收据不存在或不可读", {"side": side}).to_dict())
            continue
        verification = verify_receipt(receipt, side_row["source_root"])
        for item in verification["issues"]:
            findings.append(finding("DIM-TO-005", "takeover", side_row["batch_id"],
                                    item["message"], item["evidence"]).to_dict())
        for item in verification["files"]:
            expected = recorded.get((side, item["dimension"]))
            actual = (item["sha256"], int(item["rows"]))
            if expected is not None and expected != actual:
                findings.append(finding(
                    "DIM-TO-005", "takeover", side_row["batch_id"],
                    "收据文件与计划记录不一致",
                    {"side": side, "dimension": item["dimension"],
                     "expected": list(expected), "actual": list(actual)}).to_dict())
    for (side, dimension), (digest, _) in recorded.items():
        source = next((row for row in stored["receipts"] if row["side"] == side), None)
        if source is None:
            findings.append(finding("DIM-TO-005", "takeover", plan_id,
                                    "计划引用了不存在的收据起点",
                                    {"side": side, "dimension": dimension}).to_dict())
    return findings


def verify_plan(contract: Contract, ledger: str | Path, database: str | Path,
                plan_id: str | None = None) -> dict[str, Any]:
    """Recompute a stored plan from raw inputs and compare conclusions.

    The stored summary is only a claim. Everything is re-derived from the
    receipts, the re-hashed CSV files, and the event byte content. A mismatch is
    returned as a failure and never silently accepts the stored conclusion.
    """
    stored = read_plan(database, plan_id or latest_plan_id(database) or "")
    if stored is None:
        return {"ok": False, "plan_id": plan_id,
                "findings": [finding("DIM-TO-010", "takeover", plan_id or "-",
                                     "未找到可复验的接管计划", {}).to_dict()]}
    record = stored["plan"]
    receipts = []
    for side in ("old", "new"):
        side_row = next((item for item in stored["receipts"] if item["side"] == side),
                        None)
        if side_row is None:
            return {"ok": False, "plan_id": record["plan_id"],
                    "findings": [finding("DIM-TO-010", "takeover", record["plan_id"],
                                         "计划缺少收据引用", {"side": side}).to_dict()]}
        receipts.append({"batch_id": side_row["batch_id"],
                         "source_root": side_row["source_root"]})
    fresh = build_plan(contract, str(ledger), receipts, record["events_path"],
                       record["policy"], int(record["generated_epoch"]))
    findings = []
    findings.extend(_independent_receipt_check(ledger, stored))
    if fresh["fingerprint"] != record["fingerprint"]:
        findings.append(finding(
            "DIM-TO-010", "takeover", record["plan_id"],
            "复验结果与已记录计划不一致",
            {"stored_fingerprint": record["fingerprint"],
             "recomputed_fingerprint": fresh["fingerprint"]}).to_dict())
    stored_conclusions = [(canonical_json(item["service_date_key"]),
                           item["conclusion"], item["reason"])
                          for item in stored["partitions"]]
    fresh_conclusions = [(canonical_json(item["service_date_key"]),
                          item["conclusion"], item["reason"])
                         for item in fresh["partitions"]]
    if stored_conclusions != fresh_conclusions:
        findings.append(finding(
            "DIM-TO-010", "takeover", record["plan_id"],
            "分区采用/待核结论与复验不一致",
            {"stored": stored_conclusions,
             "recomputed": fresh_conclusions}).to_dict())
    if stored["summary"] != fresh["summary"]:
        findings.append(finding(
            "DIM-TO-010", "takeover", record["plan_id"],
            "台账摘要与复验重算不一致", {}).to_dict())
    warnings = []
    if fresh["state"] != "confirmed":
        warnings.append(finding("DIM-TO-011", "takeover", record["plan_id"],
                                "接管计划仍处于待核状态", {}).to_dict())
    return {"ok": not findings, "plan_id": record["plan_id"],
            "stored_state": record["state"], "recomputed_state": fresh["state"],
            "findings": findings + warnings, "recomputed": fresh}
