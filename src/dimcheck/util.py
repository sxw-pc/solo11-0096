"""Shared deterministic, path-safety, and finding primitives for dimcheck."""
from __future__ import annotations
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
# code -> (severity, short title, remediation)
CODES: dict[str, tuple[str, str, str]] = {
    "DIM-CONTRACT-001": ("error", "契约结构不合法", "按 contracts/README.md 修正字段与类型。"), "DIM-CONTRACT-002": ("error", "对象名或键重复", "为维度、事实、属性、度量使用唯一名称。"),
    "DIM-CONTRACT-003": ("error", "引用了未知维度", "在 dimensions 中声明该维度或修正 refs。"), "DIM-CONTRACT-004": ("error", "契约版本不受支持", "使用 schema_version: 1。"),
    "DIM-PATH-001": ("error", "路径越界或被拒绝", "只读取数据集根目录内的文件。"), "DIM-PATH-002": ("error", "路径不存在或不可读", "检查路径拼写与文件权限。"),
    "DIM-LIM-001": ("error", "输入行数超过上限", "缩减样例数据或显式提高 dataset.max_rows。"), "DIM-SRC-001": ("error", "数据源不可读", "确认 source 指向可读的 CSV/JSONL 文件。"),
    "DIM-SRC-002": ("error", "数据源格式错误", "修正 JSONL 行、CSV 列数或编码。"), "DIM-SRC-003": ("error", "列名重复或为空", "为每个数据列提供唯一非空表头。"),
    "DIM-SRC-004": ("warning", "数据源为空", "补充样例数据或确认该表允许为空。"), "DIM-SRC-005": ("error", "声明类型与数据不符", "修正 attribute.type 或源数据取值。"),
    "DIM-KEY-001": ("error", "事实表粒度不唯一", "去重事实行或把缺失列加入 grain。"), "DIM-KEY-002": ("error", "维表代理键重复", "在每个维表中保证代理键唯一。"),
    "DIM-KEY-003": ("warning", "维表自然键重复", "确认是否隐含 SCD 历史；本工具不展开 SCD2。"), "DIM-KEY-004": ("error", "键列存在空值", "补齐键值或使用可审计的未知成员。"),
    "DIM-KEY-005": ("error", "键列缺失", "在源数据中补列或修正契约列名。"), "DIM-REF-001": ("error", "引用列缺失", "在事实表或维表中补齐引用列。"),
    "DIM-REF-002": ("warning", "事实存在孤儿键", "补维表成员、修正事实键或登记 unknown_members。"), "DIM-REF-003": ("error", "必填外键为空", "补齐外键；确属可空时声明 optional: true。"),
    "DIM-REF-004": ("error", "雪花维引用不完整", "修正 snowflake 列映射并补齐父维成员。"), "DIM-REF-005": ("error", "雪花维存在环", "删除维表父引用中的循环。"),
    "DIM-FAN-001": ("warning", "连接发生行数放大", "先修复维键唯一性，再按事实粒度重算度量。"), "DIM-MEA-001": ("error", "度量列缺失", "补充度量列或修正 measure.column。"),
    "DIM-MEA-002": ("error", "聚合方式与可加性冲突", "按 additivity 选择 sum/avg/ratio 等聚合。"), "DIM-MEA-003": ("error", "半可加轴非法", "semi_additive_axis 必须是本事实引用的维度。"),
    "DIM-MEA-004": ("error", "比率定义非法", "ratio 度量必须给出同一事实内的分子与分母度量。"), "DIM-MEA-005": ("error", "度量不是数值列", "对求和/平均度量使用数值列。"),
    "DIM-MEA-006": ("warning", "可加度量含空值", "明确空值语义；不要默认按 0 处理。"), "DIM-MEA-007": ("warning", "比率分量不可安全聚合", "先修复分子/分母可加性再计算比率。"),
    "DIM-CON-001": ("error", "一致性维度属性集不同", "统一 conformed_group 内维度的属性清单。"), "DIM-CON-002": ("error", "一致性维度属性类型不同", "统一属性类型定义。"),
    "DIM-CON-003": ("warning", "一致性维度取值域不同", "核对维度成员与编码映射。"), "DIM-CON-004": ("error", "重复引用缺少角色", "同一事实多次引用同一维度时显式声明 role。"),
    "DIM-CON-005": ("error", "事实内角色重复", "为同一事实中的维度引用使用唯一角色。"), "DIM-CON-006": ("warning", "角色声明不一致", "同一维度在各事实中统一声明或不声明角色。"),
    "DIM-CON-007": ("error", "事实内角色冲突", "不同维度不要共享同一角色名。"), "DIM-CON-008": ("warning", "事实表未引用维度", "为事实表声明至少一个维度引用。"),
    "DIM-CON-009": ("info", "维度未被使用", "删除未使用维度或补充业务引用。"), "DIM-IO-001": ("error", "输出写入失败", "检查 --out 目录权限与磁盘空间。"),
    "DIM-CLI-001": ("error", "命令行参数非法", "运行 python -m dimcheck --help 查看参数。"), "DIM-EXPLAIN-001": ("error", "未知错误码", "先运行 explain 且不带参数列出全部错误码。"),
    "DIM-INTERNAL-001": ("error", "内部异常", "保留输入与堆栈反馈给维护者。"),
    "DIM-TO-001": ("error", "转线事件结构非法", "修正事件对象字段与非空字符串。"), "DIM-TO-002": ("error", "转线事件没有实际转线", "原线路与新线路必须不同。"),
    "DIM-TO-003": ("error", "转线区间冲突", "同一车站的生效区间必须半开、有序且无冲突。"), "DIM-TO-004": ("error", "接管策略非法", "只使用 atomic_cutover 或 partitioned_cutover。"),
    "DIM-TO-005": ("error", "交付收据失效", "重新登记交付；来源字节或行数变化后不能沿用旧计划。"), "DIM-TO-006": ("error", "车站身份不唯一", "在批次内保证站码与代理键唯一。"),
    "DIM-TO-007": ("error", "代理键跨批次复用", "同一代理键在新批次指向了另一站码。"), "DIM-TO-008": ("error", "转线指向未知线路", "在 dim_line 中补齐线路或修正事件。"),
    "DIM-TO-009": ("error", "接管所需对象或列缺失", "补齐契约中的 dim_line/dim_station/dim_date/fct_ridership。"), "DIM-TO-010": ("error", "接管计划被拒绝", "修正输入后重新生成计划；不得改写已确认结论。"),
    "DIM-TO-011": ("warning", "接管计划待核", "逐行复核 pending 证据后再确认。"),
}
@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    object_type: str
    object_name: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.code, self.object_name, canonical_json(self.evidence))
    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "object_type": self.object_type,
            "object_name": self.object_name,
            "message": self.message,
            "fix": CODES.get(self.code, ("", "", ""))[2],
            "evidence": self.evidence,
        }
def finding(code: str, object_type: str, object_name: str, message: str,
            evidence: dict[str, Any] | None = None, severity: str | None = None) -> Finding:
    default = CODES.get(code, ("error", "", ""))[0]
    return Finding(code, severity or default, object_type, object_name, message, evidence or {})
def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))
def value_token(value: Any) -> str:
    if value is None:
        return "null"
    return canonical_json([type(value).__name__, value])
def sort_findings(items: Iterable[Finding]) -> list[Finding]:
    return sorted(items, key=lambda item: item.sort_key)
def exit_code_for(findings: Iterable[Finding]) -> int:
    severities = {item.severity for item in findings}
    if "error" in severities:
        return 2
    if "warning" in severities:
        return 3
    return 0
def report_epoch(default: int) -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return default
def epoch_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def repo_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "contracts" / "model.yaml").is_file():
        return source_root
    return Path.cwd()
class PathError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
def is_network_path(raw: str | Path) -> bool:
    return str(raw).replace("\\", "/").startswith("//")
def _sensitive(path: Path) -> bool:
    text = str(path).replace("\\", "/").lower().rstrip("/")
    if text.startswith("//"):
        return True
    if len(text) > 1 and text[1] == ":":
        text = text[2:]
    for root in ("/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/var/run"):
        if text == root or text.startswith(root + "/"):
            return True
    system_root = os.environ.get("SystemRoot")
    if system_root:
        sys_text = str(Path(system_root).resolve()).replace("\\", "/").lower().rstrip("/")
        if len(sys_text) > 1 and sys_text[1] == ":":
            sys_text = sys_text[2:]
        if text == sys_text or text.startswith(sys_text + "/"):
            return True
    return False
def is_sensitive_path(raw: str | Path) -> bool:
    return _sensitive(Path(raw))
def guard_contract_path(raw: str | Path) -> Path:
    path = Path(raw)
    if is_network_path(path):
        raise PathError("DIM-PATH-001", f"契约路径被拒绝: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PathError("DIM-PATH-002", f"契约文件不可读: {path}") from exc
    if _sensitive(resolved) or not resolved.is_file():
        raise PathError("DIM-PATH-001", f"契约路径被拒绝: {path}")
    return resolved
def resolve_under(root: str | Path, raw: str | Path, *, label: str = "路径") -> Path:
    if is_network_path(root) or is_network_path(raw):
        raise PathError("DIM-PATH-001", f"{label}越界: {raw}")
    root_path = Path(root).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PathError("DIM-PATH-002", f"{label}不存在: {raw}") from exc
    if resolved != root_path and root_path not in resolved.parents:
        raise PathError("DIM-PATH-001", f"{label}越界: {raw}")
    if not resolved.is_file():
        raise PathError("DIM-PATH-001", f"{label}不是文件: {raw}")
    return resolved
def ensure_out_dir(raw: str | Path) -> Path:
    if is_network_path(raw):
        raise PathError("DIM-PATH-001", f"输出目录被拒绝: {raw}")
    path = Path(raw).resolve()
    if _sensitive(path):
        raise PathError("DIM-PATH-001", f"输出目录被拒绝: {raw}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathError("DIM-IO-001", f"无法创建输出目录: {raw}") from exc
    return path
def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
