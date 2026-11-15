"""Measure additivity, ratio semantics, and aggregation-safety matrix."""
from __future__ import annotations

from typing import Any

from .contract import Contract
from .sources import Dataset
from .util import finding

NUMERIC = {"integer", "float", "number"}


def _component_info(fact, measure, table, *, check_zero: bool = False) -> tuple[dict[str, Any] | None, list]:
    findings = []
    if measure is None or table is None:
        return None, findings
    profile = table.profiles.get(measure.column or "", {})
    info = {"name": measure.name, "additivity": measure.additivity,
            "column": measure.column, "type": profile.get("type")}
    if measure.additivity != "additive":
        findings.append(finding(
            "DIM-MEA-007", "measure", f"{fact.name}.{measure.name}",
            "比率分量本身不是可加度量",
            {"additivity": measure.additivity, "column": measure.column},
        ))
    if check_zero and measure.column and measure.column in table.columns:
        values = [row.get(measure.column) for row in table.rows]
        zeros = sum(1 for value in values if isinstance(value, (int, float))
                    and not isinstance(value, bool) and value == 0)
        nulls = sum(1 for value in values if value is None)
        info.update({"zero_count": zeros, "null_count": nulls})
        if zeros:
            findings.append(finding(
                "DIM-MEA-007", "measure", f"{fact.name}.{measure.name}",
                "比率分母包含 0，派生比率可能不可定义",
                {"column": measure.column, "zero_rows": zeros},
            ))
    return info, findings


def _ratio_check(fact, measure, measure_map, table) -> tuple[list, dict[str, Any]]:
    findings = []
    matrix: dict[str, Any] = {
        "fact": fact.name, "measure": measure.name, "column": None,
        "agg": measure.agg, "additivity": measure.additivity, "dtype": "ratio",
        "safe_to_sum": False,
        "safe_aggregations": ["derive_after_aggregation"],
        "reason": "比率不可直接求和；须先分别聚合分子与分母再相除。",
    }
    aggregation_ok = measure.agg == "ratio"
    if not aggregation_ok:
        findings.append(finding("DIM-MEA-002", "measure", f"{fact.name}.{measure.name}",
                                "比率度量必须使用 agg: ratio", {"agg": measure.agg}))
        matrix["reason"] = "聚合方式与 ratio 可加性冲突。"
    numerator = measure_map.get(measure.numerator or "")
    denominator = measure_map.get(measure.denominator or "")
    if numerator is None or denominator is None:
        findings.append(finding("DIM-MEA-004", "measure", f"{fact.name}.{measure.name}",
                                "比率分子或分母度量不存在",
                                {"numerator": measure.numerator,
                                 "denominator": measure.denominator}))
        matrix["numerator"] = None
        matrix["denominator"] = None
        return findings, matrix
    num_info, num_findings = _component_info(fact, numerator, table)
    den_info, den_findings = _component_info(fact, denominator, table, check_zero=True)
    findings.extend(num_findings)
    findings.extend(den_findings)
    matrix.update({
        "numerator": num_info, "denominator": den_info,
        "safe_aggregations": ["aggregate_numerator", "aggregate_denominator",
                              "derive_ratio"],
    })
    if not aggregation_ok:
        matrix["reason"] = "聚合方式与 ratio 可加性冲突。"
    elif den_info and den_info.get("zero_count", 0) == 0 and not num_findings and not den_findings:
        matrix["reason"] = "分子分母可加，先聚合后相除是安全的。"
    else:
        matrix["reason"] = "分母存在 0 或分量不可加，派生比率前必须修复。"
    return findings, matrix


def _standard_check(fact, measure, table) -> tuple[list, dict[str, Any]]:
    findings = []
    matrix: dict[str, Any] = {
        "fact": fact.name, "measure": measure.name, "column": measure.column,
        "agg": measure.agg, "additivity": measure.additivity, "dtype": None,
        "safe_to_sum": False, "safe_aggregations": [], "reason": "",
    }
    if measure.column is None or measure.column not in table.columns:
        findings.append(finding("DIM-MEA-001", "measure", f"{fact.name}.{measure.name}",
                                "度量列不存在", {"column": measure.column}))
        matrix["reason"] = "度量列缺失。"
        return findings, matrix
    profile = table.profiles[measure.column]
    dtype = profile["type"]
    matrix["dtype"] = dtype
    if measure.additivity == "additive" and measure.agg != "sum":
        findings.append(finding("DIM-MEA-002", "measure", f"{fact.name}.{measure.name}",
                                "可加度量应使用 sum 聚合",
                                {"additivity": measure.additivity, "agg": measure.agg}))
        matrix["reason"] = "可加性声明与聚合方式冲突。"
    elif measure.additivity == "semi_additive":
        if measure.agg != "sum":
            findings.append(finding("DIM-MEA-002", "measure", f"{fact.name}.{measure.name}",
                                    "半可加度量应使用 sum 聚合",
                                    {"additivity": measure.additivity, "agg": measure.agg}))
            matrix["reason"] = "半可加性声明与聚合方式冲突。"
        axes = {ref.dimension for ref in fact.refs}
        if not measure.semi_additive_axis or measure.semi_additive_axis not in axes:
            findings.append(finding("DIM-MEA-003", "measure", f"{fact.name}.{measure.name}",
                                    "半可加轴非法或缺失",
                                    {"axis": measure.semi_additive_axis,
                                     "available_dimensions": sorted(axes)}))
            matrix["reason"] = "半可加轴未绑定到事实引用的维度。"
        else:
            matrix["safe_aggregations"] = ["sum_by_non_axis_dimensions"]
            matrix["semi_additive_axis"] = measure.semi_additive_axis
            matrix["reason"] = f"仅可沿非 {measure.semi_additive_axis} 维度求和。"
    elif measure.additivity == "non_additive":
        if measure.agg == "sum":
            findings.append(finding("DIM-MEA-002", "measure", f"{fact.name}.{measure.name}",
                                    "非可加度量禁止 sum 聚合",
                                    {"additivity": measure.additivity, "agg": measure.agg}))
            matrix["reason"] = "非可加度量被声明为求和。"
        else:
            matrix["safe_aggregations"] = [measure.agg]
            matrix["reason"] = "度量非可加，只能按声明聚合方式重算。"
    elif measure.additivity == "additive" and measure.agg == "sum":
        matrix["safe_aggregations"] = ["sum"]
        matrix["reason"] = "度量可加，可在事实粒度上安全求和。"
    if measure.agg in {"sum", "avg"} and dtype not in NUMERIC:
        findings.append(finding("DIM-MEA-005", "measure", f"{fact.name}.{measure.name}",
                                "求和/平均度量必须是数值列",
                                {"column": measure.column, "type": dtype}))
        matrix["reason"] = "度量列类型非数值。"
    if measure.additivity in {"additive", "semi_additive"} and profile["null_count"]:
        findings.append(finding("DIM-MEA-006", "measure", f"{fact.name}.{measure.name}",
                                "可加度量包含空值",
                                {"column": measure.column,
                                 "null_count": profile["null_count"]}))
        if matrix["reason"] == "度量可加，可在事实粒度上安全求和。":
            matrix["reason"] = "可加度量含空值，按 0 填补会改变结果。"
    matrix["safe_to_sum"] = (
        measure.additivity == "additive" and measure.agg == "sum"
        and dtype in NUMERIC and not findings
    )
    return findings, matrix


def check_measures(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    matrix: dict[str, Any] = {}
    safe_count = 0
    for fact in contract.facts:
        table = dataset.table(fact.name)
        measure_map = {measure.name: measure for measure in fact.measures}
        for measure in fact.measures:
            if measure.additivity == "ratio":
                current, entry = _ratio_check(fact, measure, measure_map, table)
            else:
                current, entry = _standard_check(fact, measure, table)
            findings.extend(current)
            matrix[f"{fact.name}.{measure.name}"] = entry
            if entry["safe_to_sum"]:
                safe_count += 1
    return findings, {
        "measures": matrix,
        "summary": {
            "total": len(matrix), "safe_to_sum": safe_count,
            "unsafe_to_sum": len(matrix) - safe_count,
        },
    }
