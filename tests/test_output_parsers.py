from chatrepo_mcp.parsers import parse_command_output, parse_git_diff_check, parse_tsc_output, parse_vitest_output


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
