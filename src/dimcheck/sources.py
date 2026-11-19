"""CSV/JSONL source loading, profiling, null normalization, and row limits."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import Contract
from .util import PathError, finding, resolve_under

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SourceError(Exception):
    def __init__(self, code: str, source: str, message: str, evidence: dict[str, Any] | None = None):
        self.code = code
        self.source = source
        self.finding = finding(code, "source", source, message, evidence or {})
        super().__init__(f"{code} {source}: {message}")


@dataclass(frozen=True)
class Table:
    name: str
    source: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    profiles: dict[str, dict[str, Any]]

    def column_values(self, column: str) -> list[Any]:
        return [row.get(column) for row in self.rows]


@dataclass(frozen=True)
class Dataset:
    name: str
    tables: dict[str, Table]
    total_rows: int
    max_rows: int

    def table(self, name: str) -> Table:
        return self.tables[name]


def _scalar(value: Any, nulls: set[str]) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text in nulls or text.lower() in nulls:
            return None
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if INT_RE.fullmatch(text) and not (len(text) > 1 and text[0] == "0"):
            return int(text)
        if FLOAT_RE.fullmatch(text):
            return float(text)
        return text
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str) and DATE_RE.fullmatch(value):
        return "date"
    return "string"


def _sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value)
    return (1, str(value))


def _profile(values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if value is not None]
    kinds = {_kind(value) for value in present}
    if not kinds:
        inferred = "null"
    elif kinds <= {"integer", "float"}:
        inferred = "number" if len(kinds) == 2 else next(iter(kinds))
    elif len(kinds) == 1:
        inferred = next(iter(kinds))
    else:
        inferred = "mixed"
    distinct = sorted(set(present), key=_sort_key)
    return {
        "type": inferred,
        "null_count": len(values) - len(present),
        "distinct_count": len(distinct),
        "min": distinct[0] if distinct else None,
        "max": distinct[-1] if distinct else None,
        "sample": distinct[:3],
    }


def _build_table(name: str, source: str, columns: list[str], rows: list[dict[str, Any]]) -> Table:
    profiles = {
        column: _profile([row.get(column) for row in rows]) for column in columns
    }
    return Table(name, source, tuple(columns), tuple(rows), profiles)


def _header_check(header: list[str], source: str) -> list[str]:
    cleaned = [item.strip() for item in header]
    if not cleaned or any(not item for item in cleaned):
        raise SourceError("DIM-SRC-003", source, "表头存在空列名", {"header": cleaned})
    if len(set(cleaned)) != len(cleaned):
        raise SourceError("DIM-SRC-003", source, "表头存在重复列名", {"header": cleaned})
    return cleaned


def _read_csv(path: Path, source: str, nulls: set[str], max_rows: int,
              already: int) -> tuple[list[str], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = _header_check(next(reader), source)
            except StopIteration:
                return [], []
            for line_no, record in enumerate(reader, start=2):
                if not record:
                    continue
                if len(record) != len(header):
                    raise SourceError("DIM-SRC-002", source, "CSV 列数与表头不一致",
                                      {"line": line_no, "expected": len(header), "actual": len(record)})
                if already + len(rows) + 1 > max_rows:
                    raise SourceError("DIM-LIM-001", source, "输入行数超过上限",
                                      {"limit": max_rows, "attempted": already + len(rows) + 1})
                rows.append({key: _scalar(value, nulls) for key, value in zip(header, record)})
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        code = "DIM-SRC-001" if isinstance(exc, OSError) else "DIM-SRC-002"
        raise SourceError(code, source, "文件读取失败" if code == "DIM-SRC-001" else "CSV 编码或格式错误") from exc
    return header, rows


def _read_jsonl(path: Path, source: str, nulls: set[str], max_rows: int,
                already: int) -> tuple[list[str], list[dict[str, Any]]]:
    raw: list[dict[str, Any]] = []
    columns: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if already + len(raw) + 1 > max_rows:
                    raise SourceError("DIM-LIM-001", source, "输入行数超过上限",
                                      {"limit": max_rows, "attempted": already + len(raw) + 1})
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceError("DIM-SRC-002", source, "JSONL 行解析失败",
                                      {"line": line_no}) from exc
                if not isinstance(item, dict):
                    raise SourceError("DIM-SRC-002", source, "JSONL 每行必须是对象",
                                      {"line": line_no})
                columns.update(str(key) for key in item)
                raw.append(item)
    except (OSError, UnicodeDecodeError) as exc:
        code = "DIM-SRC-001" if isinstance(exc, OSError) else "DIM-SRC-002"
        raise SourceError(code, source, "文件读取失败" if code == "DIM-SRC-001" else "文件不是 UTF-8 编码") from exc
    ordered = sorted(columns)
    rows = [
        {column: _scalar(item.get(column), nulls) for column in ordered}
        for item in raw
    ]
    return ordered, rows


def _compatible(declared: str, observed: str) -> bool:
    if observed == "null" or declared == "string":
        return True
    if declared == "integer":
        return observed == "integer"
    if declared == "float":
        return observed in {"integer", "float", "number"}
    if declared == "boolean":
        return observed == "boolean"
    if declared == "date":
        return observed == "date"
    return True


def check_declared_schema(contract: Contract, dataset: Dataset) -> list:
    findings = []
    for dimension in contract.dimensions:
        table = dataset.table(dimension.name)
        if not table.rows:
            findings.append(finding("DIM-SRC-004", "dimension", dimension.name, "维度源为空",
                                    {"source": dimension.source}))
        for attr in dimension.attributes:
            if attr.name not in table.columns:
                findings.append(finding("DIM-SRC-005", "dimension", f"{dimension.name}.{attr.name}",
                                        "契约属性在源数据中不存在",
                                        {"source": dimension.source, "attribute": attr.name}))
                continue
            observed = table.profiles[attr.name]["type"]
            if not _compatible(attr.type, observed):
                findings.append(finding("DIM-SRC-005", "dimension", f"{dimension.name}.{attr.name}",
                                        "声明类型与推断类型不兼容",
                                        {"declared": attr.type, "observed": observed}))
    for fact in contract.facts:
        table = dataset.table(fact.name)
        if not table.rows:
            findings.append(finding("DIM-SRC-004", "fact", fact.name, "事实源为空",
                                    {"source": fact.source}))
    return findings


def read_table(name: str, path: str | Path, source: str, nulls: set[str],
               max_rows: int, already: int = 0, fmt: str | None = None) -> Table:
    """Parse one already-resolved local file into a table.

    The caller is responsible for path containment; this helper only parses.
    ``source`` is kept in the table for reporting and must not contain a secret
    absolute path. ``max_rows`` is the cumulative budget shared by the caller.
    """
    file_path = Path(path)
    resolved_format = fmt or ("jsonl" if file_path.suffix.lower() == ".jsonl" else "csv")
    if resolved_format == "jsonl":
        columns, rows = _read_jsonl(file_path, source, nulls, max_rows, already)
    else:
        columns, rows = _read_csv(file_path, source, nulls, max_rows, already)
    return _build_table(name, source, columns, rows)


def load_dataset(contract: Contract, overrides: dict[str, str | Path] | None = None) -> Dataset:
    """Load the contract dataset, optionally replacing an entity's source file.

    ``overrides`` maps an entity name (dimension or fact) to a concrete local
    path. Overrides never relax containment: the path must still resolve inside
    ``contract.root``. Intake receipt paths are already guarded by the caller,
    so the check here is defense in depth rather than the primary gate.
    """
    tables: dict[str, Table] = {}
    total = 0
    nulls = {token.strip().lower() for token in contract.null_tokens}
    nulls.add("")
    replacements = overrides or {}
    entities = list(contract.dimensions) + list(contract.facts)
    for entity in entities:
        replacement = replacements.get(entity.name)
        try:
            if replacement is None:
                path = resolve_under(contract.root, entity.source,
                                     label=f"{entity.name} 数据源")
            else:
                path = resolve_under(contract.root, replacement,
                                     label=f"{entity.name} 数据源")
        except PathError as exc:
            raise SourceError(exc.code, str(replacement or entity.source),
                              exc.message) from exc
        table = read_table(entity.name, path, entity.source, nulls,
                           contract.max_rows, total, entity.format)
        total += len(table.rows)
        if total > contract.max_rows:
            raise SourceError("DIM-LIM-001", entity.source, "输入行数超过上限",
                              {"limit": contract.max_rows, "attempted": total})
        tables[entity.name] = table
    return Dataset(contract.name, tables, total, contract.max_rows)
