"""Fact grain and dimension key uniqueness checks."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .contract import Contract
from .sources import Dataset
from .util import finding, value_token


def _missing(table, columns: tuple[str, ...]) -> list[str]:
    return [column for column in columns if column not in table.columns]


def _null_count(rows, columns: tuple[str, ...]) -> int:
    return sum(1 for row in rows if any(row.get(column) is None for column in columns))


def _duplicates(rows, columns: tuple[str, ...]) -> tuple[list[dict[str, Any]], int]:
    counter: Counter[tuple[str, ...]] = Counter()
    examples: dict[tuple[str, ...], list[Any]] = {}
    for row in rows:
        values = [row.get(column) for column in columns]
        if any(value is None for value in values):
            continue
        token = tuple(value_token(value) for value in values)
        counter[token] += 1
        examples.setdefault(token, values)
    groups = [
        {"key": examples[token], "count": count}
        for token, count in counter.items() if count > 1
    ]
    groups.sort(key=lambda item: tuple(value_token(value) for value in item["key"]))
    return groups[:20], sum(group["count"] for group in groups)


def _missing_findings(table, columns: tuple[str, ...], object_type: str,
                      object_name: str) -> list:
    return [
        finding("DIM-KEY-005", object_type, f"{object_name}.{column}", "键列不存在",
                {"table": table.name, "column": column})
        for column in _missing(table, columns)
    ]


def _dimension_checks(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    for dimension in contract.dimensions:
        table = dataset.table(dimension.name)
        surrogate = (dimension.surrogate_key,)
        item: dict[str, Any] = {"rows": len(table.rows)}
        findings.extend(_missing_findings(table, surrogate, "dimension",
                                          f"{dimension.name}.surrogate_key"))
        if not _missing(table, surrogate):
            null_rows = _null_count(table.rows, surrogate)
            groups, duplicate_rows = _duplicates(table.rows, surrogate)
            if null_rows:
                findings.append(finding("DIM-KEY-004", "dimension",
                                        f"{dimension.name}.{dimension.surrogate_key}",
                                        "代理键存在空值", {"null_rows": null_rows}))
            if groups:
                findings.append(finding("DIM-KEY-002", "dimension",
                                        f"{dimension.name}.{dimension.surrogate_key}",
                                        "代理键不唯一", {"groups": groups,
                                                       "rows_in_duplicate_groups": duplicate_rows}))
            item.update({
                "surrogate_key": dimension.surrogate_key,
                "surrogate_null_rows": null_rows,
                "surrogate_duplicate_groups": len(groups),
                "surrogate_duplicate_rows": duplicate_rows,
                "surrogate_unique": not groups and not null_rows,
                "distinct_surrogate_keys": len(table.rows) - null_rows - duplicate_rows
                + len(groups),
            })
        natural = dimension.natural_keys
        if natural:
            findings.extend(_missing_findings(table, natural, "dimension",
                                              f"{dimension.name}.natural_keys"))
            if not _missing(table, natural):
                null_rows = _null_count(table.rows, natural)
                groups, duplicate_rows = _duplicates(table.rows, natural)
                if null_rows:
                    findings.append(finding("DIM-KEY-004", "dimension",
                                            f"{dimension.name}.natural_keys",
                                            "自然键存在空值", {"null_rows": null_rows}))
                if groups:
                    findings.append(finding("DIM-KEY-003", "dimension",
                                            f"{dimension.name}.natural_keys",
                                            "自然键不唯一；本工具不展开 SCD2",
                                            {"groups": groups,
                                             "rows_in_duplicate_groups": duplicate_rows}))
                item.update({
                    "natural_keys": list(natural),
                    "natural_null_rows": null_rows,
                    "natural_duplicate_groups": len(groups),
                    "natural_duplicate_rows": duplicate_rows,
                })
        metrics[dimension.name] = item
    return findings, {"dimensions": metrics}


def _fact_checks(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings = []
    metrics: dict[str, Any] = {}
    for fact in contract.facts:
        table = dataset.table(fact.name)
        item: dict[str, Any] = {"rows": len(table.rows), "grain": list(fact.grain)}
        findings.extend(_missing_findings(table, fact.grain, "fact", f"{fact.name}.grain"))
        if not _missing(table, fact.grain):
            null_rows = _null_count(table.rows, fact.grain)
            groups, duplicate_rows = _duplicates(table.rows, fact.grain)
            if null_rows:
                findings.append(finding("DIM-KEY-004", "fact", f"{fact.name}.grain",
                                        "粒度键存在空值", {"null_rows": null_rows}))
            if groups:
                findings.append(finding("DIM-KEY-001", "fact", f"{fact.name}.grain",
                                        "事实表粒度不唯一",
                                        {"grain": list(fact.grain), "groups": groups,
                                         "rows_in_duplicate_groups": duplicate_rows}))
            item.update({
                "grain_null_rows": null_rows,
                "grain_duplicate_groups": len(groups),
                "grain_duplicate_rows": duplicate_rows,
                "grain_unique": not groups and not null_rows,
                "distinct_grain_keys": len(table.rows) - null_rows - duplicate_rows
                + len(groups),
            })
        metrics[fact.name] = item
    return findings, {"facts": metrics}


def check_keys(contract: Contract, dataset: Dataset) -> tuple[list, dict]:
    findings, dim_metrics = _dimension_checks(contract, dataset)
    fact_findings, fact_metrics = _fact_checks(contract, dataset)
    findings.extend(fact_findings)
    return findings, {**dim_metrics, **fact_metrics}
