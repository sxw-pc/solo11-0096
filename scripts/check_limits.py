#!/usr/bin/env python3
"""Check total Python line budget and per-file line limit."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

IGNORED = {".git", ".venv", "venv", "out", "__pycache__", ".pytest_cache",
           "build", "dist", ".mypy_cache", ".ruff_cache"}


def python_files(root: Path) -> list[Path]:
    result = []
    for path in root.rglob("*.py"):
        if any(part in IGNORED for part in path.parts):
            continue
        result.append(path)
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="dimcheck 行数预算自检")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--min", type=int, default=2500, dest="minimum")
    parser.add_argument("--max", type=int, default=5200, dest="maximum")
    parser.add_argument("--file-max", type=int, default=300)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = python_files(root)
    counts = {
        path.relative_to(root).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in files
    }
    total = sum(counts.values())
    largest = max(counts.items(), key=lambda item: (item[1], item[0]), default=("", 0))
    errors = []
    if total < args.minimum:
        errors.append(f"total {total} < minimum {args.minimum}")
    if total > args.maximum:
        errors.append(f"total {total} > maximum {args.maximum}")
    over = [(name, lines) for name, lines in counts.items() if lines > args.file_max]
    for name, lines in over:
        errors.append(f"{name}: {lines} > per-file limit {args.file_max}")
    print(f"files={len(counts)} total={total} range={args.minimum}..{args.maximum} "
          f"largest={largest[0]}:{largest[1]} file_limit={args.file_max}")
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
