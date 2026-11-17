"""Fact-level aggregation-path adjudication over sample data.

A declared path fixes one measure, a set of fact refs used as grouping axes, and
a join retention semantic. For every fact row we count how many times that row
contributes after joining all axes, using real dimension-key matches. No SQL is
generated or executed and no input data is rewritten.

Verdicts for a summable measure: ``safe`` (every row contributes exactly once),
``amplified`` (some row contributes more than once, none lost), ``incomplete``
(rows lost, none amplified), ``mixed`` (both), ``measure_blocked`` (the measure
forbids summing along the axes) and ``indeterminate`` (missing columns or a
failed prerequisite). A row is identified by its ordinal, never by the grain
key, which may be non-unique.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contract import Contract
from .contract_paths import axis_candidates
from .measures import measure_gate
from .sources import Dataset
from .util import finding, value_token

SAMPLE_LIMIT = 5
VERDICTS = ("safe", "amplified", "incomplete", "mixed", "measure_blocked", "indeterminate")


def _resolve(fact, path) -> tuple[list, str | None]:
    resolved: list = []
    for axis in path.axes:
        matches = axis_candidates(fact.refs, axis)
        if len(matches) != 1:
            return [], "ambiguous_axis"
        if any(matches[0] is ref for ref in resolved):
            return [], "duplicate_axis_reference"
        resolved.append(matches[0])
    return resolved, None


def _dimension_index(table, key: str) -> dict[str, int]:
    return dict(Counter(value_token(row[key]) for row in table.rows
                        if row.get(key) is not None))


def _row_contribution(row, refs, indexes, retain: str) -> int:
    """Join result count: inner drops on any miss, left keeps one null member."""
    product = 1
    for ref, index in zip(refs, indexes):
        value = row.get(ref.fact_key)
        matches = 0 if value is None else index.get(value_token(value), 0)
        if matches == 0:
            if retain == "inner":
                return 0
            matches = 1
        product *= matches
    return product


def _evidence(row_index, fact, row, contribution) -> dict[str, Any]:
    return {"row_ordinal": row_index, "grain": {column: row.get(column)
                                                for column in fact.grain},
            "contributions": contribution}


def _verdict(participant_count: int, lost_count: int, amplified_count: int) -> str:
    if lost_count and amplified_count:
        return "mixed"
    if amplified_count:
        return "amplified"
    if lost_count or participant_count == 0:
        return "incomplete"
    return "safe"


_FINDING = {
    "amplified": ("DIM-AGG-010", "聚合路径放大了事实行贡献"),
    "mixed": ("DIM-AGG-010", "聚合路径同时放大并丢失事实行"),
    "incomplete": ("DIM-AGG-011", "聚合路径丢失了事实行"),
    "measure_blocked": ("DIM-AGG-012", "度量语义禁止沿该路径直接求和"),
    "indeterminate": ("DIM-AGG-013", "聚合路径无法可靠裁定"),
}


def _findings(object_name: str, verdict: str, reason: str, evidence: dict) -> list:
    if verdict == "safe":
        return []
    code, message = _FINDING[verdict]
    return [finding(code, "aggregation_path", object_name, message,
                    {**evidence, "reason": reason})]


def _prerequisite(contract: Contract, dataset: Dataset, fact, path, resolved: list) -> dict:
    table = dataset.table(fact.name)
    missing = [ref.fact_key for ref in resolved if ref.fact_key not in table.columns]
    missing += [f"{ref.dimension}.{ref.dimension_key}" for ref in resolved
                if ref.dimension_key not in dataset.table(ref.dimension).columns]
    missing += [column for column in fact.grain if column not in table.columns]
    measure = next(item for item in fact.measures if item.name == path.measure)
    gate = measure_gate(fact, measure, table, tuple(ref.dimension for ref in resolved))
    gate["missing_columns"] = sorted(set(missing))
    empty = [name for name in (ref.dimension for ref in resolved)
             if not dataset.table(name).rows]
    if missing and gate["state"] == "analyze":
        gate.update({"state": "indeterminate", "reason": "missing_column"})
    elif gate["state"] == "analyze" and (not table.rows or empty):
        gate.update({"state": "indeterminate", "reason": "empty_source",
                     "empty_tables": empty})
    return gate


def analyze_path(contract: Contract, dataset: Dataset, fact, path) -> tuple[dict[str, Any], list]:
    object_name = f"{fact.name}.{path.name}"
    refs, resolution_error = _resolve(fact, path)
    if resolution_error:
        gate = {"state": "indeterminate", "reason": resolution_error,
                "missing_columns": [], "axes": list(path.axes)}
        refs = []
    else:
        gate = _prerequisite(contract, dataset, fact, path, refs)
    base = {"fact": fact.name, "path": path.name, "measure": path.measure,
            "axes": list(path.axes), "retain": path.retain, "prerequisite": gate,
            "axis_refs": [{"axis": axis, "dimension": ref.dimension, "role": ref.role,
                           "fact_key": ref.fact_key, "dimension_key": ref.dimension_key}
                          for axis, ref in zip(path.axes, refs)]}
    fact_table = dataset.table(fact.name)
    if gate["state"] != "analyze":
        verdict = "measure_blocked" if gate["state"] == "blocked" else "indeterminate"
        base.update({"verdict": verdict, "reason": gate["reason"],
                     "counts": {"fact_rows": len(fact_table.rows),
                                "participating_rows": None, "lost_rows": None,
                                "amplified_rows": None, "total_contributions": None}})
        return base, _findings(object_name, verdict, gate["reason"], _brief(base))
    indexes = [_dimension_index(dataset.table(ref.dimension), ref.dimension_key)
               for ref in refs]
    contributions = [_row_contribution(row, refs, indexes, path.retain)
                     for row in fact_table.rows]
    rows = list(enumerate(zip(fact_table.rows, contributions)))
    participants = [(index, row, count) for index, (row, count) in rows if count]
    lost = [index for index, (_, count) in rows if not count]
    amplified = [(index, row, count) for index, row, count in participants if count > 1]
    verdict = _verdict(len(participants), len(lost), len(amplified))
    base.update({
        "verdict": verdict, "reason": verdict,
        "counts": {"fact_rows": len(fact_table.rows),
                   "participating_rows": len(participants),
                   "lost_rows": len(lost), "amplified_rows": len(amplified),
                   "total_contributions": sum(contributions)},
        "evidence": {
            "lost_rows": [_evidence(i, fact, fact_table.rows[i], 0)
                          for i in sorted(lost)[:SAMPLE_LIMIT]],
            "amplified_rows": [_evidence(i, fact, row, c)
                               for i, row, c in sorted(amplified)[:SAMPLE_LIMIT]],
            "sample_limit": SAMPLE_LIMIT}})
    return base, _findings(object_name, verdict, verdict, _brief(base))


def _brief(entry: dict[str, Any]) -> dict[str, Any]:
    keys = ("fact", "path", "measure", "axes", "retain", "verdict", "counts",
            "evidence", "prerequisite")
    return {key: entry.get(key) for key in keys}


def check_aggregation_paths(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    paths: dict[str, Any] = {}
    counts = {verdict: 0 for verdict in VERDICTS}
    declared = 0
    for fact in contract.facts:
        for path in fact.aggregation_paths:
            declared += 1
            entry, current = analyze_path(contract, dataset, fact, path)
            paths[f"{fact.name}.{path.name}"] = entry
            findings.extend(current)
            counts[entry["verdict"]] += 1
    summary = {"declared": declared, **counts, "unsafe": declared - counts["safe"]}
    return findings, {"paths": paths, "summary": summary}
