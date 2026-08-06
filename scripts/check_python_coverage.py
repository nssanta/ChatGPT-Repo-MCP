#!/usr/bin/env python3
"""Enforce overall and safety-critical Python coverage thresholds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CRITICAL_THRESHOLDS = {
    "python/src/chatrepo_mcp/security.py": 90.0,
    "python/src/chatrepo_mcp/edit_tools.py": 90.0,
    "python/src/chatrepo_mcp/command_tools.py": 90.0,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--overall", type=float, default=80.0)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    overall = float(report["totals"]["percent_covered"])
    failures: list[str] = []
    if overall < args.overall:
        failures.append(f"overall {overall:.1f}% < {args.overall:.1f}%")
    files = report["files"]
    for path, minimum in CRITICAL_THRESHOLDS.items():
        if path not in files:
            failures.append(f"missing critical coverage entry: {path}")
            continue
        covered = float(files[path]["summary"]["percent_covered"])
        if covered < minimum:
            failures.append(f"{path} {covered:.1f}% < {minimum:.1f}%")
    if failures:
        raise SystemExit("coverage gate failed: " + "; ".join(failures))
    print(f"python coverage ok: overall={overall:.1f}%, critical>=90.0%")


if __name__ == "__main__":
    main()
