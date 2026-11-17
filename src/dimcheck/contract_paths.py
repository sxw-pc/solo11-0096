"""Contract-time parsing and validation for fact-level aggregation paths.

Kept out of ``contract.py`` for the per-file line budget; dataclasses are
imported lazily so the top-level import cycle stays harmless.
"""
from __future__ import annotations

from typing import Any

RETAIN = {"inner", "left"}


class InvalidPath(Exception):
    """Raised when an aggregation path declaration violates the contract."""
    def __init__(self, code: str, pointer: str, message: str,
                 evidence: dict[str, Any] | None = None):
        self.code = code
        self.pointer = pointer
        self.message = message
        self.evidence = evidence or {}
        super().__init__(message)


def _mapping(value: Any, pointer: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidPath("DIM-CONTRACT-001", pointer, "必须是映射对象")
    return value


def _list(value: Any, pointer: str, label: str = "必须是列表") -> list:
    if not isinstance(value, list):
        raise InvalidPath("DIM-CONTRACT-001", pointer, label)
    return value


def _str(obj: dict, key: str, pointer: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidPath("DIM-CONTRACT-001", f"{pointer}.{key}", "必须是非空字符串")
    return value.strip()


def _axes(entry: dict, refs, current: str) -> tuple[str, ...]:
    raw_axes = entry.get("axes")
    if not isinstance(raw_axes, list) or not raw_axes:
        raise InvalidPath("DIM-AGG-001", f"{current}.axes", "聚合路径至少需要一个轴")
    axes: list[str] = []
    resolved: list = []
    for axis_index, raw_axis in enumerate(raw_axes):
        axis_pointer = f"{current}.axes[{axis_index}]"
        if not isinstance(raw_axis, str) or not raw_axis.strip():
            raise InvalidPath("DIM-AGG-001", axis_pointer, "轴必须是非空字符串")
        spec = raw_axis.strip()
        if spec in axes:
            raise InvalidPath("DIM-AGG-006", axis_pointer, "聚合路径轴重复", {"axis": spec})
        resolved.append(_resolve_axis(refs, spec, axis_pointer, resolved))
        axes.append(spec)
    return tuple(axes)


def axis_candidates(refs, spec: str) -> list:
    """Resolve an axis by role, or by dimension name when the dimension is referenced once."""
    matches = [ref for ref in refs if ref.role == spec]
    by_dimension = [ref for ref in refs if ref.dimension == spec]
    if len(by_dimension) == 1 and by_dimension[0] not in matches:
        matches.append(by_dimension[0])
    return matches


def _resolve_axis(refs, spec: str, pointer: str, resolved: list):
    repeated = [ref for ref in refs if ref.dimension == spec]
    candidates = axis_candidates(refs, spec)
    if len(candidates) > 1 or len(repeated) > 1:
        raise InvalidPath("DIM-AGG-005", pointer, "聚合路径轴存在歧义",
                       {"axis": spec, "matches": len(candidates),
                        "hint": "重复引用同一维度时必须显式使用 role"})
    if not candidates:
        raise InvalidPath("DIM-AGG-004", pointer, "聚合路径轴无法解析", {"axis": spec})
    ref = candidates[0]
    if any(ref is other for other in resolved):
        raise InvalidPath("DIM-AGG-006", pointer, "多个轴解析到同一事实引用",
                       {"axis": spec, "role": ref.role, "dimension": ref.dimension})
    return ref


def parse_aggregation_paths(item: dict, refs, measures, pointer: str):
    """Parse ``facts[].aggregation_paths``; absent means no declared paths."""
    from .contract import AggregationPath

    raw = item.get("aggregation_paths")
    if raw is None:
        return ()
    result: list = []
    seen_names: set[str] = set()
    measure_names = {measure.name for measure in measures}
    for index, value in enumerate(_list(raw, f"{pointer}.aggregation_paths")):
        current = f"{pointer}.aggregation_paths[{index}]"
        entry = _mapping(value, current)
        name = _str(entry, "name", current)
        if name in seen_names:
            raise InvalidPath("DIM-AGG-002", f"{current}.name",
                           "聚合路径名称在事实内重复",
                           {"name": name, "fact": item.get("name")})
        seen_names.add(name)
        measure = _str(entry, "measure", current)
        if measure not in measure_names:
            raise InvalidPath("DIM-AGG-003", f"{current}.measure",
                           "聚合路径引用未知度量",
                           {"measure": measure, "fact": item.get("name")})
        axes = _axes(entry, refs, current)
        retain = _str(entry, "retain", current)
        if retain not in RETAIN:
            raise InvalidPath("DIM-AGG-007", f"{current}.retain",
                           "保留语义只支持 inner 或 left", {"retain": retain})
        result.append(AggregationPath(name, measure, axes, retain))
    return tuple(result)
