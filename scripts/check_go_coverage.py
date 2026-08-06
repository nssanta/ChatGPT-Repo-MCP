#!/usr/bin/env python3
"""Read `go tool cover -func` output and enforce a total coverage threshold."""

from __future__ import annotations

import argparse
import re
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum", type=float, required=True)
    args = parser.parse_args()
    text = sys.stdin.read()
    match = re.search(r"^total:\s+\(statements\)\s+([0-9.]+)%$", text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find total Go coverage")
    coverage = float(match.group(1))
    print(text, end="")
    if coverage < args.minimum:
        raise SystemExit(f"Go coverage {coverage:.1f}% is below required {args.minimum:.1f}%")


if __name__ == "__main__":
    main()
