from __future__ import annotations

from pathlib import Path

from chatrepo_mcp import lsp_tools
from chatrepo_mcp.security import SecurityError

from test_command_tools import make_settings


class _SimpleResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_go_diagnostics_reports_missing_binary_and_run_failure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: None)
    missing = lsp_tools._go_diagnostics(tmp_path, settings, None)
    assert missing == (
        [],
        [],
        [{"tool": "go", "install_hint": "install Go from https://go.dev/dl/ (e.g. `apt install golang-go` / `brew install go`)"}],
    )

    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: None)
    empty = lsp_tools._go_diagnostics(tmp_path, settings, None)

    assert empty == ([], [], [])


def test_go_diagnostics_maps_gobuild_output_and_tools_used(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    root = tmp_path / "svc"
    root.mkdir()
    (root / "bad.go").write_text("package main\n", encoding="utf-8")

    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(
        lsp_tools,
        "_run",
        lambda cmd, cwd: (0, "bad.go:12:3: syntax error", "") ,
    )

    diagnostics, used, missing = lsp_tools._go_diagnostics(root, settings, ["./..."])

    assert missing == []
    assert used == ["go vet ./..."]
    assert diagnostics == [
        {
            "path": "svc/bad.go",
            "line": 12,
            "col": 3,
            "severity": "error",
            "code": None,
            "message": "syntax error",
        }
    ]


def test_python_diagnostics_prefers_pyright_when_available(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: f"/{name}" if name == "pyright" else None)

    monkeypatch.setattr(
        lsp_tools.subprocess,
        "run",
        lambda *args, **kwargs: _SimpleResult(
            0,
            '{"generalDiagnostics":[{"file":"src/main.py","range":{"start":{"line":3,"character":4}},"severity":"information","rule":"reportGeneralTypeIssues","message":"type mismatch"}]}'
        ),
    )

    diagnostics, used, missing = lsp_tools._python_diagnostics(tmp_path, settings, None)

    assert used == ["pyright --outputjson"]
    assert diagnostics == [
        {
            "path": "src/main.py",
            "line": 4,
            "col": 5,
            "severity": "info",
            "code": "reportGeneralTypeIssues",
            "message": "type mismatch",
        }
    ]
    assert missing == []


def test_python_diagnostics_falls_back_to_ruff_then_compileall(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/bin/ruff" if name == "ruff" else "/usr/bin/python3")
    monkeypatch.setattr(lsp_tools, "_pyright_diagnostics", lambda *args, **kwargs: None)

    monkeypatch.setattr(
        lsp_tools,
        "_ruff_diagnostics",
        lambda cwd, paths, settings: [
            {
                "path": "a.py",
                "line": 1,
                "col": 1,
                "severity": "warning",
                "code": "F401",
                "message": "unused import",
            }
        ],
    )

    diagnostics, used, missing = lsp_tools._python_diagnostics(tmp_path, settings, None)

    assert used == ["ruff check --output-format json"]
    assert missing == [{"tool": "pyright", "install_hint": "pip install pyright  (or: npm install -g pyright)"}]
    assert diagnostics == [{"path": "a.py", "line": 1, "col": 1, "severity": "warning", "code": "F401", "message": "unused import"}]


def test_compileall_reports_diagnostics_and_is_used_when_ruff_missing(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        lsp_tools.shutil,
        "which",
        lambda name: "/usr/bin/python3" if name == "python3" else None,
    )
    monkeypatch.setattr(lsp_tools, "_pyright_diagnostics", lambda *args, **kwargs: None)

    monkeypatch.setattr(
        lsp_tools.subprocess,
        "run",
        lambda *args, **kwargs: _SimpleResult(0, '  File "bad.py", line 2\n    invalid syntax\n', ""),
    )

    diagnostics, used, missing = lsp_tools._python_diagnostics(tmp_path, settings, None)

    assert used == ["python3 -m compileall -q"]
    assert diagnostics == [
        {"path": "bad.py", "line": 2, "col": None, "severity": "error", "code": None, "message": "invalid syntax"}
    ]
    assert missing == [
        {"tool": "pyright", "install_hint": "pip install pyright  (or: npm install -g pyright)"},
        {"tool": "ruff", "install_hint": "pip install ruff"},
    ]


def test_tsc_finds_local_binary_and_parses_output(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\necho tsc\n", encoding="utf-8")

    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: (0, "main.ts(4,2): error TS2300: Duplicate identifier", ""))
    monkeypatch.setattr(
        lsp_tools.parsers,
        "parse_tsc_output",
        lambda stdout, stderr: {
            "diagnostics": [{"path": "main.ts", "line": 4, "column": 2, "code": "TS2300", "message": "Duplicate identifier"}]
        },
    )

    diagnostics, used, missing = lsp_tools._ts_diagnostics(tmp_path, settings, ["main.ts"])

    assert used == [str((tmp_path / "node_modules/.bin/tsc").resolve()) + " --noEmit --pretty false main.ts"]
    assert missing == []
    assert diagnostics == [{"path": "main.ts", "line": 4, "col": 2, "severity": "error", "code": "TS2300", "message": "Duplicate identifier"}]


def test_tsc_reports_missing_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(lsp_tools, "_find_tsc", lambda cwd: None)

    diagnostics, used, missing = lsp_tools._ts_diagnostics(tmp_path, settings, None)

    assert diagnostics == []
    assert used == []
    assert missing == [{"tool": "tsc", "install_hint": "npm install -D typescript  (or: npm install -g typescript)"}]


def test_code_diagnostics_rejects_option_path_and_supports_auto_stack_detection(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module demo\n", encoding="utf-8")

    monkeypatch.setattr(lsp_tools.workspace, "detect_stack", lambda cwd: {"go", "python"})
    monkeypatch.setattr(
        lsp_tools,
        "_go_diagnostics",
        lambda cwd, settings, paths: ([], [f'go vet {" ".join(paths or ["./..."])}'], []),
    )
    monkeypatch.setitem(lsp_tools._LANGUAGE_RUNNERS, "go", lambda cwd, settings, paths: ([], [f"go vet {' '.join(paths or ['./...'])}"], []))
    monkeypatch.setattr(lsp_tools, "_pyright_diagnostics", lambda cwd, paths, settings: None)
    monkeypatch.setattr(
        lsp_tools,
        "_ruff_diagnostics",
        lambda cwd, paths, settings: [{"path": "a.py", "line": 1, "col": 1, "severity": "warning", "code": "F401", "message": "x"}],
    )
    monkeypatch.setattr(
        lsp_tools.shutil,
        "which",
        lambda name: "/bin/ruff" if name == "ruff" else "/usr/bin/python3" if name == "python3" else None,
    )

    result = lsp_tools.code_diagnostics(settings, paths=["a.py"], language="auto", limit=1)

    assert result["ok"] is True
    assert result["diagnostics"] == [{"path": "a.py", "line": 1, "col": 1, "severity": "warning", "code": "F401", "message": "x"}]
    assert result["tool_used"] == [f"go vet {tmp_path / 'a.py'}", "ruff check --output-format json"]

    try:
        lsp_tools.code_diagnostics(settings, paths=["-n"])
        assert False, "expected SecurityError"
    except SecurityError:
        assert True


def test_code_diagnostics_handles_unsupported_language_with_missing_entry(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    result = lsp_tools.code_diagnostics(settings, language="rust")

    assert result["ok"] is True
    assert result["language"] == "rust"
    assert result["missing_tools"] == [{"tool": "rust", "install_hint": "unsupported language: rust"}]
