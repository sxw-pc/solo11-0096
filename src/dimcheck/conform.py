"""Conformed-dimension, role-playing, and bus-matrix checks."""
from __future__ import annotations

from typing import Any

from .contract import Contract
from .sources import Dataset
from .util import finding, value_token


def _domain(table, column: str) -> tuple[set[str], dict[str, Any]]:
    values: dict[str, Any] = {}
    for row in table.rows:
        value = row.get(column)
        if value is not None:
            values.setdefault(value_token(value), value)
    return set(values), values


def _conformed_check(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    groups: dict[str, list] = {}
    for dimension in contract.dimensions:
        if dimension.conformed_group:
            groups.setdefault(dimension.conformed_group, []).append(dimension)
    metrics: dict[str, Any] = {}
    for group in sorted(groups):
        members = sorted(groups[group], key=lambda item: item.name)
        baseline = members[0]
        baseline_attrs = {attr.name: attr.type for attr in baseline.attributes}
        baseline_table = dataset.table(baseline.name)
        metrics[group] = {"dimensions": [item.name for item in members], "issues": 0}
        for other in members[1:]:
            other_attrs = {attr.name: attr.type for attr in other.attributes}
            missing = sorted(set(baseline_attrs) - set(other_attrs))
            extra = sorted(set(other_attrs) - set(baseline_attrs))
            if missing or extra:
                metrics[group]["issues"] += 1
                findings.append(finding(
                    "DIM-CON-001", "conformed_group", f"{group}:{other.name}",
                    "一致性维度属性集不一致",
                    {"baseline": baseline.name, "dimension": other.name,
                     "missing": missing, "extra": extra},
                ))
            other_table = dataset.table(other.name)
            for attr in sorted(set(baseline_attrs) & set(other_attrs)):
                if baseline_attrs[attr] != other_attrs[attr]:
                    metrics[group]["issues"] += 1
                    findings.append(finding(
                        "DIM-CON-002", "conformed_group", f"{group}:{other.name}.{attr}",
                        "一致性维度属性类型不一致",
                        {"baseline": baseline_attrs[attr], "actual": other_attrs[attr]},
                    ))
                if attr not in baseline_table.columns or attr not in other_table.columns:
                    continue
                left_set, left_values = _domain(baseline_table, attr)
                right_set, right_values = _domain(other_table, attr)
                if left_set != right_set:
                    metrics[group]["issues"] += 1
                    left_only = sorted(left_set - right_set)[:3]
                    right_only = sorted(right_set - left_set)[:3]
                    findings.append(finding(
                        "DIM-CON-003", "conformed_group", f"{group}:{other.name}.{attr}",
                        "一致性维度取值域不一致",
                        {"baseline": baseline.name, "attribute": attr,
                         "baseline_distinct": len(left_set),
                         "actual_distinct": len(right_set),
                         "only_in_baseline": [left_values[token] for token in left_only],
                         "only_in_actual": [right_values[token] for token in right_only]},
                    ))
    return findings, {"conformed_groups": metrics}


def _role_check(contract: Contract) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    dimension_facts: dict[str, dict[str, list[str | None]]] = {}
    for fact in contract.facts:
        by_dimension: dict[str, list] = {}
        for ref in fact.refs:
            by_dimension.setdefault(ref.dimension, []).append(ref)
            dimension_facts.setdefault(ref.dimension, {}).setdefault(fact.name, []).append(ref.role)
        seen_roles: dict[str, list[str]] = {}
        for dimension, refs in sorted(by_dimension.items()):
            roles = [ref.role for ref in refs]
            if len(refs) > 1:
                missing = sum(1 for role in roles if not role)
                if missing:
                    findings.append(finding(
                        "DIM-CON-004", "role", f"{fact.name}.{dimension}",
                        "同一事实重复引用维度但未声明角色",
                        {"references": len(refs), "missing_roles": missing},
                    ))
                present = [role for role in roles if role]
                duplicate = sorted({role for role in present if present.count(role) > 1})
                if duplicate:
                    findings.append(finding(
                        "DIM-CON-005", "role", f"{fact.name}.{dimension}",
                        "同一事实内角色重复", {"roles": duplicate},
                    ))
            for role in roles:
                if role:
                    seen_roles.setdefault(role, []).append(dimension)
        collisions = {
            role: sorted(set(dimensions))
            for role, dimensions in seen_roles.items()
            if len(set(dimensions)) > 1
        }
        for role in sorted(collisions):
            findings.append(finding(
                "DIM-CON-007", "role", f"{fact.name}.{role}",
                "同一角色名指向多个维度", {"dimensions": collisions[role]},
            ))
    for dimension, facts in sorted(dimension_facts.items()):
        states = {}
        for fact_name, roles in facts.items():
            states[fact_name] = "declared" if all(roles) else (
                "partial" if any(roles) else "absent"
            )
        metrics[dimension] = {"facts": states}
        if "declared" in states.values() and (
            "absent" in states.values() or "partial" in states.values()
        ):
            missing = sorted(name for name, state in states.items() if state != "declared")
            findings.append(finding(
                "DIM-CON-006", "role", dimension,
                "同一维度的角色声明跨事实不一致",
                {"facts_without_roles": missing},
            ))
    return findings, {"roles": metrics}


def _bus_matrix(contract: Contract) -> tuple[list, dict]:
    findings = []
    dimensions = contract.dimensions_by_name()
    matrix: dict[str, Any] = {}
    used: set[str] = set()
    for fact in contract.facts:
        direct = sorted({ref.dimension for ref in fact.refs})
        roles = sorted({ref.role for ref in fact.refs if ref.role})
        matrix[fact.name] = {"dimensions": direct, "roles": roles}
        if not direct:
            findings.append(finding("DIM-CON-008", "fact", fact.name,
                                    "事实表没有维度引用"))
        used.update(direct)
    pending = sorted(used)
    while pending:
        name = pending.pop(0)
        parent = dimensions[name].snowflake
        if parent and parent.parent not in used:
            used.add(parent.parent)
            pending.append(parent.parent)
    unused = sorted(set(dimensions) - used)
    for name in unused:
        findings.append(finding("DIM-CON-009", "dimension", name,
                                "维度未被事实表或雪花链使用"))
    total = len(dimensions)
    return findings, {
        "bus_matrix": matrix,
        "used_dimensions": sorted(used),
        "unused_dimensions": unused,
        "coverage": round(len(used) / total, 6) if total else 0.0,
    }


def check_conform(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings, conformed = _conformed_check(contract, dataset)
    role_findings, roles = _role_check(contract)
    bus_findings, bus = _bus_matrix(contract)
    findings.extend(role_findings)
    findings.extend(bus_findings)
    return findings, {**conformed, **roles, **bus}
