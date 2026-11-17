"""Deterministic JSON, Markdown, and self-contained HTML report rendering."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from . import __version__
from .contract import Contract
from .sources import Dataset
from .util import (Finding, canonical_json, epoch_iso, exit_code_for, pretty_json,
                   sort_findings, write_text)


def _conclusion(verdict: str, errors: int, warnings: int) -> str:
    if verdict == "fail":
        return f"存在 {errors} 个阻断问题；当前模型不应被安全聚合使用。"
    if verdict == "warn":
        return f"存在 {warnings} 个风险提示；聚合前请逐条复核证据。"
    return "键、粒度、引用、放大、可加性与一致性检查均通过。"


def build_report(contract: Contract, dataset: Dataset, findings: list[Finding],
                 metrics: dict[str, Any], epoch: int) -> dict[str, Any]:
    ordered = sort_findings(findings)
    counts = {level: sum(1 for item in ordered if item.severity == level)
              for level in ("error", "warning", "info")}
    verdict = "fail" if counts["error"] else ("warn" if counts["warning"] else "pass")
    by_code: dict[str, int] = {}
    for item in ordered:
        by_code[item.code] = by_code.get(item.code, 0) + 1
    tables = {
        name: {"source": table.source, "rows": len(table.rows),
               "columns": list(table.columns), "profiles": table.profiles}
        for name, table in dataset.tables.items()
    }
    return {
        "tool": {"name": "dimcheck", "version": __version__},
        "run_id": f"{contract.name}-{contract.fingerprint[:12]}",
        "contract": {"name": contract.name, "file": contract.file_name,
                     "fingerprint": contract.fingerprint,
                     "dimensions": len(contract.dimensions), "facts": len(contract.facts)},
        "dataset": {"name": dataset.name, "total_rows": dataset.total_rows,
                    "max_rows": dataset.max_rows, "tables": tables},
        "generated_at": epoch_iso(epoch),
        "generated_epoch": epoch,
        "verdict": verdict,
        "exit_code": exit_code_for(ordered),
        "summary": {
            "errors": counts["error"], "warnings": counts["warning"], "infos": counts["info"],
            "findings": len(ordered), "dimensions": len(contract.dimensions),
            "facts": len(contract.facts), "tables": len(dataset.tables),
            "rows": dataset.total_rows, "by_code": by_code,
        },
        "conclusion": _conclusion(verdict, counts["error"], counts["warning"]),
        "findings": [item.to_dict() for item in ordered],
        "metrics": metrics,
    }


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_md(cell) for cell in row) + " |" for row in rows)
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 维模体检台报告", "",
        f"- Run ID：`{report['run_id']}`",
        f"- 契约：{report['contract']['name']} (`{report['contract']['file']}`)",
        f"- 契约指纹：`{report['contract']['fingerprint']}`",
        f"- 生成时间（SOURCE_DATE_EPOCH）：{report['generated_at']}",
        f"- 结论：**{report['verdict']}**，退出码 {report['exit_code']}",
        f"- {report['conclusion']}", "",
        "## 摘要", "",
    ]
    lines.extend(_table(
        ["项目", "值"],
        [["维度", summary["dimensions"]], ["事实", summary["facts"]],
         ["数据表", summary["tables"]], ["输入行数", summary["rows"]],
         ["error", summary["errors"]], ["warning", summary["warnings"]],
         ["info", summary["infos"]]],
    ))
    lines.extend(["", "## 发现项", ""])
    findings = report["findings"]
    if not findings:
        lines.append("未发现契约、数据或聚合安全问题。")
    else:
        lines.extend(_table(
            ["级别", "代码", "对象", "说明", "修复建议"],
            [[item["severity"], item["code"], item["object_name"], item["message"],
              item["fix"]] for item in findings],
        ))
        lines.extend(["", "### 证据", ""])
        for item in findings:
            lines.append(f"- `{item['code']}` `{item['object_name']}`："
                         f"`{_md(canonical_json(item['evidence']))}`")
    lines.extend(["", "## 聚合安全矩阵", ""])
    measures = report["metrics"].get("measures", {}).get("measures", {})
    lines.extend(_table(
        ["事实.度量", "聚合", "可加性", "可安全求和", "可用聚合", "原因"],
        [[key, item.get("agg"), item.get("additivity"), item.get("safe_to_sum"),
          ", ".join(item.get("safe_aggregations", [])), item.get("reason")]
         for key, item in sorted(measures.items())],
    ))
    lines.extend(["", "## 总线矩阵", ""])
    bus = report["metrics"].get("conform", {}).get("bus_matrix", {})
    lines.extend(_table(
        ["事实", "引用维度", "角色"],
        [[fact, ", ".join(item["dimensions"]), ", ".join(item["roles"])]
         for fact, item in sorted(bus.items())],
    ))
    lines.extend(["", "## 数据画像", ""])
    profile_rows = []
    for table_name, table in sorted(report["dataset"]["tables"].items()):
        for column, profile in sorted(table["profiles"].items()):
            profile_rows.append([table_name, column, profile["type"],
                                 profile["null_count"], profile["distinct_count"],
                                 profile["min"], profile["max"]])
    lines.extend(_table(["表", "列", "类型", "空值", "基数", "最小", "最大"], profile_rows))
    lines.extend([
        "", "---", "本报告由 dimcheck 离线生成；未发起网络请求，未改写源数据。", "",
    ])
    return "\n".join(lines)


def _html_findings(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return '<p class="ok">未发现契约、数据或聚合安全问题。</p>'
    items = []
    for item in findings:
        evidence = html.escape(pretty_json(item.get("evidence", {})))
        items.append(
            f'<article class="finding {html.escape(item["severity"])}">'
            f'<div class="badge">{html.escape(item["severity"])}</div>'
            f'<div><h3><code>{html.escape(item["code"])}</code> '
            f'{html.escape(item["object_name"])}</h3>'
            f'<p>{html.escape(item["message"])}</p>'
            f'<p class="fix">修复：{html.escape(item.get("fix", ""))}</p>'
            f'<details><summary>证据</summary><pre>{evidence}</pre></details></div></article>'
        )
    return "\n".join(items)


def render_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    measures = report["metrics"].get("measures", {}).get("measures", {})
    measure_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(key)}</code></td>"
        f"<td>{html.escape(str(item.get('agg')))}</td>"
        f"<td>{html.escape(str(item.get('additivity')))}</td>"
        f"<td>{html.escape(str(item.get('safe_to_sum')))}</td>"
        f"<td>{html.escape(', '.join(item.get('safe_aggregations', [])))}</td>"
        f"<td>{html.escape(str(item.get('reason', '')))}</td></tr>"
        for key, item in sorted(measures.items())
    )
    bus = report["metrics"].get("conform", {}).get("bus_matrix", {})
    bus_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(fact)}</code></td>"
        f"<td>{html.escape(', '.join(item['dimensions']))}</td>"
        f"<td>{html.escape(', '.join(item['roles']))}</td></tr>"
        for fact, item in sorted(bus.items())
    )
    profile_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(table_name)}</code></td>"
        f"<td>{html.escape(column)}</td><td>{html.escape(profile['type'])}</td>"
        f"<td>{profile['null_count']}</td><td>{profile['distinct_count']}</td>"
        f"<td>{html.escape(str(profile['min']))}</td>"
        f"<td>{html.escape(str(profile['max']))}</td></tr>"
        for table_name, table in sorted(report["dataset"]["tables"].items())
        for column, profile in sorted(table["profiles"].items())
    )
    verdict = html.escape(report["verdict"])
    css = """
    :root{color-scheme:light dark;--bg:#f6f7f9;--fg:#17202a;--muted:#5d6d7e;--card:#fff;
    --line:#d5dbe1;--err:#b42318;--warn:#8a6100;--ok:#177245;--accent:#245a8d}
    @media(prefers-color-scheme:dark){:root{--bg:#11161b;--fg:#e8edf1;--muted:#aab8c4;
    --card:#192129;--line:#33414d;--err:#ff8a80;--warn:#ffd166;--ok:#7bd6a7;--accent:#8cc8ff}}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.6 "Segoe UI",system-ui,sans-serif}main{max-width:1180px;margin:0 auto;padding:28px}
    h1,h2,h3{line-height:1.25}.meta{color:var(--muted);overflow-wrap:anywhere}
    .badge{display:inline-block;padding:.15rem .55rem;border-radius:999px;background:var(--line);
    font-size:.75rem;font-weight:700;text-transform:uppercase}.badge.fail{color:var(--err)}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:22px 0}
    .card,section,.finding{background:var(--card);border:1px solid var(--line);border-radius:10px}
    .card{padding:14px}.card b{display:block;font-size:1.7rem}.card span,.meta,.fix{color:var(--muted)}
    section{padding:18px;margin:18px 0;overflow:auto}.finding{display:flex;gap:14px;padding:14px;
    margin:10px 0;border-left:5px solid var(--line)}.finding.error{border-left-color:var(--err)}
    .finding.warning{border-left-color:var(--warn)}.finding h3{margin:0 0 4px}.finding p{margin:4px 0}
    .ok{color:var(--ok);font-weight:600}table{border-collapse:collapse;width:100%;min-width:620px}
    th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-size:.82rem;text-transform:uppercase}code,pre{font-family:ui-monospace,monospace}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:10px;background:var(--bg);border-radius:8px}
    a{color:var(--accent)}footer{color:var(--muted);margin-top:24px;font-size:.9rem}
    """
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>维模体检台 · {html.escape(report['contract']['name'])}</title>
<style>{css}</style></head><body><main>
<h1>维模体检台报告 <span class="badge {verdict}">{verdict}</span></h1>
<p class="meta">Run ID <code>{html.escape(report['run_id'])}</code> · 契约
<code>{html.escape(report['contract']['file'])}</code> · 生成于 {html.escape(report['generated_at'])}</p>
<p>{html.escape(report['conclusion'])}</p>
<div class="cards">
<div class="card"><span>维度 / 事实</span><b>{summary['dimensions']} / {summary['facts']}</b></div>
<div class="card"><span>输入行数</span><b>{summary['rows']}</b></div>
<div class="card"><span>error</span><b>{summary['errors']}</b></div>
<div class="card"><span>warning</span><b>{summary['warnings']}</b></div>
<div class="card"><span>info</span><b>{summary['infos']}</b></div>
<div class="card"><span>退出码</span><b>{report['exit_code']}</b></div></div>
<section><h2>发现项</h2>{_html_findings(report['findings'])}</section>
<section><h2>聚合安全矩阵</h2><table><thead><tr><th>事实.度量</th><th>聚合</th>
<th>可加性</th><th>可安全求和</th><th>可用聚合</th><th>原因</th></tr></thead>
<tbody>{measure_rows}</tbody></table></section>
<section><h2>总线矩阵</h2><table><thead><tr><th>事实</th><th>引用维度</th>
<th>角色</th></tr></thead><tbody>{bus_rows}</tbody></table></section>
<section><h2>数据画像</h2><table><thead><tr><th>表</th><th>列</th><th>类型</th>
<th>空值</th><th>基数</th><th>最小</th><th>最大</th></tr></thead>
<tbody>{profile_rows}</tbody></table></section>
<footer>本报告由 dimcheck 离线生成，HTML 自包含，无外部脚本、字体或网络资源。源数据只读。</footer>
</main></body></html>
"""


def build_run_record(report: dict[str, Any], run_meta: dict[str, Any],
                     publication_id: str | None = None,
                     report_digest: str | None = None) -> dict[str, Any]:
    """Metadata rendered into run.json.

    Wall-clock timing and the publication id live only here (and in the ledger
    /publication manifest); they never enter the deterministic report bodies.
    """
    record = {
        "run_id": report["run_id"], "tool": "dimcheck", "version": __version__,
        "started_at": run_meta["started_at"], "duration_ms": run_meta["duration_ms"],
        "exit_code": report["exit_code"], "verdict": report["verdict"],
        "contract_fingerprint": report["contract"]["fingerprint"],
        "artifacts": {"json": "report.json", "markdown": "report.md",
                      "html": "report.html"},
    }
    if publication_id is not None:
        record["publication_id"] = publication_id
    if report_digest is not None:
        record["report_digest"] = report_digest
    return record


def render_payloads(report: dict[str, Any], run_meta: dict[str, Any],
                    publication_id: str | None = None,
                    report_digest: str | None = None) -> dict[str, str]:
    """Render the four publication artifacts as strings without touching disk.

    The JSON/Markdown/HTML payloads depend only on deterministic inputs; the run
    payload is the only artifact allowed to carry timing/publication metadata.
    """
    return {
        "json": pretty_json(report) + "\n",
        "markdown": render_markdown(report) + "\n",
        "html": render_html(report),
        "run": pretty_json(build_run_record(
            report, run_meta, publication_id, report_digest)) + "\n",
    }


def write_reports(out: Path, report: dict[str, Any], run_meta: dict[str, Any]) -> dict[str, Path]:
    """Compatibility writer used outside the publication protocol.

    The CLI publishes immutable units via :mod:`dimcheck.publishing`; this
    helper remains for callers that only need the four flat files.
    """
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out / "report.json",
        "markdown": out / "report.md",
        "html": out / "report.html",
        "run": out / "run.json",
    }
    payloads = render_payloads(report, run_meta)
    write_text(paths["json"], payloads["json"])
    write_text(paths["markdown"], payloads["markdown"])
    write_text(paths["html"], payloads["html"])
    write_text(paths["run"], payloads["run"])
    return paths
