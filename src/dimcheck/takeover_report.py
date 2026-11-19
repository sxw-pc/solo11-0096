"""Deterministic JSON, Markdown, and self-contained HTML takeover reports."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .util import epoch_iso, pretty_json, write_text


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_md(cell) for cell in row) + " |" for row in rows)
    return lines


def render_markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        "# 站线归属接管计划", "",
        f"- 计划：`{plan['plan_id']}`", f"- 状态：**{plan['state']}**",
        f"- 策略：`{plan['policy']}`",
        f"- 契约指纹：`{plan['contract']['fingerprint']}`",
        f"- 转线事件：`{plan['events']['sha256']}`（{plan['events']['count']} 条）",
        f"- 生成时间（SOURCE_DATE_EPOCH）：{epoch_iso(plan['generated_epoch'])}", "",
        "## 摘要", "",
    ]
    lines.extend(_table(["项目", "值"], [
        ["分区", summary["partitions"]],
        ["整份计划被阻断", "是" if plan.get("blocked") else "否"],
        ["接管新维表分区", summary["adopted_new_partitions"]],
        ["保留旧维表分区", summary["retained_old_partitions"]],
        ["客流行数", summary["rows"]],
        ["受影响客流行数", summary["affected_rows"]],
        ["新起点客流", summary["adopted_new_rows"]],
        ["旧起点客流", summary["retained_old_rows"]],
        ["已核实行", summary["verified_rows"]],
        ["待核行", summary["pending_rows"]],
    ]))
    lines.extend(["", "## 参与的交付", ""])
    receipt_rows = []
    for side in ("old", "new"):
        receipt = plan["receipts"][side]
        for item in receipt.get("files", []):
            receipt_rows.append([side, receipt["batch_id"], receipt.get("producer"),
                                 item["dimension"], item["sha256"][:16],
                                 item["rows"]])
    lines.extend(_table(["起点", "批次", "生产方", "维度", "摘要", "行数"], receipt_rows))
    lines.extend(["", "## 分区结论", ""])
    lines.extend(_table(
        ["服务日期", "日期", "结论", "原因", "行数", "新起点", "旧起点", "受影响",
         "待核"],
        [[item["service_date_key"], item["calendar_date"], item["conclusion"],
          item["reason"], item["rows"], item["adopted_new_rows"],
          item["retained_old_rows"], item["affected_rows"], item["pending_rows"]]
         for item in plan["partitions"]]))
    lines.extend(["", "## 车站与线路", ""])
    station_rows = []
    for item in plan["partitions"]:
        for station in item["stations"]:
            station_rows.append([item["service_date_key"], station["old_station_code"],
                                 station["old_line_code"], station["new_station_code"],
                                 station["expected_line_code"],
                                 station["new_line_code"],
                                 station["verified_rows"], station["rows"]])
    lines.extend(_table(["服务日期", "旧站码", "旧线路", "新站码", "事件线路",
                         "新线路", "已核实行", "行数"], station_rows))
    lines.extend(["", "## 转线事件", ""])
    event_rows = []
    for item in plan["partitions"]:
        for event in item["transfers"]:
            event_rows.append([item["service_date_key"], event["station_code"],
                               event["from_line_code"], event["to_line_code"],
                               event["effective_at"]])
    lines.extend(_table(["服务日期", "车站", "原线路", "新线路", "UTC 生效"],
                        event_rows))
    lines.extend(["", "## 逐行待核证据", ""])
    evidence = []
    for item in plan["partitions"]:
        for row in item["pending_evidence"]:
            evidence.append([item["service_date_key"], row["event_id"],
                             row["reason"], row["station_key"],
                             row["old_station_code"], row["expected_line_code"],
                             row["new_station_code"], row["new_line_code"]])
    if evidence:
        lines.extend(_table(["服务日期", "事件", "原因", "事实站键", "旧站码",
                             "事件线路", "新站码", "新线路"], evidence))
    else:
        lines.append("无待核客流行。")
    if plan["findings"]:
        lines.extend(["", "## 阻断发现项", ""])
        lines.extend(_table(["代码", "对象", "说明"],
                            [[item["code"], item["object_name"], item["message"]]
                             for item in plan["findings"]]))
    lines.extend(["", "---", "本计划由 dimcheck 离线生成；原始维表与已登记收据均未被改写。", ""])
    return "\n".join(lines)


def render_html(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    receipt_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in (
            [side, plan["receipts"][side]["batch_id"],
             item["dimension"], item["sha256"], item["rows"]]
            for side in ("old", "new") for item in plan["receipts"][side].get("files", [])))
    partition_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in (
            [item["service_date_key"], item["calendar_date"], item["conclusion"],
             item["reason"], item["rows"], item["adopted_new_rows"],
             item["retained_old_rows"], item["affected_rows"], item["pending_rows"]]
            for item in plan["partitions"]))
    station_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in (
            [item["service_date_key"], station["old_station_code"],
             station["old_line_code"], station["new_station_code"],
             station["expected_line_code"], station["new_line_code"],
             station["verified_rows"], station["rows"]]
            for item in plan["partitions"] for station in item["stations"]))
    evidence_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in (
            [item["service_date_key"], row["event_id"], row["reason"],
             row["station_key"], row["old_station_code"],
             row["expected_line_code"], row["new_station_code"], row["new_line_code"]]
            for item in plan["partitions"] for row in item["pending_evidence"]))
    finding_rows = "".join(
        f"<li><code>{html.escape(item['code'])}</code> "
        f"{html.escape(item['object_name'])}：{html.escape(item['message'])}</li>"
        for item in plan["findings"])
    css = """
    :root{color-scheme:light dark;--bg:#f6f7f9;--fg:#17202a;--muted:#5d6d7e;--card:#fff;
    --line:#d5dbe1;--ok:#177245;--warn:#8a6100;--err:#b42318}
    @media(prefers-color-scheme:dark){:root{--bg:#11161b;--fg:#e8edf1;--muted:#aab8c4;
    --card:#192129;--line:#33414d;--ok:#7bd6a7;--warn:#ffd166;--err:#ff8a80}}
    body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif}
    main{max-width:1180px;margin:0 auto;padding:28px}h1,h2{line-height:1.25}
    .meta{color:var(--muted);overflow-wrap:anywhere}section{background:var(--card);
    border:1px solid var(--line);border-radius:10px;padding:18px;margin:18px 0;overflow:auto}
    table{border-collapse:collapse;width:100%;min-width:620px}th,td{text-align:left;
    padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-size:.82rem;text-transform:uppercase}
    code,pre{font-family:ui-monospace,monospace}.ok{color:var(--ok);font-weight:600}
    .pending{color:var(--warn);font-weight:600}
    """
    state_class = "ok" if plan["state"] == "confirmed" else "pending"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>站线归属接管计划 · {html.escape(plan['plan_id'])}</title>
<style>{css}</style></head><body><main>
<h1>站线归属接管计划</h1>
<p class="meta">计划 <code>{html.escape(plan['plan_id'])}</code> · 策略
<code>{html.escape(plan['policy'])}</code> · 状态
<span class="{state_class}">{html.escape(plan['state'])}</span> · 生成于
{html.escape(epoch_iso(plan['generated_epoch']))}</p>
<section><h2>摘要</h2><table><tbody>
<tr><th>分区</th><td>{summary['partitions']}</td></tr>
<tr><th>接管新维表分区</th><td>{summary['adopted_new_partitions']}</td></tr>
<tr><th>保留旧维表分区</th><td>{summary['retained_old_partitions']}</td></tr>
<tr><th>客流行数</th><td>{summary['rows']}</td></tr>
<tr><th>受影响客流行数</th><td>{summary['affected_rows']}</td></tr>
<tr><th>新起点客流</th><td>{summary['adopted_new_rows']}</td></tr>
<tr><th>旧起点客流</th><td>{summary['retained_old_rows']}</td></tr>
<tr><th>已核实行</th><td>{summary['verified_rows']}</td></tr>
<tr><th>待核行</th><td>{summary['pending_rows']}</td></tr>
</tbody></table></section>
<section><h2>参与的交付</h2><table><thead><tr><th>起点</th><th>批次</th>
<th>维度</th><th>摘要</th><th>行数</th></tr></thead><tbody>{receipt_rows}</tbody></table></section>
<section><h2>分区结论</h2><table><thead><tr><th>服务日期</th><th>日期</th><th>结论</th>
<th>原因</th><th>行数</th><th>新起点</th><th>旧起点</th><th>受影响</th><th>待核</th>
</tr></thead><tbody>{partition_rows}</tbody></table></section>
<section><h2>车站与线路</h2><table><thead><tr><th>服务日期</th><th>旧站码</th>
<th>旧线路</th><th>新站码</th><th>事件线路</th><th>新线路</th><th>已核实行</th>
<th>行数</th></tr></thead><tbody>{station_rows}</tbody></table></section>
<section><h2>逐行待核证据</h2><table><thead><tr><th>服务日期</th><th>事件</th>
<th>原因</th><th>事实站键</th><th>旧站码</th><th>事件线路</th><th>新站码</th>
<th>新线路</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>
<section><h2>阻断发现项</h2><ul>{finding_rows or '<li>无</li>'}</ul></section>
<footer><p class="meta">本报告由 dimcheck 离线生成，无脚本与外部资源。原始维表与收据只读。</p></footer>
</main></body></html>
"""


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Report view: never expose absolute local paths.

    Re-verification needs the stored event path and receipt roots, so the
    ledger keeps them; the archived report only carries content digests, row
    counts, and the event count.
    """
    receipts = {
        side: {key: value for key, value in plan["receipts"][side].items()
               if key != "source_root"}
        for side in plan["receipts"]}
    return {key: value for key, value in plan.items()
            if key != "events_path"} | {"receipts": receipts}


def write_plan_reports(out: str | Path, plan: dict[str, Any]) -> dict[str, Path]:
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {"json": directory / "takeover.json",
             "markdown": directory / "takeover.md",
             "html": directory / "takeover.html"}
    write_text(paths["json"], pretty_json(public_plan(plan)) + "\n")
    write_text(paths["markdown"], render_markdown(plan) + "\n")
    write_text(paths["html"], render_html(plan))
    return paths


def write_verification(out: str | Path, verification: dict[str, Any]) -> Path:
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "takeover-verify.json"
    payload = {key: value for key, value in verification.items()
               if key != "recomputed"}
    payload["recomputed_summary"] = verification.get("recomputed", {}).get("summary")
    write_text(path, pretty_json(payload) + "\n")
    return path
