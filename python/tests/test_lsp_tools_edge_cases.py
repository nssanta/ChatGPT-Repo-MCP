from __future__ import annotations

from pathlib import Path

from chatrepo_mcp import lsp_tools
from test_command_tools import make_settings


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_returns_none_on_subprocess_error(monkeypatch) -> None:
    monkeypatch.setattr(lsp_tools.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))

    assert lsp_tools._run(["echo", "hi"], Path("/tmp")) is None


def test_pyright_diagnostics_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/usr/bin/pyright")
    monkeypatch.setattr(lsp_tools.subprocess, "run", lambda *args, **kwargs: _Result(0, "{not json"))

    result = lsp_tools._pyright_diagnostics(Path("/tmp"), None, make_settings(Path("/tmp")))

    assert result is None


def test_normalize_path_falls_back_when_resolve_fails(monkeypatch, tmp_path: Path) -> None:
    def fake_display(path, settings):
        raise OSError("blocked")

    monkeypatch.setattr(lsp_tools, "display_path", fake_display)

    assert lsp_tools._normalize_path(tmp_path, "missing.py", make_settings(tmp_path)) == str(tmp_path / "missing.py")


def test_code_diagnostics_supports_absolute_paths_and_rejects_invalid_options(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    absolute = tmp_path / "a.py"
    absolute.write_text("x=1\n", encoding="utf-8")

    seen = {"paths": []}

    def fake_resolve_path_context(path: str, settings, allow_hidden: bool):
        seen["paths"].append(path)
        class _Context:
            target = Path(path)

        return _Context()

    def fake_run_pyright(cwd: Path, paths: list[str] | None, settings):
        return [{"path": str(absolute), "line": 1, "col": 1, "severity": "error", "code": "e1", "message": "bad"}]

    monkeypatch.setattr(lsp_tools, "resolve_path_context", fake_resolve_path_context)
    monkeypatch.setattr(lsp_tools, "_pyright_diagnostics", fake_run_pyright)

    result = lsp_tools.code_diagnostics(settings, paths=[str(absolute)], language="python")

    assert result["ok"] is True
    assert result["diagnostics"][0]["path"] == str(absolute)
    assert any(str(absolute) in p for p in seen["paths"])
    assert result["language"] == "python"


def test_code_diagnostics_filters_by_severity(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    diagnostics = [
        {"path": "a.py", "line": 1, "col": 1, "severity": "warning", "code": None, "message": "warn"},
        {"path": "a.py", "line": 2, "col": 1, "severity": "hint", "code": None, "message": "hint"},
    ]

    monkeypatch.setattr(lsp_tools, "_pyright_diagnostics", lambda cwd, paths, settings: None)
    monkeypatch.setattr(lsp_tools, "_ruff_diagnostics", lambda cwd, paths, settings: diagnostics)

    result = lsp_tools.code_diagnostics(settings, language="python", severity_min="warning")

    assert len(result["diagnostics"]) == 1
    assert result["diagnostics"][0]["severity"] == "warning"
