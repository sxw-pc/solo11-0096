"""Referential integrity and snowflake dimension validation."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contract import Contract, Dimension, Snowflake
from .sources import Dataset, Table
from .util import finding, value_token


def unique_index(table: Table, column: str) -> dict[str, list[Any]]:
    """Group non-null values of ``column`` by canonical token.

    Returns every group, including duplicates, so callers can distinguish a
    unique identity from a duplicated one instead of silently taking the first
    row. Null keys are excluded and must be handled explicitly by the caller.
    """
    groups: dict[str, list[Any]] = {}
    for row in table.rows:
        value = row.get(column)
        if value is None:
            continue
        groups.setdefault(value_token(value), []).append(value)
    return groups


def snowflake_line_code(stations: Table | None, lines: Table | None,
                        station_key: Any) -> str | None:
    """Resolve a ridership station key to a line code through the snowflake.

    This is the read-only identity path used by takeover verification:
    ``fct_ridership.station_key`` -> ``dim_station.station_key`` ->
    ``dim_station.line_key`` -> ``dim_line.line_code``. Any missing or
    duplicated hop returns ``None`` so the caller keeps the row pending.
    """
    station = unique_row(row_index(stations, "station_key"), station_key)
    if station is None:
        return None
    return lookup_line_code(lines, row_index(lines, "line_key"),
                            station.get("line_key"))


def station_code_for_key(stations: Table | None, station_key: Any) -> str | None:
    """Return the unique ``station_code`` for one surrogate key, else ``None``."""
    station = unique_row(row_index(stations, "station_key"), station_key)
    return station.get("station_code") if station else None


def row_index(table: Table | None, column: str) -> dict[str, list[dict[str, Any]]]:
    """Group whole rows by the canonical token of one non-null column.

    Returning rows (not bare values) lets takeover planning read several
    columns of the uniquely matched record and detect duplicates without ever
    falling back to the first match.
    """
    if table is None or column not in table.columns:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for row in table.rows:
        value = row.get(column)
        if value is None:
            continue
        result.setdefault(value_token(value), []).append(row)
    return result


def unique_row(index: dict[str, list[dict[str, Any]]], value: Any) -> dict | None:
    """Return the only row for ``value``, or ``None`` when absent/duplicated."""
    if value is None:
        return None
    values = index.get(value_token(value), [])
    return values[0] if len(values) == 1 else None


def lookup_line_code(line_table: Table | None, index: dict[str, list[dict[str, Any]]],
                     line_key: Any) -> str | None:
    """Resolve ``dim_station.line_key`` to its ``dim_line.line_code``.

    ``None`` means the snowflake key is missing or ambiguous; callers must keep
    the record pending instead of guessing a line.
    """
    row = unique_row(index, line_key)
    return row.get("line_code") if row else None


def single_lookup(table: Table, column: str) -> dict[str, Any]:
    """Map tokens to values only when the column is unique and non-null.

    A token whose value occurs more than once is intentionally absent, so a
    caller cannot accidentally attribute an ambiguous natural key.
    """
    groups = unique_index(table, column)
    return {token: values[0] for token, values in groups.items() if len(values) == 1}


def snowflake_relation(contract: Contract, dimension_name: str) -> Snowflake | None:
    dimension = contract.dimensions_by_name().get(dimension_name)
    return dimension.snowflake if dimension else None


def dimension_by_name(contract: Contract, name: str) -> Dimension | None:
    return contract.dimensions_by_name().get(name)


def _sample(values: list[Any], limit: int = 5) -> list[Any]:
    unique = {value_token(value): value for value in values}
    return [unique[token] for token in sorted(unique)[:limit]]


def _reference_check(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    dimension_map = contract.dimensions_by_name()
    for fact in contract.facts:
        fact_table = dataset.table(fact.name)
        for ref in fact.refs:
            dimension = dimension_map[ref.dimension]
            dim_table = dataset.table(dimension.name)
            key = f"{fact.name}.{ref.fact_key}->{dimension.name}.{ref.dimension_key}"
            item: dict[str, Any] = {
                "fact": fact.name, "dimension": dimension.name, "role": ref.role,
                "fact_key": ref.fact_key, "dimension_key": ref.dimension_key,
            }
            missing = [
                ("fact", ref.fact_key) if ref.fact_key not in fact_table.columns else None,
                ("dimension", ref.dimension_key) if ref.dimension_key not in dim_table.columns else None,
            ]
            missing = [entry for entry in missing if entry]
            if missing:
                findings.append(finding("DIM-REF-001", "reference", key, "引用列不存在",
                                        {"missing": [{"side": side, "column": column}
                                                     for side, column in missing]}))
                item["missing_columns"] = [column for _, column in missing]
                metrics[key] = item
                continue
            fact_values = [row.get(ref.fact_key) for row in fact_table.rows]
            known = {value_token(row.get(ref.dimension_key)) for row in dim_table.rows}
            whitelist = {value_token(value) for value in ref.unknown_members}
            accepted = known | whitelist
            null_rows = sum(1 for value in fact_values if value is None)
            unknown_values = [
                value for value in fact_values
                if value is not None and value_token(value) not in accepted
            ]
            whitelisted = sorted({
                value_token(value) for value in fact_values
                if value is not None and value_token(value) in whitelist
                and value_token(value) not in known
            })
            if null_rows and not ref.optional:
                findings.append(finding("DIM-REF-003", "reference", key,
                                        "必填外键存在空值", {"null_rows": null_rows}))
            if unknown_values:
                severity = "warning" if ref.optional else "error"
                findings.append(finding(
                    "DIM-REF-002", "reference", key, "事实键在维表中不存在",
                    {"unknown_count": len(unknown_values),
                     "unknown_keys": _sample(unknown_values),
                     "whitelisted_keys": len(whitelisted), "optional": ref.optional},
                    severity=severity,
                ))
            item.update({
                "null_rows": null_rows, "unknown_rows": len(unknown_values),
                "unknown_keys": len({value_token(value) for value in unknown_values}),
                "whitelisted_keys": len(whitelisted), "matched_rows": len(fact_values)
                - null_rows - len(unknown_values),
            })
            metrics[key] = item
    return findings, {"references": metrics}


def _cycle_findings(contract: Contract) -> tuple[list, list[list[str]]]:
    edges = {
        item.name: item.snowflake.parent
        for item in contract.dimensions if item.snowflake
    }
    cycles: set[tuple[str, ...]] = set()
    for start in sorted(edges):
        path: list[str] = []
        current: str | None = start
        while current is not None and current not in path:
            path.append(current)
            current = edges.get(current)
        if current is not None and current in path:
            body = path[path.index(current):]
            rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
            cycles.add(min(rotations))
    ordered = [list(cycle) for cycle in sorted(cycles)]
    findings = [
        finding("DIM-REF-005", "snowflake", " -> ".join(cycle + [cycle[0]]),
                "雪花维父引用存在环", {"cycle": cycle})
        for cycle in ordered
    ]
    return findings, ordered


def _snowflake_checks(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    dimensions = contract.dimensions_by_name()
    for dimension in contract.dimensions:
        if not dimension.snowflake:
            continue
        snow = dimension.snowflake
        name = f"{dimension.name} -> {snow.parent}"
        parent = dimensions.get(snow.parent)
        item: dict[str, Any] = {
            "parent": snow.parent, "child_key": snow.child_key,
            "parent_key": snow.parent_key,
        }
        if parent is None:
            findings.append(finding("DIM-REF-004", "snowflake", name, "父维度不存在"))
            metrics[name] = item
            continue
        child_table = dataset.table(dimension.name)
        parent_table = dataset.table(parent.name)
        missing = [
            ("child", snow.child_key) if snow.child_key not in child_table.columns else None,
            ("parent", snow.parent_key) if snow.parent_key not in parent_table.columns else None,
        ]
        missing = [entry for entry in missing if entry]
        if missing:
            findings.append(finding("DIM-REF-004", "snowflake", name, "雪花维列不存在",
                                    {"missing": [{"side": side, "column": column}
                                                 for side, column in missing]}))
            metrics[name] = item
            continue
        parent_values = [row.get(snow.parent_key) for row in parent_table.rows]
        parent_counts = Counter(value_token(value) for value in parent_values
                                if value is not None)
        parent_set = set(parent_counts)
        child_values = [row.get(snow.child_key) for row in child_table.rows]
        null_rows = sum(1 for value in child_values if value is None)
        orphan_values = [value for value in child_values
                         if value is not None and value_token(value) not in parent_set]
        duplicate_parent = {
            token: count for token, count in parent_counts.items() if count > 1
        }
        if null_rows:
            findings.append(finding("DIM-REF-004", "snowflake", name, "雪花维子键为空",
                                    {"null_rows": null_rows}))
        if orphan_values:
            findings.append(finding("DIM-REF-004", "snowflake", name, "雪花维存在孤儿引用",
                                    {"orphan_rows": len(orphan_values),
                                     "orphan_keys": _sample(orphan_values)}))
        if duplicate_parent:
            findings.append(finding("DIM-REF-004", "snowflake", name,
                                    "父维连接键不唯一", {"duplicate_keys": len(duplicate_parent)}))
        item.update({"null_rows": null_rows, "orphan_rows": len(orphan_values),
                     "orphan_keys": len({value_token(value) for value in orphan_values}),
                     "duplicate_parent_keys": len(duplicate_parent)})
        metrics[name] = item
    cycle_findings, cycles = _cycle_findings(contract)
    findings.extend(cycle_findings)
    return findings, {"snowflake": metrics, "cycles": cycles}


def check_refs(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings, ref_metrics = _reference_check(contract, dataset)
    snow_findings, snow_metrics = _snowflake_checks(contract, dataset)
    findings.extend(snow_findings)
    return findings, {**ref_metrics, **snow_metrics}
