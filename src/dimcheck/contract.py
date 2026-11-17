"""Declarative model contract loading, validation, and fingerprinting."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml
from .contract_paths import InvalidPath, parse_aggregation_paths
from .util import finding, fingerprint, is_network_path, is_sensitive_path
class ContractError(Exception):
    def __init__(self, findings):
        self.findings = list(findings)
        super().__init__("; ".join(f"{f.code} {f.object_name}: {f.message}" for f in self.findings))
@dataclass(frozen=True)
class Attribute: name: str; type: str
@dataclass(frozen=True)
class Snowflake: parent: str; child_key: str; parent_key: str
@dataclass(frozen=True)
class Dimension:
    name: str; source: str; format: str; surrogate_key: str; natural_keys: tuple[str, ...]
    attributes: tuple[Attribute, ...]; conformed_group: str | None; snowflake: Snowflake | None
@dataclass(frozen=True)
class Ref:
    dimension: str; fact_key: str; dimension_key: str; role: str | None
    optional: bool; unknown_members: tuple[Any, ...]
@dataclass(frozen=True)
class Measure:
    name: str; column: str | None; agg: str; additivity: str; unit: str | None = None
    numerator: str | None = None; denominator: str | None = None; semi_additive_axis: str | None = None
@dataclass(frozen=True)
class AggregationPath:
    name: str; measure: str; axes: tuple[str, ...]; retain: str
@dataclass(frozen=True)
class Fact:
    name: str; source: str; format: str; grain: tuple[str, ...]
    refs: tuple[Ref, ...]; measures: tuple[Measure, ...]
    aggregation_paths: tuple[AggregationPath, ...] = ()
@dataclass(frozen=True)
class Contract:
    name: str; root: Path; max_rows: int; run_epoch: int; null_tokens: tuple[str, ...]
    dimensions: tuple[Dimension, ...]; facts: tuple[Fact, ...]; fingerprint: str; file_name: str
    def dimensions_by_name(self) -> dict[str, Dimension]:
        return {item.name: item for item in self.dimensions}
    def facts_by_name(self) -> dict[str, Fact]:
        return {item.name: item for item in self.facts}
class _DuplicateKey(Exception): pass
class _Loader(yaml.SafeLoader): pass
def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKey(str(key))
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping
_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
FORMATS = {"csv", "jsonl"}
TYPES = {"integer", "float", "string", "boolean", "date"}
ADDITIVITY = {"additive", "semi_additive", "non_additive", "ratio"}
AGGS = {"sum", "avg", "min", "max", "count", "count_distinct", "ratio"}
def _fail(code: str, pointer: str, message: str, evidence: dict | None = None):
    raise ContractError([finding(code, "contract", pointer, message, evidence or {})])
def _mapping(value: Any, pointer: str) -> dict:
    if not isinstance(value, dict): _fail("DIM-CONTRACT-001", pointer, "必须是映射对象")
    return value
def _list(value: Any, pointer: str) -> list:
    if not isinstance(value, list): _fail("DIM-CONTRACT-001", pointer, "必须是列表")
    return value
def _str(obj: dict, key: str, pointer: str, *, optional: bool = False) -> str | None:
    value = obj.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail("DIM-CONTRACT-001", f"{pointer}.{key}", "必须是非空字符串")
    return value.strip()
def _int(obj: dict, key: str, pointer: str, default: int | None = None) -> int:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("DIM-CONTRACT-001", f"{pointer}.{key}", "必须是正整数")
    return value
def _str_list(obj: dict, key: str, pointer: str, *, required: bool = True) -> tuple[str, ...]:
    if key not in obj and not required:
        return ()
    values = _list(obj.get(key), f"{pointer}.{key}")
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            _fail("DIM-CONTRACT-001", f"{pointer}.{key}[{index}]", "必须是非空字符串")
        result.append(value.strip())
    if len(set(result)) != len(result):
        _fail("DIM-CONTRACT-002", f"{pointer}.{key}", "列表内存在重复值")
    return tuple(result)
def _format(obj: dict, pointer: str) -> str:
    fmt = obj.get("format")
    if fmt is None:
        source = str(obj.get("source", ""))
        fmt = "jsonl" if source.lower().endswith(".jsonl") else "csv"
    if fmt not in FORMATS:
        _fail("DIM-CONTRACT-001", f"{pointer}.format", "仅支持 csv 或 jsonl")
    return fmt
def _attribute(raw: Any, pointer: str) -> Attribute:
    item = _mapping(raw, pointer)
    kind = _str(item, "type", pointer)
    if kind not in TYPES:
        _fail("DIM-CONTRACT-001", f"{pointer}.type", "属性类型不受支持", {"type": kind})
    return Attribute(_str(item, "name", pointer), kind)
def _dimension(raw: Any, index: int) -> Dimension:
    pointer = f"dimensions[{index}]"
    item = _mapping(raw, pointer)
    attrs = tuple(_attribute(value, f"{pointer}.attributes[{i}]")
                  for i, value in enumerate(_list(item.get("attributes"), f"{pointer}.attributes")))
    if not attrs:
        _fail("DIM-CONTRACT-001", f"{pointer}.attributes", "至少需要一个属性")
    names = [attr.name for attr in attrs]
    if len(set(names)) != len(names):
        _fail("DIM-CONTRACT-002", f"{pointer}.attributes", "属性名重复")
    snowflake = None
    raw_snow = item.get("snowflake")
    if raw_snow is not None:
        snow = _mapping(raw_snow, f"{pointer}.snowflake")
        snowflake = Snowflake(
            _str(snow, "parent", f"{pointer}.snowflake"),
            _str(snow, "child_key", f"{pointer}.snowflake"),
            _str(snow, "parent_key", f"{pointer}.snowflake"),
        )
    return Dimension(
        name=_str(item, "name", pointer),
        source=_str(item, "source", pointer),
        format=_format(item, pointer),
        surrogate_key=_str(item, "surrogate_key", pointer),
        natural_keys=_str_list(item, "natural_keys", pointer, required=False),
        attributes=attrs,
        conformed_group=_str(item, "conformed_group", pointer, optional=True),
        snowflake=snowflake,
    )
def _ref(raw: Any, pointer: str) -> Ref:
    item = _mapping(raw, pointer)
    optional = item.get("optional", False)
    if not isinstance(optional, bool):
        _fail("DIM-CONTRACT-001", f"{pointer}.optional", "必须是布尔值")
    unknown = _list(item.get("unknown_members", []), f"{pointer}.unknown_members")
    for index, value in enumerate(unknown):
        if isinstance(value, (dict, list)) or value is None:
            _fail("DIM-CONTRACT-001", f"{pointer}.unknown_members[{index}]", "必须标量")
    return Ref(
        dimension=_str(item, "dimension", pointer),
        fact_key=_str(item, "fact_key", pointer),
        dimension_key=_str(item, "dimension_key", pointer),
        role=_str(item, "role", pointer, optional=True),
        optional=optional,
        unknown_members=tuple(unknown),
    )
def _measure(raw: Any, pointer: str) -> Measure:
    item = _mapping(raw, pointer)
    additivity = _str(item, "additivity", pointer)
    agg = _str(item, "agg", pointer)
    if additivity not in ADDITIVITY:
        _fail("DIM-CONTRACT-001", f"{pointer}.additivity", "可加性声明不受支持")
    if agg not in AGGS:
        _fail("DIM-CONTRACT-001", f"{pointer}.agg", "聚合方式不受支持")
    column = _str(item, "column", pointer, optional=True)
    if additivity != "ratio" and column is None:
        _fail("DIM-CONTRACT-001", f"{pointer}.column", "非比率度量必须声明列")
    numerator = _str(item, "numerator", pointer, optional=True)
    denominator = _str(item, "denominator", pointer, optional=True)
    if additivity == "ratio" and (numerator is None or denominator is None):
        _fail("DIM-CONTRACT-001", f"{pointer}", "比率度量必须声明 numerator 与 denominator")
    return Measure(
        name=_str(item, "name", pointer), column=column, agg=agg, additivity=additivity,
        unit=_str(item, "unit", pointer, optional=True), numerator=numerator,
        denominator=denominator,
        semi_additive_axis=_str(item, "semi_additive_axis", pointer, optional=True),
    )
def _aggregation_paths(item: dict, refs: tuple[Ref, ...], measures: tuple[Measure, ...],
                       pointer: str) -> tuple[AggregationPath, ...]:
    try:
        return parse_aggregation_paths(item, refs, measures, pointer)
    except InvalidPath as exc:
        raise ContractError([finding(exc.code, "contract", exc.pointer,
                                     exc.message, exc.evidence)]) from exc


def _fact(raw: Any, index: int) -> Fact:
    pointer = f"facts[{index}]"
    item = _mapping(raw, pointer)
    refs = tuple(_ref(value, f"{pointer}.refs[{i}]")
                 for i, value in enumerate(_list(item.get("refs", []), f"{pointer}.refs")))
    measures = tuple(_measure(value, f"{pointer}.measures[{i}]")
                     for i, value in enumerate(_list(item.get("measures"), f"{pointer}.measures")))
    if not measures:
        _fail("DIM-CONTRACT-001", f"{pointer}.measures", "至少需要一个度量")
    names = [measure.name for measure in measures]
    if len(set(names)) != len(names):
        _fail("DIM-CONTRACT-002", f"{pointer}.measures", "度量名重复")
    return Fact(
        name=_str(item, "name", pointer), source=_str(item, "source", pointer),
        format=_format(item, pointer), grain=_str_list(item, "grain", pointer),
        refs=refs, measures=measures,
        aggregation_paths=_aggregation_paths(item, refs, measures, pointer),
    )
def _validate_model(dimensions: tuple[Dimension, ...], facts: tuple[Fact, ...]) -> None:
    dim_names = [item.name for item in dimensions]
    fact_names = [item.name for item in facts]
    if len(set(dim_names)) != len(dim_names):
        _fail("DIM-CONTRACT-002", "dimensions", "维度名重复")
    if len(set(fact_names)) != len(fact_names):
        _fail("DIM-CONTRACT-002", "facts", "事实名重复")
    if set(dim_names) & set(fact_names):
        _fail("DIM-CONTRACT-002", "model", "维度与事实不能同名")
    dim_set = set(dim_names)
    for dim in dimensions:
        if dim.snowflake and dim.snowflake.parent not in dim_set:
            _fail("DIM-CONTRACT-003", f"dimension {dim.name}.snowflake.parent", "父维度未声明")
    for fact in facts:
        measure_map = {item.name: item for item in fact.measures}
        for ref in fact.refs:
            if ref.dimension not in dim_set:
                _fail("DIM-CONTRACT-003", f"fact {fact.name}.refs", "引用了未知维度",
                      {"dimension": ref.dimension})
        for measure in fact.measures:
            if measure.additivity != "ratio":
                continue
            for label, name in (("numerator", measure.numerator),
                                ("denominator", measure.denominator)):
                if name == measure.name:
                    _fail("DIM-CONTRACT-001", f"measure {measure.name}.{label}", "比率不能引用自身")
                if name not in measure_map:
                    _fail("DIM-CONTRACT-003", f"measure {measure.name}.{label}", "引用未知度量",
                          {"measure": name})
def load_contract(path: str | Path) -> Contract:
    source = Path(path)
    try:
        resolved = source.resolve(strict=True)
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError([finding("DIM-PATH-002", "contract", str(source), "契约文件不可读")]) from exc
    try:
        loader = _Loader(text)
        try:
            raw = loader.get_single_data()
        finally:
            loader.dispose()
    except _DuplicateKey as exc:
        raise ContractError([finding("DIM-CONTRACT-002", "contract", "$", "YAML 键重复",
                                     {"key": str(exc)})]) from exc
    except yaml.YAMLError as exc:
        raise ContractError([finding("DIM-CONTRACT-001", "contract", "$", "YAML 解析失败",
                                     {"error": str(exc)})]) from exc
    root = _mapping(raw, "$")
    version = root.get("schema_version")
    if version != 1:
        _fail("DIM-CONTRACT-004", "$.schema_version", "仅支持 schema_version 1", {"value": version})
    dataset = _mapping(root.get("dataset"), "$.dataset")
    run_epoch = _int(root, "run_epoch", "$")
    max_rows = _int(dataset, "max_rows", "$.dataset", default=500000)
    null_tokens = _str_list(dataset, "null_tokens", "$.dataset", required=False)
    if not null_tokens:
        null_tokens = ("", "NULL", "null")
    dimensions = tuple(_dimension(value, index)
                       for index, value in enumerate(_list(root.get("dimensions"), "$.dimensions")))
    facts = tuple(_fact(value, index)
                  for index, value in enumerate(_list(root.get("facts"), "$.facts")))
    if not dimensions or not facts:
        _fail("DIM-CONTRACT-001", "$", "dimensions 与 facts 均不能为空")
    _validate_model(dimensions, facts)
    root_raw = dataset.get("root", ".")
    if not isinstance(root_raw, str): _fail("DIM-CONTRACT-001", "$.dataset.root", "必须是字符串路径")
    dataset_root = (resolved.parent / root_raw).resolve() if not is_network_path(root_raw) else None
    if dataset_root is None or is_sensitive_path(dataset_root): _fail("DIM-PATH-001", "$.dataset.root", "路径被拒绝")
    if not dataset_root.is_dir():
        _fail("DIM-PATH-002", "$.dataset.root", "数据集根目录不存在")
    return Contract(
        name=_str(dataset, "name", "$.dataset"), root=dataset_root, max_rows=max_rows,
        run_epoch=run_epoch, null_tokens=tuple(null_tokens), dimensions=dimensions,
        facts=facts, fingerprint=fingerprint(root), file_name=resolved.name,
    )
