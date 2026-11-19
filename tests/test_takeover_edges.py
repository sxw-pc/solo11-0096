"""Boundary cases for takeover: path overrides, wrong prior lines, unknown keys."""
from __future__ import annotations

import json

import pytest

from takeover_support import (BATCHES, CONTRACT, TAKEOVER, plan,
                              register_batches)

OLD = BATCHES["old"][0]


def test_sources_override_loading_stays_under_root(project):
    from dimcheck.contract import load_contract
    from dimcheck.sources import load_dataset
    contract = load_contract(project.contract)
    replacement = project.data / "dim_customer.csv"
    replacement.write_text("customer_key,customer_id,segment\n1,C001,retail\n",
                           encoding="utf-8")
    dataset = load_dataset(contract, {"dim_customer": replacement})
    assert len(dataset.table("dim_customer").rows) == 1
    with pytest.raises(Exception):
        load_dataset(contract, {"dim_customer": "../outside.csv"})


def test_event_with_wrong_prior_line_is_pending(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "new"))
    events = tmp_path / "events.json"
    events.write_text(json.dumps({"events": [{
        "station_code": "S-A01", "from_line_code": "L-03",
        "to_line_code": "L-02", "effective_at": "2026-01-03T00:00:00Z"}]}),
        encoding="utf-8")
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover",
                  events=events)
    # The chain says S-A01 used to be L-03 but the old batch says L-01, so
    # neither the pre-transfer day nor the transfer day can be verified.
    assert result["state"] == "pending"
    reasons = {row["reason"] for part in result["partitions"]
               for row in part["pending_evidence"]}
    assert "old_line_mismatch" in reasons
    assert all(part["conclusion"] == "retained_old"
               for part in result["partitions"] if part["pending_rows"])


def test_unknown_station_key_keeps_row_evidence(ledger, tmp_path):
    register_batches(ledger.parent, ledger, ("old", "new"))
    data = tmp_path / "data"
    (data / "old").mkdir(parents=True)
    text = (TAKEOVER / "fct_ridership.csv").read_text(encoding="utf-8")
    (data / "fct_ridership.csv").write_text(
        text.replace("R00007,3,5", "R00007,3,42"), encoding="utf-8")
    (data / "dim_date.csv").write_bytes((TAKEOVER / "dim_date.csv").read_bytes())
    for name in ("dim_line", "dim_station"):
        (data / "old" / f"{name}.csv").write_bytes(
            (TAKEOVER / "old" / f"{name}.csv").read_bytes())
    contract_path = tmp_path / "model.yaml"
    contract_path.write_text(
        CONTRACT.read_text(encoding="utf-8").replace("root: .", f"root: {data}"),
        encoding="utf-8")
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover",
                  contract=contract_path)
    row = next(item for part in result["partitions"]
               for item in part["pending_evidence"] if item["event_id"] == "R00007")
    assert row["reason"] == "unknown_old_station_key"
    assert row["new_station_code"] is None


def test_plan_is_deterministic_and_replay_stable(ledger):
    from dimcheck.takeover_report import render_html, render_markdown
    register_batches(ledger.parent, ledger, ("old", "new"))
    first = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    second = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    assert first == second
    assert render_markdown(first) == render_markdown(second)
    assert render_html(first) == render_html(second)


def test_report_tamper_without_ledger_change_is_caught(ledger, tmp_path):
    from dimcheck.contract import load_contract
    from dimcheck.takeover_report import write_plan_reports
    from dimcheck.takeover_store import save_plan, verify_plan
    register_batches(ledger.parent, ledger, ("old", "conflict"))
    result = plan(ledger, OLD, BATCHES["conflict"][0], "partitioned_cutover")
    database = tmp_path / "takeover.sqlite3"
    save_plan(database, result)
    out = tmp_path / "out"
    paths = write_plan_reports(out, result)
    # Forge a report that claims the whole plan was adopted on the new batch.
    import json as _json
    payload = _json.loads(paths["json"].read_text(encoding="utf-8"))
    payload["partitions"] = [
        {**item, "conclusion": "adopted_new", "reason": "all_rows_verified"}
        for item in payload["partitions"]]
    payload["summary"] = {**payload["summary"], "adopted_new_rows": payload["summary"]["rows"]}
    paths["json"].write_text(_json.dumps(payload), encoding="utf-8")
    contract = load_contract(CONTRACT)
    verification = verify_plan(contract, ledger, database, result["plan_id"])
    assert verification["ok"] is True  # ledger is the source of truth
    forged = _json.loads(paths["json"].read_text(encoding="utf-8"))
    assert forged["summary"]["adopted_new_rows"] == verification["recomputed"]["summary"]["rows"]
    # The forged summary does not match the recomputed conclusions.
    assert forged["partitions"] != verification["recomputed"]["partitions"]


def test_unknown_old_line_holds_row_pending(ledger, tmp_path):
    """An orphan dim_station.line_key in a receipt must not silently verify."""
    from dimcheck.contract import load_contract
    from dimcheck.intake import register
    from dimcheck.takeover import build_plan
    data = tmp_path / "data"
    (data / "old").mkdir(parents=True)
    (data / "new").mkdir()
    for folder in ("old", "new"):
        for name in ("dim_line", "dim_station"):
            (data / folder / f"{name}.csv").write_bytes(
                (TAKEOVER / ("old" if folder == "old" else "new-clean")
                 / f"{name}.csv").read_bytes())
    station = data / "old" / "dim_station.csv"
    station.write_text(station.read_text(encoding="utf-8").replace(
        "1,S-A01,示例甲一,1", "1,S-A01,示例甲一,9"), encoding="utf-8")
    for name in ("dim_date.csv", "fct_ridership.csv"):
        (data / name).write_bytes((TAKEOVER / name).read_bytes())
    for batch, folder in (("orphan-old", "old"), ("orphan-new", "new")):
        files = [{"name": name, "source": f"{folder}/{name}.csv",
                  "sha256": __import__("hashlib").sha256(
                      (data / folder / f"{name}.csv").read_bytes()).hexdigest()}
                 for name in ("dim_line", "dim_station")]
        path = tmp_path / f"{batch}.json"
        path.write_text(json.dumps({"batch_id": batch, "producer": "p",
                                    "dimensions": files}), encoding="utf-8")
        register(path, data, ledger)
    contract_path = tmp_path / "model.yaml"
    contract_path.write_text(
        CONTRACT.read_text(encoding="utf-8").replace("root: .", f"root: {data}"),
        encoding="utf-8")
    result = build_plan(load_contract(contract_path), str(ledger),
                        [{"batch_id": "orphan-old", "source_root": str(data)},
                         {"batch_id": "orphan-new", "source_root": str(data)}],
                        str(TAKEOVER / "transfers.json"), "partitioned_cutover",
                        1767225600)
    reasons = {row["reason"] for part in result["partitions"]
               for row in part["pending_evidence"]}
    assert "line_unresolved" in reasons
    assert result["state"] == "pending"
    assert result["summary"]["adopted_new_rows"] == 0


def test_shared_old_line_without_events_stays_verified(ledger, tmp_path):
    """Stations outside the event list keep the same line and verify normally."""
    register_batches(ledger.parent, ledger, ("old", "new"))
    result = plan(ledger, OLD, BATCHES["new"][0], "partitioned_cutover")
    unaffected = [row["event_id"] for part in result["partitions"]
                  for row in part["pending_evidence"]
                  if row["old_station_code"] in {"S-A02", "S-B01"}]
    assert unaffected == []
    assert result["summary"]["pending_rows"] == 0


def test_duplicate_station_code_in_new_batch_is_reported_not_global(ledger):
    """A duplicated new code is a finding, but only the touched rows go pending."""
    register_batches(ledger.parent, ledger, ("old", "conflict"))
    result = plan(ledger, OLD, BATCHES["conflict"][0], "partitioned_cutover")
    finding_codes = {item["code"] for item in result["findings"]}
    assert "DIM-TO-006" in finding_codes
    assert result["blocked"] is False
    assert result["summary"]["adopted_new_rows"] == 4


def test_verify_uses_refs_identity_helper(ledger):
    """The shared refs helper must resolve identity the same way planning does."""
    from dimcheck.contract import load_contract
    from dimcheck.refs import snowflake_line_code, station_code_for_key
    from dimcheck.sources import load_dataset
    contract = load_contract(CONTRACT)
    dataset = load_dataset(contract)
    stations = dataset.table("dim_station")
    lines = dataset.table("dim_line")
    assert station_code_for_key(stations, 1) == "S-A01"
    assert snowflake_line_code(stations, lines, 1) == "L-01"
    assert station_code_for_key(stations, 99) is None
    assert snowflake_line_code(stations, lines, 99) is None
