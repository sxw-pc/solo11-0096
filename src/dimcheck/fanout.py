"""Join fan-out estimation and measurement per fact/dimension reference."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contract import Contract
from .sources import Dataset
from .util import finding, value_token


def _risk(dimension_duplicate_keys: int, referenced_duplicates: int,
          measured: float | None) -> str:
    if dimension_duplicate_keys == 0:
        return "none"
    if referenced_duplicates == 0:
        return "low"
    if measured is not None and measured >= 2.0:
        return "high"
    return "medium" if measured is not None and measured > 1.0 else "low"


def _join_metric(fact, dimension, ref, fact_table, dim_table) -> tuple[dict[str, Any], dict | None]:
    if ref.fact_key not in fact_table.columns or ref.dimension_key not in dim_table.columns:
        return {"status": "skipped", "reason": "missing_column"}, None
    dim_counts: Counter[str] = Counter()
    dim_examples: dict[str, Any] = {}
    for row in dim_table.rows:
        value = row.get(ref.dimension_key)
        if value is None:
            continue
        token = value_token(value)
        dim_counts[token] += 1
        dim_examples.setdefault(token, value)
    dim_rows = sum(dim_counts.values())
    distinct_keys = len(dim_counts)
    duplicate_keys = sum(1 for count in dim_counts.values() if count > 1)
    duplicate_rows = sum(count for count in dim_counts.values() if count > 1)
    fact_counts: Counter[str] = Counter()
    for row in fact_table.rows:
        value = row.get(ref.fact_key)
        if value is not None:
            fact_counts[value_token(value)] += 1
    matched_rows = sum(count for token, count in fact_counts.items() if token in dim_counts)
    joined_rows = sum(count * dim_counts[token] for token, count in fact_counts.items()
                      if token in dim_counts)
    measured = round(joined_rows / matched_rows, 6) if matched_rows else None
    estimated = round(dim_rows / distinct_keys, 6) if distinct_keys else None
    referenced = [token for token in fact_counts if dim_counts.get(token, 0) > 1]
    referenced_duplicate_keys = len(referenced)
    sample = [
        dim_examples[token] for token in sorted(referenced, key=value_token)[:5]
    ]
    risk = _risk(duplicate_keys, referenced_duplicate_keys, measured)
    metric = {
        "fact": fact.name, "dimension": dimension.name, "role": ref.role,
        "fact_rows": len(fact_table.rows), "matched_fact_rows": matched_rows,
        "unmatched_fact_rows": len(fact_table.rows) - matched_rows - (
            len(fact_table.rows) - sum(fact_counts.values())),
        "dimension_rows": dim_rows, "dimension_distinct_keys": distinct_keys,
        "duplicate_dimension_keys": duplicate_keys,
        "duplicate_dimension_rows": duplicate_rows,
        "referenced_duplicate_keys": referenced_duplicate_keys,
        "estimated_joined_rows": round(matched_rows * estimated, 6) if estimated else None,
        "measured_joined_rows": joined_rows,
        "estimated_amplification": estimated,
        "measured_amplification": measured,
        "extra_rows": joined_rows - matched_rows,
        "risk": risk,
    }
    evidence = {
        "fact": fact.name, "dimension": dimension.name, "role": ref.role,
        "risk": risk, "measured_amplification": measured,
        "estimated_amplification": estimated,
        "measured_joined_rows": joined_rows, "matched_fact_rows": matched_rows,
        "extra_rows": joined_rows - matched_rows,
        "duplicate_dimension_keys": duplicate_keys,
        "referenced_duplicate_keys": referenced_duplicate_keys,
        "sample_duplicate_keys": sample,
    }
    return metric, evidence


def check_fanout(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    dimensions = contract.dimensions_by_name()
    max_amplification = 1.0
    risky = 0
    for fact in contract.facts:
        fact_table = dataset.table(fact.name)
        for ref in fact.refs:
            dimension = dimensions[ref.dimension]
            dim_table = dataset.table(dimension.name)
            key = f"{fact.name}.{ref.fact_key}->{dimension.name}.{ref.dimension_key}"
            metric, evidence = _join_metric(fact, dimension, ref, fact_table, dim_table)
            metric["status"] = metric.get("status", "checked")
            metrics[key] = metric
            if metric.get("status") == "skipped":
                continue
            measured = metric["measured_amplification"]
            if measured is not None:
                max_amplification = max(max_amplification, measured)
            if metric["risk"] in {"medium", "high"}:
                risky += 1
                findings.append(finding(
                    "DIM-FAN-001", "join", key,
                    "维表键重复导致连接行数放大",
                    evidence,
                ))
    return findings, {
        "joins": metrics,
        "max_measured_amplification": round(max_amplification, 6),
        "risky_join_count": risky,
    }
