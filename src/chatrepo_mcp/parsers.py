from __future__ import annotations

import re
from typing import Any


VITEST_FILES_RE = re.compile(r"Test Files\s+(.+)")
VITEST_TESTS_RE = re.compile(r"Tests\s+(.+)")
COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|skipped)", re.IGNORECASE)
TS_DIAG_RE = re.compile(r"^(?P<path>[^()\n]+)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<message>.+)$")


def _counts(fragment: str) -> dict[str, int]:
    result = {"failed": 0, "passed": 0, "skipped": 0}
    for count, label in COUNT_RE.findall(fragment):
        result[label.lower()] = int(count)
    return result


def parse_vitest_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    files = {"failed": 0, "passed": 0, "skipped": 0}
    tests = {"failed": 0, "passed": 0, "skipped": 0}
    for line in text.splitlines():
        file_match = VITEST_FILES_RE.search(line)
        if file_match:
            files.update(_counts(file_match.group(1)))
        test_match = VITEST_TESTS_RE.search(line)
        if test_match:
            tests.update(_counts(test_match.group(1)))

    failures = []
    current_file: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAIL ") or stripped.startswith("❯ "):
            parts = stripped.split()
            if len(parts) >= 2 and (parts[0] == "FAIL" or parts[0] == "❯"):
                current_file = parts[1]
        if ("AssertionError:" in stripped or "Error:" in stripped) and len(failures) < 20:
            failures.append({"file": current_file, "message": stripped[:500]})

    summary_parts = []
    if files["failed"] or files["passed"] or files["skipped"]:
        summary_parts.append(
            f"{files['failed']} failed files, {files['passed']} passed files, {files['skipped']} skipped files"
        )
    if tests["failed"] or tests["passed"] or tests["skipped"]:
        summary_parts.append(f"{tests['failed']} failed tests, {tests['passed']} passed tests, {tests['skipped']} skipped tests")
    return {
        "kind": "vitest",
        "summary": "; ".join(summary_parts) if summary_parts else "no vitest summary found",
        "test_files": files,
        "tests": tests,
        "failures": failures,
    }


def parse_tsc_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    diagnostics = []
    for line in text.splitlines():
        match = TS_DIAG_RE.match(line.strip())
        if match:
            item = match.groupdict()
            item["line"] = int(item["line"])
            item["column"] = int(item["column"])
            diagnostics.append(item)
    return {
        "kind": "tsc",
        "summary": "clean" if not diagnostics else f"{len(diagnostics)} TypeScript errors",
        "error_count": len(diagnostics),
        "diagnostics": diagnostics[:50],
    }


def parse_git_diff_check(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}".strip()
    lines = [line for line in text.splitlines() if line.strip()]
    return {
        "kind": "git_diff_check",
        "summary": "clean" if not lines else f"{len(lines)} whitespace/diff check issues",
        "issues": lines,
    }


def infer_parse_kind(command: str, preset_parser: str | None = None) -> str:
    if preset_parser and preset_parser != "auto":
        return preset_parser
    if "vitest" in command or "npm run test" in command or "npm run test:fast" in command:
        return "vitest"
    if "tsc" in command or "npm run build" in command or "npm run typecheck" in command:
        return "tsc"
    if command.strip() == "git diff --check":
        return "git_diff_check"
    return "none"


def parse_command_output(command: str, stdout: str, stderr: str = "", parse_kind: str | None = "auto") -> dict[str, Any] | None:
    kind = infer_parse_kind(command) if parse_kind in {None, "auto"} else parse_kind
    if kind == "vitest":
        return parse_vitest_output(stdout, stderr)
    if kind == "tsc":
        return parse_tsc_output(stdout, stderr)
    if kind == "git_diff_check":
        return parse_git_diff_check(stdout, stderr)
    return None
