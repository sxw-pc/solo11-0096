"""Markdown/HTML rendering for the independent aggregation-path section."""
from __future__ import annotations

import html
from typing import Any

from .util import canonical_json, pretty_json

HEADERS = ["事实.路径", "度量", "轴", "保留", "裁定", "事实行", "参与行", "丢失行",
           "重复行", "贡献总数", "前置"]
COUNTS = ("fact_rows", "participating_rows", "lost_rows", "amplified_rows",
          "total_contributions")
SUMMARY_KEYS = ("declared", "safe", "amplified", "incomplete", "mixed",
                "measure_blocked", "indeterminate")


def _paths(report: dict[str, Any]) -> tuple[dict, dict]:
    metric = report.get("metrics", {}).get("aggregation_paths", {})
    return metric.get("paths", {}), metric.get("summary", {})


def _prerequisite(entry: dict[str, Any]) -> str:
    gate = entry.get("prerequisite") or {}
    reason = gate.get("reason") or ""
    return f"{gate.get('state', '')}:{reason}" if reason else str(gate.get("state", ""))


def _summary_text(summary: dict, separator: str) -> str:
    return separator.join(f"{key} {summary.get(key, 0)}" for key in SUMMARY_KEYS)


def _cells(key: str, entry: dict[str, Any]) -> list[Any]:
    count = entry.get("counts") or {}
    return [key, entry.get("measure"), ", ".join(entry.get("axes", [])),
            entry.get("retain"), entry.get("verdict"),
            *[count.get(name) for name in COUNTS], _prerequisite(entry)]


def _bundle(entry: dict[str, Any]) -> dict[str, Any]:
    return {"counts": entry.get("counts"), "axis_refs": entry.get("axis_refs"),
            "prerequisite": entry.get("prerequisite"), "rows": entry.get("evidence")}


def _md(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown_paths(report: dict[str, Any]) -> list[str]:
    paths, summary = _paths(report)
    lines = ["", "## 聚合路径裁定", ""]
    if not paths:
        lines.append("未声明事实级 `aggregation_paths`；本小节无裁定。")
        return lines
    lines.append(f"共 {_summary_text(summary, '，')}。")
    lines.extend(["", "| " + " | ".join(HEADERS) + " |",
                  "| " + " | ".join("---" for _ in HEADERS) + " |"])
    for key, entry in sorted(paths.items()):
        lines.append("| " + " | ".join(_md(cell) for cell in _cells(key, entry)) + " |")
    lines.extend(["", "### 路径证据", "",
                  "事实行身份以行序号为准；粒度键可能不唯一，仅作为可读定位。", ""])
    for key, entry in sorted(paths.items()):
        lines.append(f"- `{key}`（{entry.get('verdict')}）："
                     f"`{_md(canonical_json(_bundle(entry)))}`")
    return lines


def _cell(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render_html_paths(report: dict[str, Any]) -> str:
    paths, summary = _paths(report)
    if not paths:
        body = '<p class="ok">未声明事实级 aggregation_paths；无路径裁定。</p>'
    else:
        rows = "".join(
            "<tr>" + "".join(f"<td>{_cell(value)}</td>" for value in _cells(key, entry))
            + "</tr>"
            for key, entry in sorted(paths.items())
        )
        evidence = "".join(
            "<details><summary><code>{}</code> {}</summary><pre>{}</pre></details>".format(
                _cell(key), _cell(entry.get("verdict")),
                html.escape(pretty_json(_bundle(entry))))
            for key, entry in sorted(paths.items())
        )
        head = "".join(f"<th>{html.escape(name)}</th>" for name in HEADERS)
        body = (
            f"<p>共 {_summary_text(summary, ' · ')}</p>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
            "<h3>路径证据</h3><p>事实行身份以行序号为准；粒度键可能不唯一，仅作可读定位。</p>"
            + evidence
        )
    return f"\n<section><h2>聚合路径裁定</h2>{body}</section>"
