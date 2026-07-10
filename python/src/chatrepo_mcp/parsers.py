from __future__ import annotations

import re
import shlex
from typing import Any, Callable


VITEST_FILES_RE = re.compile(r"Test Files\s+(.+)")
VITEST_TESTS_RE = re.compile(r"Tests\s+(.+)")
COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|skipped)", re.IGNORECASE)
TS_DIAG_RE = re.compile(r"^(?P<path>[^()\n]+)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<message>.+)$")

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


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

    failures: list[dict[str, str | None]] = []
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


# --- pytest ---------------------------------------------------------------

_PYTEST_RESULT_LABEL = r"(?:passed|failed|skipped|error|errors|warning|warnings)"
# Matches both the verbose/bordered form (`===== 2 failed, 3 passed in 0.42s =====`)
# and pytest's quiet-mode (`-q`) form, which omits the `=` border entirely
# (`1 passed in 0.01s`).
_PYTEST_SUMMARY_RE = re.compile(
    r"^(?:=+\s)?(?P<body>\d+\s+"
    + _PYTEST_RESULT_LABEL
    + r"(?:,\s*\d+\s+"
    + _PYTEST_RESULT_LABEL
    + r")*)\sin\s(?P<seconds>[\d.]+)s(?:\s=+)?\s*$",
    re.MULTILINE,
)
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<message>.+))?$", re.MULTILINE)
_PYTEST_ASSERT_RE = re.compile(r"^E\s+(?P<text>.+)$", re.MULTILINE)


def parse_pytest_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"

    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "warning": 0}
    summary = "no pytest summary found"
    summary_match = _PYTEST_SUMMARY_RE.search(text)
    if summary_match:
        body = summary_match.group("body")
        summary = f"{body} in {summary_match.group('seconds')}s"
        for count, label in re.findall(r"(\d+)\s+(passed|failed|skipped|error|errors|warning|warnings)", body, re.IGNORECASE):
            key = label.lower().rstrip("s") if label.lower() not in {"passed", "failed", "skipped"} else label.lower()
            if key in counts:
                counts[key] = int(count)

    failures = []
    for match in _PYTEST_FAILED_RE.finditer(text):
        failures.append({"nodeid": match.group("nodeid"), "message": (match.group("message") or "").strip()[:500]})
        if len(failures) >= 20:
            break

    assertions = [match.group("text").strip()[:500] for match in _PYTEST_ASSERT_RE.finditer(text)][:20]

    return {
        "kind": "pytest",
        "summary": summary,
        "counts": counts,
        "failures": failures,
        "assertions": assertions,
    }


# --- go test / go vet / go build ------------------------------------------

_GOTEST_FAIL_RE = re.compile(r"^--- FAIL: (?P<name>\S+)(?:\s+\(([\d.]+)s\))?", re.MULTILINE)
_GOTEST_PASS_RE = re.compile(r"^--- PASS: (?P<name>\S+)(?:\s+\(([\d.]+)s\))?", re.MULTILINE)
_GO_PKG_OK_RE = re.compile(r"^ok\s+(?P<pkg>\S+)\s+([\d.]+)s", re.MULTILINE)
_GO_PKG_FAIL_RE = re.compile(r"^FAIL\s+(?P<pkg>\S+)(?:\s+\[[^\]]+\])?", re.MULTILINE)
_GO_COMPILE_ERR_RE = re.compile(r"^(?P<path>[^\s:]+\.go):(?P<line>\d+):(?P<column>\d+):\s+(?P<message>.+)$", re.MULTILINE)


def parse_go_test_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"

    failed_tests = [match.group("name") for match in _GOTEST_FAIL_RE.finditer(text)]
    passed_tests = [match.group("name") for match in _GOTEST_PASS_RE.finditer(text)]
    ok_packages = [match.group("pkg") for match in _GO_PKG_OK_RE.finditer(text)]
    failed_packages = [match.group("pkg") for match in _GO_PKG_FAIL_RE.finditer(text)]
    compile_errors = []
    for match in _GO_COMPILE_ERR_RE.finditer(text):
        item = match.groupdict()
        item["line"] = int(item["line"])
        item["column"] = int(item["column"])
        compile_errors.append(item)

    counts = {"passed": len(passed_tests), "failed": len(failed_tests)}
    parts = []
    if failed_tests or passed_tests:
        parts.append(f"{counts['failed']} failed tests, {counts['passed']} passed tests")
    if ok_packages or failed_packages:
        parts.append(f"{len(ok_packages)} ok packages, {len(failed_packages)} failed packages")
    if compile_errors:
        parts.append(f"{len(compile_errors)} compile errors")
    summary = "; ".join(parts) if parts else ("clean" if not compile_errors else f"{len(compile_errors)} compile errors")

    return {
        "kind": "gotest",
        "summary": summary,
        "counts": counts,
        "failures": [{"test": name} for name in failed_tests[:50]],
        "packages": {"ok": ok_packages, "failed": failed_packages},
        "compile_errors": compile_errors[:50],
    }


def parse_gobuild_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    diagnostics = []
    for match in _GO_COMPILE_ERR_RE.finditer(text):
        item = match.groupdict()
        item["line"] = int(item["line"])
        item["column"] = int(item["column"])
        diagnostics.append(item)
    return {
        "kind": "gobuild",
        "summary": "clean" if not diagnostics else f"{len(diagnostics)} issues",
        "error_count": len(diagnostics),
        "diagnostics": diagnostics[:50],
    }


# --- ruff -------------------------------------------------------------------

_RUFF_LINE_RE = re.compile(r"^(?P<path>[^\s:]+):(?P<line>\d+):(?P<column>\d+):\s+(?P<code>[A-Z]+\d+)\s+(?P<message>.+)$", re.MULTILINE)
_RUFF_SUMMARY_RE = re.compile(r"Found (?P<count>\d+) errors?", re.IGNORECASE)


def parse_ruff_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    diagnostics = []
    for match in _RUFF_LINE_RE.finditer(text):
        item = match.groupdict()
        item["line"] = int(item["line"])
        item["column"] = int(item["column"])
        diagnostics.append(item)

    summary_match = _RUFF_SUMMARY_RE.search(text)
    error_count = int(summary_match.group("count")) if summary_match else len(diagnostics)
    summary = "clean" if not error_count else f"{error_count} ruff errors"

    return {
        "kind": "ruff",
        "summary": summary,
        "error_count": error_count,
        "diagnostics": diagnostics[:50],
    }


# --- mypy --------------------------------------------------------------------

_MYPY_LINE_RE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s+error:\s+(?P<message>.+?)(?:\s+\[(?P<code>[a-zA-Z0-9\-]+)\])?$",
    re.MULTILINE,
)
_MYPY_SUMMARY_RE = re.compile(r"Found (?P<count>\d+) errors? in (?P<files>\d+) files?", re.IGNORECASE)


def parse_mypy_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    diagnostics = []
    for match in _MYPY_LINE_RE.finditer(text):
        item = match.groupdict()
        item["line"] = int(item["line"])
        item["column"] = int(item["column"]) if item["column"] else None
        diagnostics.append(item)

    summary_match = _MYPY_SUMMARY_RE.search(text)
    error_count = int(summary_match.group("count")) if summary_match else len(diagnostics)
    summary = "clean" if not error_count else f"{error_count} mypy errors"

    return {
        "kind": "mypy",
        "summary": summary,
        "error_count": error_count,
        "diagnostics": diagnostics[:50],
    }


# --- cargo (test + build/clippy) --------------------------------------------

_CARGO_ERR_RE = re.compile(r"^error(?:\[(?P<code>E\d+)\])?:\s+(?P<message>.+)$", re.MULTILINE)
_CARGO_LOC_RE = re.compile(r"^\s*-->\s+(?P<path>[^\s:]+):(?P<line>\d+):(?P<column>\d+)")
_CARGO_TEST_SUMMARY_RE = re.compile(
    r"test result:\s+(?P<status>ok|FAILED)\.\s+(?P<passed>\d+) passed;\s+(?P<failed>\d+) failed;\s+(?P<ignored>\d+) ignored",
)


def parse_cargo_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    lines = text.splitlines()

    errors: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for line in lines:
        err_match = _CARGO_ERR_RE.match(line)
        if err_match:
            pending = {
                "code": err_match.group("code"),
                "message": err_match.group("message").strip()[:500],
                "path": None,
                "line": None,
                "column": None,
            }
            errors.append(pending)
            continue
        if pending is not None and pending["path"] is None:
            loc_match = _CARGO_LOC_RE.match(line)
            if loc_match:
                pending["path"] = loc_match.group("path")
                pending["line"] = int(loc_match.group("line"))
                pending["column"] = int(loc_match.group("column"))

    test_summary_match = _CARGO_TEST_SUMMARY_RE.search(text)
    counts: dict[str, int] = {}
    parts = []
    if test_summary_match:
        counts = {
            "passed": int(test_summary_match.group("passed")),
            "failed": int(test_summary_match.group("failed")),
            "ignored": int(test_summary_match.group("ignored")),
        }
        parts.append(f"{counts['failed']} failed, {counts['passed']} passed, {counts['ignored']} ignored")
    if errors:
        parts.append(f"{len(errors)} compile errors")
    summary = "; ".join(parts) if parts else "clean"

    return {
        "kind": "cargo",
        "summary": summary,
        "counts": counts,
        "errors": errors[:50],
    }


# --- eslint (stylish) --------------------------------------------------------

_ESLINT_LINE_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<column>\d+)\s+(?P<severity>error|warning)\s+(?P<rest>.+)$"
)
_ESLINT_SUMMARY_RE = re.compile(r"[✖x]\s+(?P<count>\d+)\s+problems?\s+\((?P<errors>\d+)\s+errors?,\s+(?P<warnings>\d+)\s+warnings?\)")


def parse_eslint_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    text = f"{stdout}\n{stderr}"
    diagnostics: list[dict[str, Any]] = []
    current_path: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        line_match = _ESLINT_LINE_RE.match(raw_line)
        if line_match:
            rest = line_match.group("rest").strip()
            rule = None
            message = rest
            split = re.split(r"\s{2,}", rest)
            if len(split) >= 2 and re.fullmatch(r"[\w@.\-]+(?:/[\w@.\-]+)?", split[-1]):
                rule = split[-1]
                message = "  ".join(split[:-1]).strip()
            diagnostics.append(
                {
                    "path": current_path,
                    "line": int(line_match.group("line")),
                    "column": int(line_match.group("column")),
                    "severity": line_match.group("severity"),
                    "message": message[:500],
                    "rule": rule,
                }
            )
            continue
        if raw_line[0].isspace():
            continue
        if raw_line.strip().startswith(("✖", "x", "Oops")):
            continue
        current_path = raw_line.strip()

    summary_match = _ESLINT_SUMMARY_RE.search(text)
    if summary_match:
        error_count = int(summary_match.group("errors"))
        warning_count = int(summary_match.group("warnings"))
    else:
        error_count = sum(1 for item in diagnostics if item["severity"] == "error")
        warning_count = sum(1 for item in diagnostics if item["severity"] == "warning")

    summary = "clean" if not (error_count or warning_count) else f"{error_count} errors, {warning_count} warnings"

    return {
        "kind": "eslint",
        "summary": summary,
        "counts": {"errors": error_count, "warnings": warning_count},
        "diagnostics": diagnostics[:50],
    }


PARSERS: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "vitest": parse_vitest_output,
    "tsc": parse_tsc_output,
    "git_diff_check": parse_git_diff_check,
    "pytest": parse_pytest_output,
    "gotest": parse_go_test_output,
    "gobuild": parse_gobuild_output,
    "ruff": parse_ruff_output,
    "mypy": parse_mypy_output,
    "cargo_test": parse_cargo_output,
    "cargo_build": parse_cargo_output,
    "eslint": parse_eslint_output,
}


# --- kind inference -----------------------------------------------------------

def _tokenize(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    return tokens[idx:]


def infer_parse_kind(command: str, preset_parser: str | None = None) -> str:
    """Infer a ``PARSERS`` key from the command's first executable token/subcommand.

    Falls back to ``"auto"`` (rather than failing) for ``make <target>`` and
    any command that doesn't map to a known tool; callers should then try
    :func:`infer_from_output` against the actual stdout/stderr.
    """
    if preset_parser and preset_parser != "auto":
        return preset_parser

    tokens = _tokenize(command)
    if tokens:
        first = tokens[0].rsplit("/", 1)[-1]
        second = tokens[1] if len(tokens) > 1 else ""
        third = tokens[2] if len(tokens) > 2 else ""

        if first == "pytest":
            return "pytest"
        if first in {"python", "python3"} and second == "-m" and third == "pytest":
            return "pytest"
        if first == "ruff":
            return "ruff"
        if first == "mypy":
            return "mypy"
        if first in {"python", "python3"} and second == "-m" and third == "mypy":
            return "mypy"
        if first == "go":
            if second == "test":
                return "gotest"
            if second in {"vet", "build"}:
                return "gobuild"
        if first == "cargo":
            if second == "test":
                return "cargo_test"
            if second in {"build", "clippy"}:
                return "cargo_build"
        if first == "eslint":
            return "eslint"

    if "vitest" in command or "npm run test" in command or "npm run test:fast" in command:
        return "vitest"
    if "eslint" in command or "npm run lint" in command:
        return "eslint"
    if "tsc" in command or "npm run build" in command or "npm run typecheck" in command:
        return "tsc"
    if command.strip() == "git diff --check":
        return "git_diff_check"
    return "auto"


_GOTEST_OUTPUT_RE = re.compile(r"^(=== RUN|--- FAIL:|--- PASS:|ok\s+\S+\s+[\d.]+s|FAIL\s+\S+)", re.MULTILINE)
_PYTEST_OUTPUT_RE = re.compile(r"(=+\s*FAILURES\s*=+|warnings summary)")
_VITEST_OUTPUT_RE = re.compile(r"(Test Files\s+\d+|^\s*[✓✗]\s)", re.MULTILINE)
_CARGO_ERROR_OUTPUT_RE = re.compile(r"error\[E\d+\]")


def infer_from_output(stdout: str, stderr: str = "") -> str | None:
    """Best-effort detection of the tool that produced ``stdout``/``stderr``.

    Used as a fallback when the command itself doesn't identify the tool
    (e.g. ``make test`` delegating to an arbitrary underlying runner).
    Returns ``None`` when nothing recognizable is found.
    """
    text = f"{stdout}\n{stderr}"
    if not text.strip():
        return None

    if _GOTEST_OUTPUT_RE.search(text):
        return "gotest"
    if _PYTEST_OUTPUT_RE.search(text) or _PYTEST_SUMMARY_RE.search(text):
        return "pytest"
    if _VITEST_OUTPUT_RE.search(text):
        return "vitest"
    if "error TS" in text:
        return "tsc"
    if _CARGO_TEST_SUMMARY_RE.search(text):
        return "cargo_test"
    if _CARGO_ERROR_OUTPUT_RE.search(text):
        return "cargo_build"
    if _MYPY_SUMMARY_RE.search(text) or _MYPY_LINE_RE.search(text):
        return "mypy"
    if _RUFF_SUMMARY_RE.search(text) or _RUFF_LINE_RE.search(text):
        return "ruff"
    return None


def parse_command_output(command: str, stdout: str, stderr: str = "", parse_kind: str | None = "auto") -> dict[str, Any] | None:
    kind = infer_parse_kind(command) if parse_kind in {None, "auto"} else parse_kind
    if kind in {None, "auto", "none"}:
        inferred = infer_from_output(stdout, stderr)
        if not inferred:
            return None
        kind = inferred
    if not isinstance(kind, str):
        return None
    parser = PARSERS.get(kind)
    if parser is None:
        return None
    return parser(stdout, stderr)
