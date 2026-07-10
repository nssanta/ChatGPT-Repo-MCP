from chatrepo_mcp.parsers import (
    infer_from_output,
    infer_parse_kind,
    parse_cargo_output,
    parse_command_output,
    parse_eslint_output,
    parse_git_diff_check,
    parse_go_test_output,
    parse_gobuild_output,
    parse_mypy_output,
    parse_pytest_output,
    parse_ruff_output,
    parse_tsc_output,
    parse_vitest_output,
)


def test_parse_green_vitest_output() -> None:
    parsed = parse_vitest_output("Test Files  30 passed (30)\nTests  370 passed | 9 skipped (379)\n")

    assert parsed["test_files"]["passed"] == 30
    assert parsed["tests"]["passed"] == 370
    assert parsed["tests"]["skipped"] == 9
    assert "passed" in parsed["summary"]


def test_parse_failed_vitest_output() -> None:
    output = """
FAIL test/scenarios/navigation.e2e.test.ts > Navigation
AssertionError: expected /ai engine/i, got Модели и AI
Test Files  8 failed | 67 passed | 14 skipped (89)
Tests  8 failed | 370 passed | 9 skipped (387)
"""
    parsed = parse_vitest_output(output)

    assert parsed["test_files"]["failed"] == 8
    assert parsed["tests"]["failed"] == 8
    assert parsed["failures"][0]["file"] == "test/scenarios/navigation.e2e.test.ts"


def test_parse_tsc_output() -> None:
    parsed = parse_tsc_output("src/a.ts(10,5): error TS2322: Type string is not assignable.\n")

    assert parsed["error_count"] == 1
    assert parsed["diagnostics"][0]["code"] == "TS2322"
    assert parsed["diagnostics"][0]["line"] == 10


def test_parse_git_diff_check() -> None:
    assert parse_git_diff_check("")["summary"] == "clean"
    assert parse_git_diff_check("file.ts:1: trailing whitespace.\n")["issues"]


def test_auto_parser_infers_from_command() -> None:
    parsed = parse_command_output("npm run test:fast -w packages/integration", "Tests  1 passed (1)\n")

    assert parsed
    assert parsed["kind"] == "vitest"


PYTEST_FAILED_OUTPUT = """
============================= test session starts ==============================
collected 2 items

tests/test_a.py .F                                                       [100%]

=================================== FAILURES ===================================
___________________________________ test_b ____________________________________

    def test_b():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:5: AssertionError
FAILED tests/test_a.py::test_b - assert 1 == 2
========================= 1 failed, 1 passed in 0.03s =========================
"""


def test_infer_parse_kind_pytest_variants() -> None:
    assert infer_parse_kind("pytest -x -q") == "pytest"
    assert infer_parse_kind("python -m pytest tests/") == "pytest"
    assert infer_parse_kind("python3 -m pytest -k foo") == "pytest"


def test_parse_pytest_failed_output() -> None:
    parsed = parse_pytest_output(PYTEST_FAILED_OUTPUT)

    assert parsed["kind"] == "pytest"
    assert parsed["counts"]["failed"] == 1
    assert parsed["counts"]["passed"] == 1
    assert parsed["failures"] == [{"nodeid": "tests/test_a.py::test_b", "message": "assert 1 == 2"}]
    assert "1 failed, 1 passed in 0.03s" == parsed["summary"]


def test_parse_pytest_quiet_mode_summary() -> None:
    parsed = parse_pytest_output("1 passed in 0.01s\n")

    assert parsed["counts"]["passed"] == 1
    assert parsed["counts"]["failed"] == 0
    assert parsed["failures"] == []


def test_infer_from_output_detects_pytest_without_command_hint() -> None:
    assert infer_from_output(PYTEST_FAILED_OUTPUT) == "pytest"
    assert infer_from_output("1 passed in 0.01s\n") == "pytest"


GO_TEST_OUTPUT = """
=== RUN   TestFoo
--- FAIL: TestFoo (0.00s)
    foo_test.go:10: expected 1 got 2
=== RUN   TestBar
--- PASS: TestBar (0.00s)
FAIL	example.com/pkg	0.004s
"""


def test_infer_parse_kind_go() -> None:
    assert infer_parse_kind("go test ./...") == "gotest"
    assert infer_parse_kind("go vet ./...") == "gobuild"
    assert infer_parse_kind("go build ./...") == "gobuild"


def test_parse_go_test_output() -> None:
    parsed = parse_go_test_output(GO_TEST_OUTPUT)

    assert parsed["kind"] == "gotest"
    assert parsed["counts"] == {"passed": 1, "failed": 1}
    assert parsed["failures"] == [{"test": "TestFoo"}]
    assert parsed["packages"]["failed"] == ["example.com/pkg"]


def test_parse_gobuild_output_diagnostics() -> None:
    parsed = parse_gobuild_output("main.go:12:5: unreachable code\n")

    assert parsed["kind"] == "gobuild"
    assert parsed["error_count"] == 1
    assert parsed["diagnostics"][0] == {"path": "main.go", "line": 12, "column": 5, "message": "unreachable code"}


def test_infer_from_output_detects_go_test() -> None:
    assert infer_from_output(GO_TEST_OUTPUT) == "gotest"


RUFF_OUTPUT = """
example.py:12:5: E501 line too long (90 > 88 characters)
example.py:20:1: F401 'os' imported but unused
Found 2 errors.
"""


def test_infer_parse_kind_ruff() -> None:
    assert infer_parse_kind("ruff check .") == "ruff"


def test_parse_ruff_output() -> None:
    parsed = parse_ruff_output(RUFF_OUTPUT)

    assert parsed["kind"] == "ruff"
    assert parsed["error_count"] == 2
    assert parsed["diagnostics"][0]["code"] == "E501"
    assert parsed["diagnostics"][0]["line"] == 12


def test_infer_from_output_detects_ruff() -> None:
    assert infer_from_output(RUFF_OUTPUT) == "ruff"


MYPY_OUTPUT = """
example.py:12: error: Incompatible return value type (got "int", expected "str")  [return-value]
Found 1 error in 1 file (checked 3 source files)
"""


def test_infer_parse_kind_mypy() -> None:
    assert infer_parse_kind("mypy .") == "mypy"
    assert infer_parse_kind("python -m mypy .") == "mypy"


def test_parse_mypy_output() -> None:
    parsed = parse_mypy_output(MYPY_OUTPUT)

    assert parsed["kind"] == "mypy"
    assert parsed["error_count"] == 1
    assert parsed["diagnostics"][0]["code"] == "return-value"
    assert parsed["diagnostics"][0]["line"] == 12


def test_infer_from_output_detects_mypy() -> None:
    assert infer_from_output(MYPY_OUTPUT) == "mypy"


CARGO_BUILD_ERROR = """
error[E0308]: mismatched types
 --> src/main.rs:3:5
  |
3 |     1
  |     ^ expected `()`, found integer
"""

CARGO_TEST_OUTPUT = "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"


def test_infer_parse_kind_cargo() -> None:
    assert infer_parse_kind("cargo test") == "cargo_test"
    assert infer_parse_kind("cargo build") == "cargo_build"
    assert infer_parse_kind("cargo clippy") == "cargo_build"


def test_parse_cargo_build_errors() -> None:
    parsed = parse_cargo_output(CARGO_BUILD_ERROR)

    assert parsed["kind"] == "cargo"
    assert parsed["errors"][0]["code"] == "E0308"
    assert parsed["errors"][0]["path"] == "src/main.rs"
    assert parsed["errors"][0]["line"] == 3


def test_parse_cargo_test_summary() -> None:
    parsed = parse_cargo_output(CARGO_TEST_OUTPUT)

    assert parsed["counts"] == {"passed": 3, "failed": 0, "ignored": 0}


def test_infer_from_output_detects_cargo() -> None:
    assert infer_from_output(CARGO_BUILD_ERROR) == "cargo_build"
    assert infer_from_output(CARGO_TEST_OUTPUT) == "cargo_test"


ESLINT_OUTPUT = """
/src/index.js
  12:5  error    'foo' is not defined  no-undef
  20:1  warning  Missing semicolon     semi

✖ 2 problems (1 error, 1 warning)
"""


def test_infer_parse_kind_eslint() -> None:
    assert infer_parse_kind("eslint .") == "eslint"
    assert infer_parse_kind("npm run lint") == "eslint"


def test_parse_eslint_output() -> None:
    parsed = parse_eslint_output(ESLINT_OUTPUT)

    assert parsed["kind"] == "eslint"
    assert parsed["counts"] == {"errors": 1, "warnings": 1}
    assert parsed["diagnostics"][0]["path"] == "/src/index.js"
    assert parsed["diagnostics"][0]["rule"] == "no-undef"
    assert parsed["diagnostics"][1]["severity"] == "warning"


def test_make_target_falls_back_to_output_inference() -> None:
    assert infer_parse_kind("make test") == "auto"

    parsed = parse_command_output("make test", PYTEST_FAILED_OUTPUT, "")

    assert parsed is not None
    assert parsed["kind"] == "pytest"


def test_parsers_are_robust_to_empty_and_garbage_input() -> None:
    for fn in (
        parse_pytest_output,
        parse_go_test_output,
        parse_gobuild_output,
        parse_ruff_output,
        parse_mypy_output,
        parse_cargo_output,
        parse_eslint_output,
    ):
        assert fn("", "") is not None
        assert fn("garbage \x00 nonsense !!!", "more \n garbage") is not None

    assert infer_from_output("", "") is None
    assert infer_from_output("nothing recognizable here", "") is None
