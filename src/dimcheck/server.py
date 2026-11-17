"""Read-only localhost review console for the latest report and ledger."""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .ledger import read_isolated_publications, read_ledger, read_publications
from .pubprotocol import CURRENT_LINK, verify_unit
from .util import pretty_json

STATIC = {
    "/report.json": ("application/json; charset=utf-8", "report.json"),
    "/report.md": ("text/markdown; charset=utf-8", "report.md"),
    "/report.html": ("text/html; charset=utf-8", "report.html"),
    "/run.json": ("application/json; charset=utf-8", "run.json"),
}
SECURITY = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _publications_payload(out: Path) -> dict:
    """Publication registry plus a fresh byte-level check of the current view."""
    publications = read_publications(out)
    isolated = read_isolated_publications(out)
    current = out / CURRENT_LINK
    current_id = None
    current_verified = False
    if current.is_symlink():
        try:
            current_id = current.readlink().name
            current_verified = verify_unit(current)[0]
        except OSError:
            current_verified = False
    return {"current_publication_id": current_id,
            "current_view_verified": current_verified,
            "publications": publications,
            "isolated": isolated}


def _index(out: Path) -> bytes:
    report_path = out / "report.json"
    report = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    try:
        ledger = read_ledger(out)
    except Exception:
        ledger = {"runs": [], "latest_findings": []}
    try:
        publications = _publications_payload(out)
    except Exception:
        publications = {"current_publication_id": None,
                        "current_view_verified": False,
                        "publications": [], "isolated": []}
    current_id = publications.get("current_publication_id")
    if report and current_id:
        summary = report["summary"]
        headline = (
            f"<h1>维模体检台 <span class='pill {html.escape(report['verdict'])}'>"
            f"{html.escape(report['verdict'])}</span></h1>"
            f"<p>契约 <code>{html.escape(report['contract']['name'])}</code> · "
            f"Run ID <code>{html.escape(report['run_id'])}</code> · "
            f"发布单元 <code>{html.escape(str(current_id))}</code> · "
            f"摘要校验 {'通过' if publications['current_view_verified'] else '失败'} · "
            f"error {summary['errors']} / warning {summary['warnings']} / info {summary['infos']}</p>"
            "<p><a href='/report.html'>HTML 报告</a> · <a href='/report.md'>Markdown</a> · "
            "<a href='/report.json'>JSON</a> · <a href='/ledger.json'>台账 JSON</a> · "
            "<a href='/publications.json'>发布清单</a></p>"
        )
    elif report:
        summary = report["summary"]
        headline = (
            f"<h1>维模体检台 <span class='pill {html.escape(report['verdict'])}'>"
            f"{html.escape(report['verdict'])}</span></h1>"
            f"<p>契约 <code>{html.escape(report['contract']['name'])}</code> · "
            f"Run ID <code>{html.escape(report['run_id'])}</code> · "
            f"error {summary['errors']} / warning {summary['warnings']} / info {summary['infos']}</p>"
            "<p><a href='/report.html'>HTML 报告</a> · <a href='/report.md'>Markdown</a> · "
            "<a href='/report.json'>JSON</a> · <a href='/ledger.json'>台账 JSON</a> · "
            "<a href='/publications.json'>发布清单</a></p>"
        )
    else:
        headline = ("<h1>维模体检台</h1><p>尚无 report.json。请先运行 "
                    "<code>python -m dimcheck run</code>。</p>"
                    "<p><a href='/ledger.json'>台账 JSON</a> · "
                    "<a href='/publications.json'>发布清单</a></p>")
    rows = "".join(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td>"
        "<td>{}</td><td>{}</td></tr>".format(
            int(run["pk"]), html.escape(str(run["run_id"])), html.escape(str(run["verdict"])),
            int(run["errors"]), int(run["warnings"]), int(run["duration_ms"]),
        )
        for run in ledger.get("runs", [])
    )
    body = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>维模体检台</title><style>"
        ":root{color-scheme:light dark;--bg:#f6f7f9;--fg:#17202a;--card:#fff;--line:#d5dbe1;--err:#b42318;--warn:#8a6100;--ok:#177245}"
        "@media(prefers-color-scheme:dark){:root{--bg:#11161b;--fg:#e8edf1;--card:#192129;--line:#33414d;--err:#ff8a80;--warn:#ffd166;--ok:#7bd6a7}}"
        "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 'Segoe UI',system-ui,sans-serif}"
        "main{max-width:1000px;margin:auto;padding:28px}section{background:var(--card);border:1px solid var(--line);"
        "border-radius:10px;padding:18px;margin:18px 0;overflow:auto}table{border-collapse:collapse;width:100%}"
        "th,td{text-align:left;padding:7px;border-bottom:1px solid var(--line)}"
        ".pill{padding:.15rem .55rem;border-radius:999px;background:var(--line);font-size:.75rem}"
        ".fail{color:var(--err)}.warn{color:var(--warn)}.pass{color:var(--ok)}a{color:#2767a8}"
        "</style></head><body><main>" + headline +
        "<section><h2>最近运行台账</h2><table><thead><tr><th>#</th><th>Run ID</th>"
        "<th>结论</th><th>error</th><th>warning</th><th>耗时 ms</th></tr></thead><tbody>"
        + (rows or "<tr><td colspan='6'>暂无记录</td></tr>") +
        "</tbody></table></section><p>只读复核台，仅监听 127.0.0.1；不提供写入接口。</p>"
        "</main></body></html>"
    )
    return body.encode("utf-8")


def make_server(out_dir: str | Path, port: int = 8765) -> ThreadingHTTPServer:
    out = Path(out_dir).resolve()
    if not 0 <= int(port) <= 65535:
        raise ValueError("端口必须在 0..65535 之间")

    class Handler(BaseHTTPRequestHandler):
        server_version = "dimcheck-review/0.1"

        def log_message(self, *_args):
            return

        def _respond(self, status: int, content_type: str, payload: bytes,
                     send_body: bool = True) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in SECURITY.items():
                self.send_header(key, value)
            self.end_headers()
            if send_body:
                self.wfile.write(payload)

        def _get(self, send_body: bool) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                self._respond(200, "text/html; charset=utf-8", _index(out), send_body)
                return
            if path == "/ledger.json":
                try:
                    payload = pretty_json(read_ledger(out)).encode("utf-8")
                except Exception:
                    self._respond(500, "application/json; charset=utf-8",
                                  b'{"error":"ledger_unreadable"}', send_body)
                    return
                self._respond(200, "application/json; charset=utf-8", payload, send_body)
                return
            if path == "/publications.json":
                try:
                    payload = pretty_json(_publications_payload(out)).encode("utf-8")
                except Exception:
                    self._respond(500, "application/json; charset=utf-8",
                                  b'{"error":"publications_unreadable"}', send_body)
                    return
                self._respond(200, "application/json; charset=utf-8", payload, send_body)
                return
            route = STATIC.get(path)
            if route is None or ".." in path:
                self._respond(404, "text/plain; charset=utf-8", b"not found\n", send_body)
                return
            content_type, filename = route
            # Never serve a view that fails byte-level verification (e.g. a
            # corrupted unit whose quarantine is pending a writer-side recovery).
            current = out / CURRENT_LINK
            if not current.exists() or not verify_unit(current)[0]:
                self._respond(404, "text/plain; charset=utf-8",
                              b"current view unavailable\n", send_body)
                return
            try:
                payload = (out / filename).read_bytes()
            except OSError:
                self._respond(404, "text/plain; charset=utf-8", b"not found\n", send_body)
                return
            self._respond(200, content_type, payload, send_body)

        def do_GET(self):
            self._get(True)

        def do_HEAD(self):
            self._get(False)

        def do_POST(self):
            self._respond(405, "text/plain; charset=utf-8", b"method not allowed\n")

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    server.daemon_threads = True
    return server
