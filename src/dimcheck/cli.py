"""Command line interface: demo, run, explain, and serve."""
from __future__ import annotations

import argparse
import runpy
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .conform import check_conform
from .contract import ContractError, load_contract
from .fanout import check_fanout
from .keys import check_keys
from .ledger import append_run
from .measures import check_measures
from .refs import check_refs
from .report import build_report, write_reports
from .server import make_server
from .sources import SourceError, check_declared_schema, load_dataset
from .util import (CODES, PathError, ensure_out_dir, guard_contract_path, repo_root,
                   report_epoch)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"DIM-CLI-001 参数错误: {message}", file=sys.stderr)
        raise SystemExit(1)


def _utc_now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit_finding(item) -> None:
    print(f"{item.code} {item.object_name}: {item.message}", file=sys.stderr)


def run_once(contract_path: str | Path, out: str | Path,
             quiet: bool = False) -> tuple[dict[str, Any], dict[str, Path]]:
    started_at = _utc_now()
    started = time.monotonic()
    contract_file = guard_contract_path(contract_path)
    contract = load_contract(contract_file)
    dataset = load_dataset(contract)
    findings = check_declared_schema(contract, dataset)
    metrics: dict[str, Any] = {}
    for name, checker in (
        ("keys", check_keys), ("refs", check_refs), ("fanout", check_fanout),
        ("measures", check_measures), ("conform", check_conform),
    ):
        found, detail = checker(contract, dataset)
        findings.extend(found)
        metrics[name] = detail
    epoch = report_epoch(contract.run_epoch)
    report = build_report(contract, dataset, findings, metrics, epoch)
    output_dir = ensure_out_dir(out)
    run_meta = {
        "started_at": started_at,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    }
    paths = write_reports(output_dir, report, run_meta)
    append_run(output_dir, report, run_meta)
    if not quiet:
        summary = report["summary"]
        print(f"dimcheck {report['run_id']} verdict={report['verdict']} "
              f"errors={summary['errors']} warnings={summary['warnings']} "
              f"infos={summary['infos']}")
        print(f"report: {paths['html']}")
    return report, paths


def _handle_fatal(exc: Exception) -> int:
    if isinstance(exc, (ContractError, SourceError, PathError)):
        if isinstance(exc, ContractError):
            for item in exc.findings:
                _emit_finding(item)
        elif isinstance(exc, SourceError):
            _emit_finding(exc.finding)
        else:
            print(f"{exc.code} {exc.message}", file=sys.stderr)
        return 1
    if isinstance(exc, OSError):
        print(f"DIM-IO-001 {exc}", file=sys.stderr)
        return 1
    print(f"DIM-INTERNAL-001 {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def _command_run(args) -> int:
    try:
        report, _ = run_once(args.contract, args.out, quiet=args.quiet)
        return report["exit_code"]
    except Exception as exc:  # noqa: BLE001 - graded CLI boundary
        return _handle_fatal(exc)


def _command_demo(args) -> int:
    script = repo_root() / "scripts" / "make_fixtures.py"
    try:
        generate = runpy.run_path(str(script), run_name="dimcheck_fixture_builder")["generate"]
        generate(repo_root() / "fixtures", quiet=args.quiet)
        report, _ = run_once(repo_root() / "contracts" / "model.yaml", args.out,
                             quiet=args.quiet)
        return report["exit_code"]
    except Exception as exc:  # noqa: BLE001 - graded CLI boundary
        return _handle_fatal(exc)


def _command_explain(args) -> int:
    if args.code is None:
        for code in sorted(CODES):
            severity, title, _ = CODES[code]
            print(f"{code} [{severity}] {title}")
        return 0
    code = args.code.upper()
    if code not in CODES:
        print(f"DIM-EXPLAIN-001 未知错误码: {args.code}", file=sys.stderr)
        return 1
    severity, title, fix = CODES[code]
    print(f"{code} [{severity}] {title}")
    print(f"说明: {title}")
    print(f"修复: {fix}")
    print("详见 docs/CODES.md")
    return 0


def _command_serve(args) -> int:
    out = Path(args.out)
    if not (out / "report.json").is_file():
        print(f"DIM-IO-001 未找到报告: {out / 'report.json'}", file=sys.stderr)
        return 1
    try:
        server = make_server(out, args.port)
    except (OSError, ValueError) as exc:
        print(f"DIM-IO-001 无法启动只读复核台: {exc}", file=sys.stderr)
        return 1
    port = server.server_address[1]
    print(f"dimcheck review: http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="dimcheck", description="离线数仓维度模型聚合安全体检工具",
                     allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"dimcheck {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="生成 fixture 并完整运行示例", allow_abbrev=False)
    demo.add_argument("--out", default="out", help="输出目录（默认 ./out）")
    demo.add_argument("--quiet", action="store_true")
    demo.set_defaults(func=_command_demo)
    run = sub.add_parser("run", help="体检指定契约", allow_abbrev=False)
    run.add_argument("--contract", default=str(repo_root() / "contracts" / "model.yaml"))
    run.add_argument("--out", default="out", help="输出目录（默认 ./out）")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=_command_run)
    explain = sub.add_parser("explain", help="解释错误码", allow_abbrev=False)
    explain.add_argument("code", nargs="?", help="错误码；省略时列出全部")
    explain.set_defaults(func=_command_explain)
    serve = sub.add_parser("serve", help="启动只读复核台（仅 127.0.0.1）", allow_abbrev=False)
    serve.add_argument("--out", default="out")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
